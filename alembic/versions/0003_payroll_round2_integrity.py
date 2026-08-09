"""Harden payroll relational invariants and make migration changes reversible.

Revision ID: 0003_payroll_round2_integrity
Revises: 0002_payroll
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_payroll_round2_integrity"
down_revision = "0002_payroll"
branch_labels = None
depends_on = None


def _assert_legacy_voucher_line_organizations() -> None:
    """Fail closed when rows cannot be assigned a single organization."""

    bind = op.get_bind()
    voucher_lines = sa.table(
        "voucher_lines",
        sa.column("id", sa.Uuid()),
        sa.column("voucher_id", sa.Uuid()),
        sa.column("account_id", sa.Uuid()),
        sa.column("counterparty_id", sa.Uuid()),
    )
    vouchers = sa.table(
        "vouchers", sa.column("id", sa.Uuid()), sa.column("org_id", sa.Uuid())
    )
    accounts = sa.table(
        "accounts", sa.column("id", sa.Uuid()), sa.column("org_id", sa.Uuid())
    )
    counterparties = sa.table(
        "counterparties", sa.column("id", sa.Uuid()), sa.column("org_id", sa.Uuid())
    )
    polluted = bind.execute(
        sa.select(voucher_lines.c.id)
        .select_from(
            voucher_lines.join(vouchers, vouchers.c.id == voucher_lines.c.voucher_id)
            .join(accounts, accounts.c.id == voucher_lines.c.account_id)
            .outerjoin(
                counterparties, counterparties.c.id == voucher_lines.c.counterparty_id
            )
        )
        .where(
            sa.or_(
                vouchers.c.org_id != accounts.c.org_id,
                sa.and_(
                    voucher_lines.c.counterparty_id.is_not(None),
                    vouchers.c.org_id != counterparties.c.org_id,
                ),
            )
        )
        .limit(1)
    ).scalar_one_or_none()
    if polluted is not None:
        raise RuntimeError("VOUCHER_LINE_ORG_INVARIANT_VIOLATION")


def _assert_round2_downgrade_safe() -> None:
    """Reject a downgrade before it can discard payroll history or new links."""

    bind = op.get_bind()
    guarded_tables = (
        "payroll_batches",
        "payroll_withholding_entitlements",
        "payroll_withholding_payment_allocations",
        "payroll_tax_state_slots",
        "payroll_event_links",
        "payroll_batch_evidence",
    )
    for table_name in guarded_tables:
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).scalar() is not None:
            raise RuntimeError(
                "PAYROLL_DOWNGRADE_UNSAFE: payroll data exists; preserve accounting history"
            )


def upgrade() -> None:
    # Preflight is intentionally first: a polluted legacy row must leave this
    # revision unapplied rather than receiving an arbitrary organization id.
    _assert_legacy_voucher_line_organizations()

    with op.batch_alter_table("business_events") as batch_op:
        batch_op.add_column(sa.Column("request_payload_hash", sa.String(length=64), nullable=True))

    with op.batch_alter_table("evidence") as batch_op:
        batch_op.create_unique_constraint("uq_evidence_org_id", ["org_id", "id"])

    with op.batch_alter_table("employee_payroll_profile_versions") as batch_op:
        batch_op.add_column(sa.Column("supersedes_id", sa.Uuid(), nullable=True))
        batch_op.drop_constraint(
            "uq_employee_payroll_profile_effective_from", type_="unique"
        )
        batch_op.create_unique_constraint("uq_payroll_profile_successor", ["supersedes_id"])
        batch_op.create_foreign_key(
            "fk_payroll_profile_supersedes",
            "employee_payroll_profile_versions",
            ["org_id", "employee_id", "supersedes_id"],
            ["org_id", "employee_id", "id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("payroll_policy_versions") as batch_op:
        batch_op.add_column(sa.Column("supersedes_id", sa.Uuid(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_payroll_policy_org_region_id", ["org_id", "region", "id"]
        )
        batch_op.create_unique_constraint("uq_payroll_policy_successor", ["supersedes_id"])
        batch_op.create_foreign_key(
            "fk_payroll_policy_supersedes",
            "payroll_policy_versions",
            ["org_id", "region", "supersedes_id"],
            ["org_id", "region", "id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("payroll_opening_states") as batch_op:
        batch_op.add_column(sa.Column("supersedes_id", sa.Uuid(), nullable=True))
        batch_op.drop_constraint("uq_payroll_opening_state_period", type_="unique")
        batch_op.create_unique_constraint(
            "uq_payroll_opening_state_period_id",
            ["org_id", "employee_id", "tax_year", "through_month", "id"],
        )
        batch_op.create_unique_constraint("uq_payroll_opening_state_successor", ["supersedes_id"])
        batch_op.create_foreign_key(
            "fk_payroll_opening_state_supersedes",
            "payroll_opening_states",
            ["org_id", "employee_id", "tax_year", "through_month", "supersedes_id"],
            ["org_id", "employee_id", "tax_year", "through_month", "id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("payroll_lines") as batch_op:
        batch_op.add_column(sa.Column("regular_payroll_batch_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_payroll_line_org_regular_batch",
            "payroll_batches",
            ["org_id", "regular_payroll_batch_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("voucher_lines") as batch_op:
        batch_op.add_column(sa.Column("org_id", sa.Uuid(), nullable=True))
    voucher_lines = sa.table(
        "voucher_lines",
        sa.column("voucher_id", sa.Uuid()),
        sa.column("org_id", sa.Uuid()),
    )
    vouchers = sa.table(
        "vouchers", sa.column("id", sa.Uuid()), sa.column("org_id", sa.Uuid())
    )
    op.execute(
        voucher_lines.update().values(
            org_id=sa.select(vouchers.c.org_id)
            .where(vouchers.c.id == voucher_lines.c.voucher_id)
            .scalar_subquery()
        )
    )
    with op.batch_alter_table("voucher_lines") as batch_op:
        batch_op.alter_column("org_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_foreign_key(
            "fk_voucher_line_org_voucher",
            "vouchers",
            ["org_id", "voucher_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_voucher_line_org_account",
            "accounts",
            ["org_id", "account_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_foreign_key(
            "fk_voucher_line_org_counterparty",
            "counterparties",
            ["org_id", "counterparty_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
    op.create_index("ix_voucher_lines_org_id", "voucher_lines", ["org_id"])

    op.create_table(
        "payroll_withholding_entitlements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_line_id", sa.Uuid(), nullable=False),
        sa.Column("contribution_group", sa.String(length=50), nullable=False),
        sa.Column("insurance_kind", sa.String(length=50), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "contribution_group IN ('employee_social_insurance','employee_housing_fund',"
            "'individual_income_tax')",
            name="ck_withholding_entitlement_group",
        ),
        sa.CheckConstraint("amount_fen >= 0", name="ck_withholding_entitlement_amount"),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_line_id"],
            ["payroll_lines.org_id", "payroll_lines.id"],
            name="fk_withholding_entitlement_org_line",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_withholding_entitlement_org_id"),
        sa.UniqueConstraint(
            "org_id",
            "payroll_line_id",
            "contribution_group",
            "insurance_kind",
            name="uq_withholding_entitlement_kind",
        ),
    )
    op.create_index(
        "ix_payroll_withholding_entitlements_org_id",
        "payroll_withholding_entitlements",
        ["org_id"],
    )
    op.create_index(
        "ix_payroll_withholding_entitlements_payroll_line_id",
        "payroll_withholding_entitlements",
        ["payroll_line_id"],
    )
    op.create_table(
        "payroll_withholding_payment_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("entitlement_id", sa.Uuid(), nullable=False),
        sa.Column("payment_event_id", sa.Uuid(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("reversed", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_fen > 0", name="ck_withholding_payment_amount"),
        sa.ForeignKeyConstraint(
            ["org_id", "entitlement_id"],
            ["payroll_withholding_entitlements.org_id", "payroll_withholding_entitlements.id"],
            name="fk_withholding_payment_org_entitlement",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_withholding_payment_org_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "entitlement_id",
            "payment_event_id",
            name="uq_withholding_payment_entitlement_event",
        ),
    )
    op.create_index(
        "ix_payroll_withholding_payment_allocations_org_id",
        "payroll_withholding_payment_allocations",
        ["org_id"],
    )
    op.create_index(
        "ix_payroll_withholding_payment_allocations_entitlement_id",
        "payroll_withholding_payment_allocations",
        ["entitlement_id"],
    )
    op.create_index(
        "ix_payroll_withholding_payment_allocations_payment_event_id",
        "payroll_withholding_payment_allocations",
        ["payment_event_id"],
    )

    op.create_table(
        "payroll_tax_state_slots",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("tax_month", sa.Integer(), nullable=False),
        sa.Column("regular_batch_id", sa.Uuid(), nullable=False),
        sa.Column("final_batch_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("tax_year BETWEEN 1900 AND 9999", name="ck_payroll_tax_slot_year"),
        sa.CheckConstraint("tax_month BETWEEN 1 AND 12", name="ck_payroll_tax_slot_month"),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_payroll_tax_slot_org_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "regular_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_tax_slot_org_regular_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "final_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_tax_slot_org_final_batch",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "employee_id", "tax_year", "tax_month", name="uq_payroll_tax_state_slot"
        ),
    )
    op.create_index("ix_payroll_tax_state_slots_org_id", "payroll_tax_state_slots", ["org_id"])
    op.create_index(
        "ix_payroll_tax_state_slots_employee_id", "payroll_tax_state_slots", ["employee_id"]
    )

    op.create_table(
        "payroll_event_links",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_batch_id", sa.Uuid(), nullable=False),
        sa.Column("source_payment_event_id", sa.Uuid(), nullable=True),
        sa.Column("link_kind", sa.String(length=40), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "link_kind IN ('payroll_accrual','salary_payment','statutory_payment','reversal')",
            name="ck_payroll_event_link_kind",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_payroll_event_link_org_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_event_link_org_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_payroll_event_link_org_source_payment",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "event_id", "link_kind", name="uq_payroll_event_link"),
    )
    op.create_index("ix_payroll_event_links_org_id", "payroll_event_links", ["org_id"])
    op.create_table(
        "payroll_batch_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_batch_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_batch_evidence_org_batch",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_payroll_batch_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "payroll_batch_id", "evidence_id"),
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_final_voucher(target_voucher_id uuid)
        RETURNS void AS $$
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE debit_total bigint;
        DECLARE credit_total bigint;
        DECLARE line_count bigint;
        BEGIN
            SELECT * INTO target_voucher FROM vouchers WHERE id = target_voucher_id;
            IF NOT FOUND OR target_voucher.status NOT IN ('posted', 'reversed') THEN
                RETURN;
            END IF;
            SELECT COALESCE(SUM(debit_fen), 0), COALESCE(SUM(credit_fen), 0), COUNT(*)
              INTO debit_total, credit_total, line_count
              FROM voucher_lines
             WHERE voucher_id = target_voucher.id AND org_id = target_voucher.org_id;
            IF line_count < 2 OR debit_total <= 0 OR debit_total <> credit_total THEN
                RAISE EXCEPTION 'final voucher requires at least two balanced nonzero lines';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_voucher()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_final_voucher(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_voucher_from_line()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_voucher(OLD.voucher_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_voucher(NEW.voucher_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER final_voucher_balance_deferred
        AFTER INSERT OR UPDATE OR DELETE ON vouchers
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_voucher();

        CREATE CONSTRAINT TRIGGER final_voucher_line_balance_deferred
        AFTER INSERT OR UPDATE OR DELETE ON voucher_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_voucher_from_line();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_final_business_event_mutation()
        RETURNS trigger AS $$
        DECLARE valid_reversal boolean;
        BEGIN
            IF TG_OP = 'DELETE' AND OLD.status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final business events are immutable; create a reversal';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status IN ('posted', 'reversed') THEN
                IF OLD.status = 'posted'
                   AND NEW.status = 'reversed'
                   AND NEW.reversed_by_event_id IS NOT NULL
                   AND (to_jsonb(NEW) - ARRAY['status', 'reversed_by_event_id'])
                       = (to_jsonb(OLD) - ARRAY['status', 'reversed_by_event_id']) THEN
                    SELECT EXISTS (
                        SELECT 1 FROM business_events reversal
                         WHERE reversal.id = NEW.reversed_by_event_id
                           AND reversal.org_id = OLD.org_id
                           AND reversal.id <> OLD.id
                           AND reversal.status IN ('posted', 'reversed')
                    ) INTO valid_reversal;
                    IF valid_reversal THEN
                        RETURN NEW;
                    END IF;
                END IF;
                RAISE EXCEPTION 'final business events are immutable; create a reversal';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_final_business_event
        BEFORE UPDATE OR DELETE ON business_events
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_business_event_mutation();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_payroll_withholding_entitlement(
            target_entitlement_id uuid
        )
        RETURNS void AS $$
        DECLARE target_entitlement payroll_withholding_entitlements%ROWTYPE;
        DECLARE active_total bigint;
        BEGIN
            SELECT * INTO target_entitlement
              FROM payroll_withholding_entitlements WHERE id = target_entitlement_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT COALESCE(SUM(amount_fen) FILTER (WHERE reversed IS FALSE), 0)
              INTO active_total
              FROM payroll_withholding_payment_allocations
             WHERE entitlement_id = target_entitlement.id
               AND org_id = target_entitlement.org_id;
            IF active_total > target_entitlement.amount_fen THEN
                RAISE EXCEPTION 'payroll withholding allocation exceeds per-kind entitlement';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_withholding_entitlement()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_payroll_withholding_entitlement(
                COALESCE(NEW.id, OLD.id)
            );
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_withholding_payment()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_payroll_withholding_entitlement(OLD.entitlement_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_payroll_withholding_entitlement(NEW.entitlement_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER payroll_withholding_entitlement_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_withholding_entitlements
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_withholding_entitlement();

        CREATE CONSTRAINT TRIGGER payroll_withholding_payment_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_withholding_payment_allocations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_withholding_payment();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_validate_profile_version_chain()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.supersedes_id = NEW.id THEN
                RAISE EXCEPTION 'payroll profile version cannot supersede itself';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT version.supersedes_id
                      FROM employee_payroll_profile_versions version
                      JOIN ancestors ancestor ON version.id = ancestor.id
                     WHERE version.supersedes_id IS NOT NULL
                )
                SELECT 1 FROM ancestors WHERE id = NEW.id
            ) THEN
                RAISE EXCEPTION 'payroll profile supersession cycle';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT version.supersedes_id
                      FROM employee_payroll_profile_versions version
                      JOIN ancestors ancestor ON version.id = ancestor.id
                     WHERE version.supersedes_id IS NOT NULL
                ), descendants(id) AS (
                    SELECT version.id FROM employee_payroll_profile_versions version
                     WHERE version.supersedes_id = NEW.id
                    UNION
                    SELECT version.id FROM employee_payroll_profile_versions version
                      JOIN descendants descendant ON version.supersedes_id = descendant.id
                ), lineage(id) AS (
                    SELECT id FROM ancestors UNION SELECT id FROM descendants
                )
                SELECT 1 FROM employee_payroll_profile_versions other
                 WHERE other.org_id = NEW.org_id
                   AND other.employee_id = NEW.employee_id
                   AND other.id <> NEW.id
                   AND other.effective_from <= COALESCE(NEW.effective_to, 'infinity'::date)
                   AND NEW.effective_from <= COALESCE(other.effective_to, 'infinity'::date)
                   AND NOT EXISTS (SELECT 1 FROM lineage WHERE lineage.id = other.id)
            ) THEN
                RAISE EXCEPTION 'overlapping payroll profile requires explicit supersession';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER payroll_profile_version_chain
        BEFORE INSERT OR UPDATE ON employee_payroll_profile_versions
        FOR EACH ROW EXECUTE FUNCTION finance_validate_profile_version_chain();

        CREATE OR REPLACE FUNCTION finance_validate_policy_version_chain()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.supersedes_id = NEW.id THEN
                RAISE EXCEPTION 'payroll policy version cannot supersede itself';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT version.supersedes_id
                      FROM payroll_policy_versions version
                      JOIN ancestors ancestor ON version.id = ancestor.id
                     WHERE version.supersedes_id IS NOT NULL
                )
                SELECT 1 FROM ancestors WHERE id = NEW.id
            ) THEN
                RAISE EXCEPTION 'payroll policy supersession cycle';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT version.supersedes_id
                      FROM payroll_policy_versions version
                      JOIN ancestors ancestor ON version.id = ancestor.id
                     WHERE version.supersedes_id IS NOT NULL
                ), descendants(id) AS (
                    SELECT version.id FROM payroll_policy_versions version
                     WHERE version.supersedes_id = NEW.id
                    UNION
                    SELECT version.id FROM payroll_policy_versions version
                      JOIN descendants descendant ON version.supersedes_id = descendant.id
                ), lineage(id) AS (
                    SELECT id FROM ancestors UNION SELECT id FROM descendants
                )
                SELECT 1 FROM payroll_policy_versions other
                 WHERE other.org_id = NEW.org_id
                   AND other.region = NEW.region
                   AND other.id <> NEW.id
                   AND other.effective_from <= COALESCE(NEW.effective_to, 'infinity'::date)
                   AND NEW.effective_from <= COALESCE(other.effective_to, 'infinity'::date)
                   AND NOT EXISTS (SELECT 1 FROM lineage WHERE lineage.id = other.id)
            ) THEN
                RAISE EXCEPTION 'overlapping payroll policy requires explicit supersession';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER payroll_policy_version_chain
        BEFORE INSERT OR UPDATE ON payroll_policy_versions
        FOR EACH ROW EXECUTE FUNCTION finance_validate_policy_version_chain();

        CREATE OR REPLACE FUNCTION finance_validate_opening_state_version_chain()
        RETURNS trigger AS $$
        BEGIN
            IF NEW.supersedes_id = NEW.id THEN
                RAISE EXCEPTION 'payroll opening state cannot supersede itself';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT state.supersedes_id
                      FROM payroll_opening_states state
                      JOIN ancestors ancestor ON state.id = ancestor.id
                     WHERE state.supersedes_id IS NOT NULL
                )
                SELECT 1 FROM ancestors WHERE id = NEW.id
            ) THEN
                RAISE EXCEPTION 'payroll opening state supersession cycle';
            END IF;
            IF EXISTS (
                WITH RECURSIVE ancestors(id) AS (
                    SELECT NEW.supersedes_id WHERE NEW.supersedes_id IS NOT NULL
                    UNION
                    SELECT state.supersedes_id
                      FROM payroll_opening_states state
                      JOIN ancestors ancestor ON state.id = ancestor.id
                     WHERE state.supersedes_id IS NOT NULL
                ), descendants(id) AS (
                    SELECT state.id FROM payroll_opening_states state
                     WHERE state.supersedes_id = NEW.id
                    UNION
                    SELECT state.id FROM payroll_opening_states state
                      JOIN descendants descendant ON state.supersedes_id = descendant.id
                ), lineage(id) AS (
                    SELECT id FROM ancestors UNION SELECT id FROM descendants
                )
                SELECT 1 FROM payroll_opening_states other
                 WHERE other.org_id = NEW.org_id
                   AND other.employee_id = NEW.employee_id
                   AND other.tax_year = NEW.tax_year
                   AND other.through_month = NEW.through_month
                   AND other.id <> NEW.id
                   AND NOT EXISTS (SELECT 1 FROM lineage WHERE lineage.id = other.id)
            ) THEN
                RAISE EXCEPTION 'opening state correction requires explicit supersession';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER payroll_opening_state_version_chain
        BEFORE INSERT OR UPDATE ON payroll_opening_states
        FOR EACH ROW EXECUTE FUNCTION finance_validate_opening_state_version_chain();
        """
    )
    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_final_payroll_batch(target_batch_id uuid)
        RETURNS void AS $$
        DECLARE target_batch payroll_batches%ROWTYPE;
        DECLARE final_voucher_id uuid;
        BEGIN
            SELECT * INTO target_batch FROM payroll_batches WHERE id = target_batch_id;
            IF NOT FOUND OR target_batch.status NOT IN ('posted', 'reversed') THEN
                RETURN;
            END IF;
            IF target_batch.business_event_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM business_events event
                 WHERE event.id = target_batch.business_event_id
                   AND event.org_id = target_batch.org_id
                   AND event.event_type = 'payroll_accrual'
                   AND event.status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'final payroll batch lacks payroll_accrual event';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM payroll_lines line
                 WHERE line.payroll_batch_id = target_batch.id
                   AND line.org_id = target_batch.org_id
            ) THEN
                RAISE EXCEPTION 'final payroll batch requires at least one payroll line';
            END IF;
            SELECT voucher.id INTO final_voucher_id
              FROM vouchers voucher
             WHERE voucher.event_id = target_batch.business_event_id
               AND voucher.org_id = target_batch.org_id
               AND voucher.status IN ('posted', 'reversed');
            IF final_voucher_id IS NULL THEN
                RAISE EXCEPTION 'final payroll batch requires a same-organization final voucher';
            END IF;
            PERFORM finance_assert_final_voucher(final_voucher_id);
            IF target_batch.status = 'reversed' AND NOT EXISTS (
                SELECT 1 FROM payroll_batches reversal
                 WHERE reversal.reversal_of_batch_id = target_batch.id
                   AND reversal.org_id = target_batch.org_id
                   AND reversal.status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'reversed payroll batch requires a linked final reversal batch';
            END IF;
        END;
        $$ LANGUAGE plpgsql;
        """
    )


def downgrade() -> None:
    _assert_round2_downgrade_safe()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for statement in (
            "DROP TRIGGER IF EXISTS payroll_opening_state_version_chain ON payroll_opening_states",
            "DROP FUNCTION IF EXISTS finance_validate_opening_state_version_chain()",
            "DROP TRIGGER IF EXISTS payroll_policy_version_chain ON payroll_policy_versions",
            "DROP FUNCTION IF EXISTS finance_validate_policy_version_chain()",
            "DROP TRIGGER IF EXISTS payroll_profile_version_chain "
            "ON employee_payroll_profile_versions",
            "DROP FUNCTION IF EXISTS finance_validate_profile_version_chain()",
            "DROP TRIGGER IF EXISTS payroll_withholding_payment_invariant_deferred "
            "ON payroll_withholding_payment_allocations",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_withholding_payment()",
            "DROP TRIGGER IF EXISTS payroll_withholding_entitlement_invariant_deferred "
            "ON payroll_withholding_entitlements",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_withholding_entitlement()",
            "DROP FUNCTION IF EXISTS finance_assert_payroll_withholding_entitlement(uuid)",
            "DROP TRIGGER IF EXISTS immutable_final_business_event ON business_events",
            "DROP FUNCTION IF EXISTS finance_block_final_business_event_mutation()",
            "DROP TRIGGER IF EXISTS final_voucher_line_balance_deferred ON voucher_lines",
            "DROP FUNCTION IF EXISTS finance_validate_final_voucher_from_line()",
            "DROP TRIGGER IF EXISTS final_voucher_balance_deferred ON vouchers",
            "DROP FUNCTION IF EXISTS finance_validate_final_voucher()",
            "DROP FUNCTION IF EXISTS finance_assert_final_voucher(uuid)",
        ):
            op.execute(statement)
        op.execute(
            """
            CREATE OR REPLACE FUNCTION finance_assert_final_payroll_batch(target_batch_id uuid)
            RETURNS void AS $$
            DECLARE target_batch payroll_batches%ROWTYPE;
            BEGIN
                SELECT * INTO target_batch FROM payroll_batches WHERE id = target_batch_id;
                IF NOT FOUND OR target_batch.status NOT IN ('posted', 'reversed') THEN
                    RETURN;
                END IF;
                IF target_batch.business_event_id IS NULL OR NOT EXISTS (
                    SELECT 1 FROM business_events event
                     WHERE event.id = target_batch.business_event_id
                       AND event.org_id = target_batch.org_id
                       AND event.status IN ('posted', 'reversed')
                ) THEN
                    RAISE EXCEPTION 'final payroll batch lacks business event';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM payroll_lines line
                     WHERE line.payroll_batch_id = target_batch.id
                       AND line.org_id = target_batch.org_id
                ) THEN
                    RAISE EXCEPTION 'final payroll batch requires at least one payroll line';
                END IF;
                IF NOT EXISTS (
                    SELECT 1 FROM vouchers voucher
                     WHERE voucher.event_id = target_batch.business_event_id
                       AND voucher.org_id = target_batch.org_id
                       AND voucher.status IN ('posted', 'reversed')
                ) THEN
                    RAISE EXCEPTION 'final payroll batch lacks final voucher';
                END IF;
                IF target_batch.status = 'reversed' AND NOT EXISTS (
                    SELECT 1 FROM payroll_batches reversal
                     WHERE reversal.reversal_of_batch_id = target_batch.id
                       AND reversal.org_id = target_batch.org_id
                       AND reversal.status IN ('posted', 'reversed')
                ) THEN
                    RAISE EXCEPTION 'reversed payroll batch requires a linked final reversal batch';
                END IF;
            END;
            $$ LANGUAGE plpgsql;
            """
        )

    op.drop_index(
        "ix_payroll_withholding_payment_allocations_payment_event_id",
        table_name="payroll_withholding_payment_allocations",
    )
    op.drop_index(
        "ix_payroll_withholding_payment_allocations_entitlement_id",
        table_name="payroll_withholding_payment_allocations",
    )
    op.drop_index(
        "ix_payroll_withholding_payment_allocations_org_id",
        table_name="payroll_withholding_payment_allocations",
    )
    op.drop_index(
        "ix_payroll_withholding_entitlements_payroll_line_id",
        table_name="payroll_withholding_entitlements",
    )
    op.drop_index(
        "ix_payroll_withholding_entitlements_org_id",
        table_name="payroll_withholding_entitlements",
    )
    op.drop_index("ix_payroll_tax_state_slots_employee_id", table_name="payroll_tax_state_slots")
    op.drop_index("ix_payroll_tax_state_slots_org_id", table_name="payroll_tax_state_slots")
    op.drop_index("ix_payroll_event_links_org_id", table_name="payroll_event_links")
    op.drop_table("payroll_batch_evidence")
    op.drop_table("payroll_event_links")
    op.drop_table("payroll_tax_state_slots")
    op.drop_table("payroll_withholding_payment_allocations")
    op.drop_table("payroll_withholding_entitlements")

    op.drop_index("ix_voucher_lines_org_id", table_name="voucher_lines")
    with op.batch_alter_table("voucher_lines") as batch_op:
        batch_op.drop_constraint("fk_voucher_line_org_counterparty", type_="foreignkey")
        batch_op.drop_constraint("fk_voucher_line_org_account", type_="foreignkey")
        batch_op.drop_constraint("fk_voucher_line_org_voucher", type_="foreignkey")
        batch_op.drop_column("org_id")

    with op.batch_alter_table("payroll_lines") as batch_op:
        batch_op.drop_constraint("fk_payroll_line_org_regular_batch", type_="foreignkey")
        batch_op.drop_column("regular_payroll_batch_id")
    with op.batch_alter_table("payroll_opening_states") as batch_op:
        batch_op.drop_constraint("fk_payroll_opening_state_supersedes", type_="foreignkey")
        batch_op.drop_constraint("uq_payroll_opening_state_successor", type_="unique")
        batch_op.drop_constraint("uq_payroll_opening_state_period_id", type_="unique")
        batch_op.create_unique_constraint(
            "uq_payroll_opening_state_period",
            ["org_id", "employee_id", "tax_year", "through_month"],
        )
        batch_op.drop_column("supersedes_id")
    with op.batch_alter_table("payroll_policy_versions") as batch_op:
        batch_op.drop_constraint("fk_payroll_policy_supersedes", type_="foreignkey")
        batch_op.drop_constraint("uq_payroll_policy_successor", type_="unique")
        batch_op.drop_constraint("uq_payroll_policy_org_region_id", type_="unique")
        batch_op.drop_column("supersedes_id")
    with op.batch_alter_table("employee_payroll_profile_versions") as batch_op:
        batch_op.drop_constraint("fk_payroll_profile_supersedes", type_="foreignkey")
        batch_op.drop_constraint("uq_payroll_profile_successor", type_="unique")
        batch_op.create_unique_constraint(
            "uq_employee_payroll_profile_effective_from", ["employee_id", "effective_from"]
        )
        batch_op.drop_column("supersedes_id")
    with op.batch_alter_table("evidence") as batch_op:
        batch_op.drop_constraint("uq_evidence_org_id", type_="unique")
    with op.batch_alter_table("business_events") as batch_op:
        batch_op.drop_column("request_payload_hash")
