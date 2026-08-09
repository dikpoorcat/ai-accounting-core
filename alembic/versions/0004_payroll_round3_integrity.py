"""Close third-round payroll database invariants without rewriting prior revisions.

Revision ID: 0004_payroll_round3_integrity
Revises: 0003_payroll_round2_integrity
Create Date: 2026-08-09
"""

# ruff: noqa: E501

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0004_payroll_round3_integrity"
down_revision = "0003_payroll_round2_integrity"
branch_labels = None
depends_on = None


def _assert_open_item_state_consistency() -> None:
    """Fail before DDL when historical balances and state labels disagree."""

    polluted = op.get_bind().execute(
        sa.text(
            """
            SELECT item.id
              FROM open_items AS item
              LEFT JOIN settlements AS settlement
                ON settlement.org_id = item.org_id
               AND settlement.open_item_id = item.id
             GROUP BY item.id, item.org_id, item.original_amount_fen,
                      item.settled_amount_fen, item.status
            HAVING
                COALESCE(SUM(CASE WHEN settlement.reversed IS FALSE
                                  THEN settlement.amount_fen ELSE 0 END), 0)
                    <> item.settled_amount_fen
                OR COALESCE(SUM(CASE WHEN settlement.reversed IS FALSE
                                     THEN settlement.amount_fen ELSE 0 END), 0)
                    > item.original_amount_fen
                OR (item.status = 'open' AND item.settled_amount_fen <> 0)
                OR (item.status = 'partial' AND NOT (
                    item.settled_amount_fen > 0
                    AND item.settled_amount_fen < item.original_amount_fen
                ))
                OR (item.status = 'settled'
                    AND item.settled_amount_fen <> item.original_amount_fen)
                OR (item.status = 'reversed' AND item.settled_amount_fen <> 0)
                OR item.status NOT IN ('open', 'partial', 'settled', 'reversed')
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if polluted is not None:
        raise RuntimeError("OPEN_ITEM_STATE_INVARIANT_VIOLATION")


def _assert_legacy_bank_match_organizations() -> None:
    """A legacy pointer may only be migrated when its event is organization-bound."""

    polluted = op.get_bind().execute(
        sa.text(
            """
            SELECT bank.id
              FROM bank_transactions AS bank
              JOIN business_events AS event ON event.id = bank.matched_event_id
             WHERE bank.matched_event_id IS NOT NULL
               AND event.org_id <> bank.org_id
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if polluted is not None:
        raise RuntimeError("BANK_TRANSACTION_MATCH_ORGANIZATION_VIOLATION")


def _assert_legacy_final_event_state() -> None:
    """Refuse ambiguous historical 'refund means reversed' state before hardening."""

    bind = op.get_bind()
    ambiguous_refund = bind.execute(
        sa.text(
            """
            SELECT original.id
              FROM business_events AS original
              JOIN business_events AS refund
                ON refund.id = original.reversed_by_event_id
             WHERE original.status = 'reversed'
               AND refund.event_type = 'customer_refund'
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if ambiguous_refund is not None:
        raise RuntimeError("LEGACY_REFUND_REVERSED_EVENT_REQUIRES_REPAIR")

    incomplete = bind.execute(
        sa.text(
            """
            SELECT event.id
              FROM business_events AS event
             WHERE event.status IN ('posted', 'reversed')
               AND NOT EXISTS (
                   SELECT 1
                     FROM vouchers AS voucher
                    WHERE voucher.org_id = event.org_id
                      AND voucher.event_id = event.id
                      AND voucher.status IN ('posted', 'reversed')
               )
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if incomplete is not None:
        raise RuntimeError("FINAL_EVENT_VOUCHER_INVARIANT_VIOLATION")


def _assert_round3_downgrade_safe() -> None:
    """Do not discard immutable matching, guard, or R3 payroll history."""

    bind = op.get_bind()
    for table_name in ("payroll_tax_year_guards", "bank_transaction_matches"):
        if bind.execute(sa.text(f"SELECT 1 FROM {table_name} LIMIT 1")).scalar() is not None:
            raise RuntimeError(
                "PAYROLL_DOWNGRADE_UNSAFE: round-3 payroll data exists; preserve accounting history"
            )


def _migrate_legacy_bank_matches() -> None:
    """Turn every unambiguous legacy current pointer into append-only history."""

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT id, org_id, matched_event_id, imported_at
              FROM bank_transactions
             WHERE matched_event_id IS NOT NULL
            """
        )
    ).mappings()
    for row in rows:
        bind.execute(
            sa.text(
                """
                INSERT INTO bank_transaction_matches
                    (id, org_id, bank_transaction_id, event_id, created_at)
                VALUES (:id, :org_id, :bank_transaction_id, :event_id, :created_at)
                """
            ),
            {
                "id": str(uuid.uuid4()),
                "org_id": str(row["org_id"]),
                "bank_transaction_id": str(row["id"]),
                "event_id": str(row["matched_event_id"]),
                "created_at": row["imported_at"],
            },
        )


def upgrade() -> None:
    # Every preflight runs before DDL so a rejected database remains on 0003.
    _assert_open_item_state_consistency()
    _assert_legacy_bank_match_organizations()
    _assert_legacy_final_event_state()

    with op.batch_alter_table("business_events") as batch_op:
        batch_op.drop_constraint("ck_event_status", type_="check")
        batch_op.create_check_constraint(
            "ck_event_status",
            "status IN ('draft','posted','needs_information','rejected','reversed')",
        )

    with op.batch_alter_table("payroll_batches") as batch_op:
        batch_op.drop_constraint("ck_payroll_batch_status", type_="check")
        batch_op.create_check_constraint(
            "ck_payroll_batch_status",
            "status IN ('draft','calculated','posted','reversed','superseded')",
        )

    with op.batch_alter_table("bank_transactions") as batch_op:
        batch_op.create_unique_constraint("uq_bank_transaction_org_id", ["org_id", "id"])

    with op.batch_alter_table("payroll_withholding_payment_allocations") as batch_op:
        batch_op.add_column(sa.Column("reversed_by_event_id", sa.Uuid(), nullable=True))
        batch_op.create_foreign_key(
            "fk_withholding_payment_org_reversal_event",
            "business_events",
            ["org_id", "reversed_by_event_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )

    with op.batch_alter_table("payroll_event_links") as batch_op:
        batch_op.add_column(sa.Column("source_open_item_id", sa.Uuid(), nullable=True))
        batch_op.drop_constraint("uq_payroll_event_link", type_="unique")
        batch_op.create_foreign_key(
            "fk_payroll_event_link_org_source_open_item",
            "open_items",
            ["org_id", "source_open_item_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )

    op.create_table(
        "payroll_tax_year_guards",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("tax_year BETWEEN 1900 AND 9999", name="ck_payroll_tax_guard_year"),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_payroll_tax_guard_org_employee",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "employee_id", "tax_year"),
    )

    op.create_table(
        "bank_transaction_matches",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("bank_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("invalidated_by_event_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(invalidated_by_event_id IS NULL AND invalidated_at IS NULL) OR "
            "(invalidated_by_event_id IS NOT NULL AND invalidated_at IS NOT NULL)",
            name="ck_bank_match_invalidation_pair",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "bank_transaction_id"],
            ["bank_transactions.org_id", "bank_transactions.id"],
            name="fk_bank_match_org_transaction",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_bank_match_org_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "invalidated_by_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_bank_match_org_invalidation_event",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "bank_transaction_id", "event_id", name="uq_bank_match_event"),
    )
    op.create_index("ix_bank_transaction_matches_org_id", "bank_transaction_matches", ["org_id"])
    op.create_index(
        "ix_bank_transaction_matches_bank_transaction_id",
        "bank_transaction_matches",
        ["bank_transaction_id"],
    )
    op.create_index("ix_bank_transaction_matches_event_id", "bank_transaction_matches", ["event_id"])
    op.create_index(
        "uq_bank_transaction_match_current",
        "bank_transaction_matches",
        ["org_id", "bank_transaction_id"],
        unique=True,
        postgresql_where=sa.text("invalidated_by_event_id IS NULL"),
        sqlite_where=sa.text("invalidated_by_event_id IS NULL"),
    )

    op.create_index(
        "uq_payroll_event_link_without_source",
        "payroll_event_links",
        ["org_id", "event_id", "link_kind"],
        unique=True,
        postgresql_where=sa.text("source_payment_event_id IS NULL AND source_open_item_id IS NULL"),
        sqlite_where=sa.text("source_payment_event_id IS NULL AND source_open_item_id IS NULL"),
    )
    op.create_index(
        "uq_payroll_event_link_salary_source",
        "payroll_event_links",
        ["org_id", "event_id", "link_kind", "source_open_item_id"],
        unique=True,
        postgresql_where=sa.text("source_payment_event_id IS NULL AND source_open_item_id IS NOT NULL"),
        sqlite_where=sa.text("source_payment_event_id IS NULL AND source_open_item_id IS NOT NULL"),
    )
    op.create_index(
        "uq_payroll_event_link_payment_source",
        "payroll_event_links",
        [
            "org_id",
            "event_id",
            "link_kind",
            "source_payment_event_id",
            "source_open_item_id",
        ],
        unique=True,
        postgresql_where=sa.text("source_payment_event_id IS NOT NULL AND source_open_item_id IS NOT NULL"),
        sqlite_where=sa.text("source_payment_event_id IS NOT NULL AND source_open_item_id IS NOT NULL"),
    )
    op.create_index(
        "uq_payroll_event_link_reversal_source",
        "payroll_event_links",
        ["org_id", "event_id", "link_kind", "source_payment_event_id"],
        unique=True,
        postgresql_where=sa.text("source_payment_event_id IS NOT NULL AND source_open_item_id IS NULL"),
        sqlite_where=sa.text("source_payment_event_id IS NOT NULL AND source_open_item_id IS NULL"),
    )

    _migrate_legacy_bank_matches()

    if op.get_bind().dialect.name == "postgresql":
        _install_postgresql_invariants()


def _install_postgresql_invariants() -> None:
    """Install deferred PostgreSQL assertions after all R3 structures exist."""

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_final_business_event(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE reversal business_events%ROWTYPE;
        DECLARE final_voucher_id uuid;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN
                RETURN;
            END IF;
            IF target_event.event_type NOT IN (
                'service_cash_sale', 'service_credit_sale', 'service_fulfillment',
                'customer_receipt', 'customer_advance', 'customer_refund',
                'expense_cash', 'expense_payable', 'supplier_payment',
                'employee_reimbursement', 'owner_loan_received',
                'owner_contribution_received', 'owner_repayment', 'bank_fee',
                'internal_transfer', 'tax_payment', 'tax_relief',
                'salary_payment', 'social_insurance_payment',
                'housing_fund_payment', 'individual_income_tax_payment',
                'payroll_accrual', 'reversal'
            ) THEN
                RAISE EXCEPTION 'final business event has an unsupported event type';
            END IF;
            SELECT voucher.id INTO final_voucher_id
              FROM vouchers AS voucher
             WHERE voucher.org_id = target_event.org_id
               AND voucher.event_id = target_event.id
               AND voucher.status IN ('posted', 'reversed');
            IF final_voucher_id IS NULL THEN
                RAISE EXCEPTION 'final business event requires a complete final voucher';
            END IF;
            PERFORM finance_assert_final_voucher(final_voucher_id);
            IF target_event.status = 'posted' AND target_event.reversed_by_event_id IS NOT NULL THEN
                RAISE EXCEPTION 'posted business event cannot name a reversal event';
            END IF;
            IF target_event.status = 'reversed' THEN
                IF target_event.reversed_by_event_id IS NULL THEN
                    RAISE EXCEPTION 'reversed business event requires an explicit reversal event';
                END IF;
                SELECT * INTO reversal
                  FROM business_events
                 WHERE id = target_event.reversed_by_event_id
                   AND org_id = target_event.org_id;
                IF NOT FOUND
                   OR reversal.id = target_event.id
                   OR reversal.status <> 'posted'
                   OR reversal.event_type NOT IN ('reversal', 'payroll_accrual')
                   OR reversal.facts ->> 'original_event_id' <> target_event.id::text THEN
                    RAISE EXCEPTION 'reversed business event requires a same-organization formal reversal';
                END IF;
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_final_business_event_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' AND NEW.status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final business events must be created as draft';
            END IF;
            IF TG_OP = 'DELETE' AND OLD.status IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'final business events are immutable; create a reversal';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status IN ('posted', 'reversed') THEN
                IF OLD.status = 'posted'
                   AND NEW.status = 'reversed'
                   AND NEW.reversed_by_event_id IS NOT NULL
                   AND (to_jsonb(NEW) - ARRAY['status', 'reversed_by_event_id'])
                       = (to_jsonb(OLD) - ARRAY['status', 'reversed_by_event_id']) THEN
                    RETURN NEW;
                END IF;
                RAISE EXCEPTION 'final business events are immutable; create a reversal';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'draft'
               AND NEW.status = 'posted'
               AND (to_jsonb(NEW) - 'status') <> (to_jsonb(OLD) - 'status') THEN
                RAISE EXCEPTION 'draft business event facts must be complete before finalization';
            END IF;
            IF TG_OP = 'UPDATE' AND OLD.status = 'draft' AND NEW.status = 'reversed' THEN
                RAISE EXCEPTION 'business event cannot transition directly from draft to reversed';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_business_event()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_final_business_event(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_business_event_from_voucher()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_business_event(OLD.event_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_business_event(NEW.event_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS immutable_final_business_event ON business_events;
        CREATE TRIGGER immutable_final_business_event
        BEFORE INSERT OR UPDATE OR DELETE ON business_events
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_business_event_mutation();

        CREATE CONSTRAINT TRIGGER final_business_event_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON business_events
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_business_event();

        CREATE CONSTRAINT TRIGGER final_business_event_voucher_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON vouchers
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_business_event_from_voucher();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_payroll_withholding_batch(target_batch_id uuid)
        RETURNS void AS $$
        DECLARE target_batch payroll_batches%ROWTYPE;
        DECLARE invalid_snapshot boolean;
        DECLARE invalid_totals boolean;
        DECLARE invalid_entitlements boolean;
        BEGIN
            SELECT * INTO target_batch FROM payroll_batches WHERE id = target_batch_id;
            IF NOT FOUND
               OR target_batch.status <> 'posted'
               OR target_batch.reversal_of_batch_id IS NOT NULL THEN
                RETURN;
            END IF;
            SELECT EXISTS (
                SELECT 1
                  FROM payroll_lines AS line
                 WHERE line.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
                   AND (
                       jsonb_typeof(line.employee_social_insurance_items::jsonb) <> 'object'
                       OR jsonb_typeof(line.employee_housing_fund_items::jsonb) <> 'object'
                       OR EXISTS (
                           SELECT 1 FROM jsonb_each_text(line.employee_social_insurance_items::jsonb)
                            WHERE value !~ '^[0-9]+$'
                       )
                       OR EXISTS (
                           SELECT 1 FROM jsonb_each_text(line.employee_housing_fund_items::jsonb)
                            WHERE value !~ '^[0-9]+$'
                       )
                   )
            ) INTO invalid_snapshot;
            IF invalid_snapshot THEN
                RAISE EXCEPTION 'payroll withholding snapshot must contain nonnegative integer insurance items';
            END IF;

            SELECT EXISTS (
                SELECT 1
                  FROM payroll_lines AS line
                 WHERE line.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
                   AND (
                       line.employee_social_insurance_fen <> COALESCE((
                           SELECT SUM(value::bigint)
                             FROM jsonb_each_text(line.employee_social_insurance_items::jsonb)
                       ), 0)
                       OR line.employee_housing_fund_fen <> COALESCE((
                           SELECT SUM(value::bigint)
                             FROM jsonb_each_text(line.employee_housing_fund_items::jsonb)
                       ), 0)
                   )
            ) INTO invalid_totals;
            IF invalid_totals THEN
                RAISE EXCEPTION 'payroll withholding totals do not match immutable payroll line items';
            END IF;

            WITH expected AS (
                SELECT line.id AS payroll_line_id,
                       'employee_social_insurance'::varchar AS contribution_group,
                       component.key::varchar AS insurance_kind,
                       component.value::bigint AS amount_fen
                  FROM payroll_lines AS line
                  CROSS JOIN LATERAL jsonb_each_text(line.employee_social_insurance_items::jsonb)
                       AS component(key, value)
                 WHERE line.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
                   AND component.value::bigint > 0
                UNION ALL
                SELECT line.id,
                       'employee_housing_fund'::varchar,
                       component.key::varchar,
                       component.value::bigint
                  FROM payroll_lines AS line
                  CROSS JOIN LATERAL jsonb_each_text(line.employee_housing_fund_items::jsonb)
                       AS component(key, value)
                 WHERE line.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
                   AND component.value::bigint > 0
                UNION ALL
                SELECT line.id,
                       'individual_income_tax'::varchar,
                       'individual_income_tax'::varchar,
                       line.individual_income_tax_fen
                  FROM payroll_lines AS line
                 WHERE line.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
                   AND line.individual_income_tax_fen > 0
            ), actual AS (
                SELECT entitlement.payroll_line_id,
                       entitlement.contribution_group,
                       entitlement.insurance_kind,
                       entitlement.amount_fen
                  FROM payroll_withholding_entitlements AS entitlement
                  JOIN payroll_lines AS line
                    ON line.id = entitlement.payroll_line_id
                   AND line.org_id = entitlement.org_id
                 WHERE entitlement.org_id = target_batch.org_id
                   AND line.payroll_batch_id = target_batch.id
            )
            SELECT EXISTS (
                SELECT 1
                  FROM expected
                  FULL OUTER JOIN actual
                    USING (payroll_line_id, contribution_group, insurance_kind)
                 WHERE expected.amount_fen IS DISTINCT FROM actual.amount_fen
            ) INTO invalid_entitlements;
            IF invalid_entitlements THEN
                RAISE EXCEPTION 'final payroll withholding entitlements must exactly match payroll line facts';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_withholding_batch()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_payroll_withholding_batch(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_withholding_batch_from_line()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_payroll_withholding_batch(OLD.payroll_batch_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_payroll_withholding_batch(NEW.payroll_batch_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_withholding_batch_from_entitlement()
        RETURNS trigger AS $$
        DECLARE affected_line_id uuid;
        DECLARE affected_batch_id uuid;
        BEGIN
            affected_line_id := COALESCE(NEW.payroll_line_id, OLD.payroll_line_id);
            SELECT payroll_batch_id INTO affected_batch_id
              FROM payroll_lines WHERE id = affected_line_id;
            PERFORM finance_assert_payroll_withholding_batch(affected_batch_id);
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_final_payroll_withholding_entitlement_mutation()
        RETURNS trigger AS $$
        DECLARE target_line_id uuid;
        BEGIN
            target_line_id := COALESCE(NEW.payroll_line_id, OLD.payroll_line_id);
            IF EXISTS (
                SELECT 1
                  FROM payroll_lines AS line
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE line.id = target_line_id
                   AND batch.status IN ('posted', 'reversed', 'superseded')
            ) THEN
                RAISE EXCEPTION 'final payroll withholding entitlements are immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_payroll_withholding_payment(target_allocation_id uuid)
        RETURNS void AS $$
        DECLARE allocation payroll_withholding_payment_allocations%ROWTYPE;
        DECLARE source_batch payroll_batches%ROWTYPE;
        DECLARE payment business_events%ROWTYPE;
        DECLARE reversal business_events%ROWTYPE;
        DECLARE active_total bigint;
        BEGIN
            SELECT * INTO allocation
              FROM payroll_withholding_payment_allocations
             WHERE id = target_allocation_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT batch.* INTO source_batch
              FROM payroll_withholding_entitlements AS entitlement
              JOIN payroll_lines AS line
                ON line.id = entitlement.payroll_line_id AND line.org_id = entitlement.org_id
              JOIN payroll_batches AS batch
                ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
             WHERE entitlement.id = allocation.entitlement_id
               AND entitlement.org_id = allocation.org_id;
            IF NOT FOUND OR source_batch.status <> 'posted'
               OR source_batch.reversal_of_batch_id IS NOT NULL THEN
                RAISE EXCEPTION 'withholding payment allocation requires a final non-reversal payroll line';
            END IF;
            SELECT * INTO payment
              FROM business_events
             WHERE id = allocation.payment_event_id AND org_id = allocation.org_id;
            -- The allocation remains an immutable audit record after its
            -- salary payment has been formally reversed.  A non-final source
            -- is invalid, but ``reversed`` is the valid terminal state here.
            IF NOT FOUND
               OR payment.status NOT IN ('posted', 'reversed')
               OR payment.event_type <> 'salary_payment' THEN
                RAISE EXCEPTION 'withholding payment allocation requires a final salary payment event';
            END IF;
            IF allocation.reversed IS FALSE AND allocation.reversed_by_event_id IS NOT NULL THEN
                RAISE EXCEPTION 'active withholding allocation cannot name a reversal';
            END IF;
            IF allocation.reversed IS TRUE THEN
                IF allocation.reversed_by_event_id IS NULL THEN
                    RAISE EXCEPTION 'reversed withholding allocation requires a formal reversal event';
                END IF;
                SELECT * INTO reversal
                  FROM business_events
                 WHERE id = allocation.reversed_by_event_id AND org_id = allocation.org_id;
                IF NOT FOUND OR reversal.status <> 'posted'
                   OR reversal.facts ->> 'original_event_id' <> allocation.payment_event_id::text THEN
                    RAISE EXCEPTION 'withholding allocation reversal must reference its salary payment';
                END IF;
            END IF;
            SELECT COALESCE(SUM(amount_fen) FILTER (WHERE reversed IS FALSE), 0)
              INTO active_total
              FROM payroll_withholding_payment_allocations
             WHERE entitlement_id = allocation.entitlement_id
               AND org_id = allocation.org_id;
            IF active_total > (
                SELECT amount_fen FROM payroll_withholding_entitlements
                 WHERE id = allocation.entitlement_id AND org_id = allocation.org_id
            ) THEN
                RAISE EXCEPTION 'payroll withholding allocation exceeds per-kind entitlement';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_withholding_payment_r3()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_payroll_withholding_payment(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_payroll_withholding_payment_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'payroll withholding payment allocations are append-only';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                NEW.id <> OLD.id
                OR NEW.org_id <> OLD.org_id
                OR NEW.entitlement_id <> OLD.entitlement_id
                OR NEW.payment_event_id <> OLD.payment_event_id
                OR NEW.amount_fen <> OLD.amount_fen
                OR NEW.created_at <> OLD.created_at
                OR OLD.reversed IS TRUE
                OR NEW.reversed IS FALSE
                OR NEW.reversed_by_event_id IS NULL
            ) THEN
                RAISE EXCEPTION 'payroll withholding payment allocations are immutable except formal reversal';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_final_payroll_withholding_entitlement
        BEFORE INSERT OR UPDATE OR DELETE ON payroll_withholding_entitlements
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_payroll_withholding_entitlement_mutation();

        CREATE TRIGGER immutable_payroll_withholding_payment_allocation
        BEFORE UPDATE OR DELETE ON payroll_withholding_payment_allocations
        FOR EACH ROW EXECUTE FUNCTION finance_block_payroll_withholding_payment_mutation();

        CREATE CONSTRAINT TRIGGER payroll_withholding_batch_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_batches
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_withholding_batch();

        CREATE CONSTRAINT TRIGGER payroll_withholding_line_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_withholding_batch_from_line();

        CREATE CONSTRAINT TRIGGER payroll_withholding_entitlement_shape_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_withholding_entitlements
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_withholding_batch_from_entitlement();

        CREATE CONSTRAINT TRIGGER payroll_withholding_payment_r3_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_withholding_payment_allocations
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_withholding_payment_r3();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_payroll_tax_state_slot(target_slot_id uuid)
        RETURNS void AS $$
        DECLARE slot payroll_tax_state_slots%ROWTYPE;
        DECLARE regular payroll_batches%ROWTYPE;
        DECLARE final_batch payroll_batches%ROWTYPE;
        BEGIN
            SELECT * INTO slot FROM payroll_tax_state_slots WHERE id = target_slot_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT * INTO regular
              FROM payroll_batches
             WHERE id = slot.regular_batch_id AND org_id = slot.org_id;
            IF NOT FOUND
               OR regular.batch_kind <> 'regular'
               OR regular.status <> 'posted'
               OR EXTRACT(YEAR FROM regular.payment_date)::integer <> slot.tax_year
               OR EXTRACT(MONTH FROM regular.payment_date)::integer <> slot.tax_month
               OR NOT EXISTS (
                   SELECT 1 FROM payroll_lines
                    WHERE org_id = slot.org_id
                      AND payroll_batch_id = regular.id
                      AND employee_id = slot.employee_id
               ) THEN
                RAISE EXCEPTION 'tax state slot requires a final same-employee regular payroll batch';
            END IF;
            SELECT * INTO final_batch
              FROM payroll_batches
             WHERE id = slot.final_batch_id AND org_id = slot.org_id;
            IF NOT FOUND
               OR final_batch.status <> 'posted'
               OR EXTRACT(YEAR FROM final_batch.payment_date)::integer <> slot.tax_year
               OR EXTRACT(MONTH FROM final_batch.payment_date)::integer <> slot.tax_month THEN
                RAISE EXCEPTION 'tax state slot final batch must be final in the same payment month';
            END IF;
            IF final_batch.id = regular.id THEN
                RETURN;
            END IF;
            IF final_batch.batch_kind <> 'annual_bonus'
               OR final_batch.tax_method <> 'combined'
               OR NOT EXISTS (
                   SELECT 1 FROM payroll_lines
                    WHERE org_id = slot.org_id
                      AND payroll_batch_id = final_batch.id
                      AND employee_id = slot.employee_id
                      AND regular_payroll_batch_id = regular.id
               ) THEN
                RAISE EXCEPTION 'tax state slot final batch must be its employee combined annual bonus';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_deleted_payroll_tax_state_slot(
            target_regular_batch_id uuid,
            target_final_batch_id uuid,
            target_org_id uuid
        )
        RETURNS void AS $$
        DECLARE regular payroll_batches%ROWTYPE;
        BEGIN
            IF target_regular_batch_id <> target_final_batch_id THEN
                RAISE EXCEPTION 'combined payroll tax state must be restored before removal';
            END IF;
            SELECT * INTO regular
              FROM payroll_batches
             WHERE id = target_regular_batch_id AND org_id = target_org_id;
            IF NOT FOUND OR regular.status <> 'reversed' THEN
                RAISE EXCEPTION 'payroll tax state slot can only be removed with its reversed regular payroll';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_payroll_tax_state_slot_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' AND NEW.regular_batch_id <> NEW.final_batch_id THEN
                RAISE EXCEPTION 'new payroll tax state slot must start with its regular batch final';
            END IF;
            IF TG_OP = 'UPDATE' THEN
                IF NEW.org_id <> OLD.org_id
                   OR NEW.employee_id <> OLD.employee_id
                   OR NEW.tax_year <> OLD.tax_year
                   OR NEW.tax_month <> OLD.tax_month
                   OR NEW.regular_batch_id <> OLD.regular_batch_id THEN
                    RAISE EXCEPTION 'payroll tax state identity and regular batch are immutable';
                END IF;
                IF NOT (
                    (OLD.final_batch_id = OLD.regular_batch_id
                     AND NEW.final_batch_id <> NEW.regular_batch_id)
                    OR (OLD.final_batch_id <> OLD.regular_batch_id
                        AND NEW.final_batch_id = NEW.regular_batch_id)
                ) THEN
                    RAISE EXCEPTION 'payroll tax state final batch has an illegal transition';
                END IF;
            END IF;
            IF TG_OP = 'DELETE' AND OLD.final_batch_id <> OLD.regular_batch_id THEN
                RAISE EXCEPTION 'combined payroll tax state must be restored before removal';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_tax_state_slot()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                PERFORM finance_assert_deleted_payroll_tax_state_slot(
                    OLD.regular_batch_id, OLD.final_batch_id, OLD.org_id
                );
            ELSE
                PERFORM finance_assert_payroll_tax_state_slot(NEW.id);
                IF TG_OP = 'UPDATE'
                   AND OLD.final_batch_id <> OLD.regular_batch_id
                   AND NEW.final_batch_id = NEW.regular_batch_id
                   AND NOT EXISTS (
                       SELECT 1 FROM payroll_batches
                        WHERE id = OLD.final_batch_id
                          AND org_id = OLD.org_id
                          AND status = 'reversed'
                   ) THEN
                    RAISE EXCEPTION 'combined payroll tax state can only restore after its bonus reversal';
                END IF;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_payroll_tax_state_slot
        BEFORE INSERT OR UPDATE OR DELETE ON payroll_tax_state_slots
        FOR EACH ROW EXECUTE FUNCTION finance_block_payroll_tax_state_slot_mutation();

        CREATE CONSTRAINT TRIGGER payroll_tax_state_slot_shape_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_tax_state_slots
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_tax_state_slot();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_payroll_event_link_mutation()
        RETURNS trigger AS $$
        DECLARE target_event_id uuid;
        DECLARE target_status varchar;
        BEGIN
            target_event_id := COALESCE(NEW.event_id, OLD.event_id);
            SELECT status INTO target_status FROM business_events WHERE id = target_event_id;
            IF target_status <> 'draft' THEN
                RAISE EXCEPTION 'payroll event links are immutable after event finalization';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_payroll_event_link(target_link_id uuid)
        RETURNS void AS $$
        DECLARE link payroll_event_links%ROWTYPE;
        DECLARE linked_event business_events%ROWTYPE;
        DECLARE source_event business_events%ROWTYPE;
        DECLARE source_item open_items%ROWTYPE;
        BEGIN
            SELECT * INTO link FROM payroll_event_links WHERE id = target_link_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT * INTO linked_event
              FROM business_events WHERE id = link.event_id AND org_id = link.org_id;
            IF NOT FOUND OR linked_event.status NOT IN ('posted', 'reversed') THEN
                RETURN;
            END IF;
            IF link.link_kind = 'payroll_accrual' THEN
                IF linked_event.event_type <> 'payroll_accrual'
                   OR link.source_payment_event_id IS NOT NULL
                   OR link.source_open_item_id IS NOT NULL
                   OR NOT EXISTS (
                       SELECT 1 FROM payroll_batches
                        WHERE id = link.payroll_batch_id
                          AND org_id = link.org_id
                          AND business_event_id = link.event_id
                   ) THEN
                    RAISE EXCEPTION 'payroll accrual event link has an invalid shape';
                END IF;
                RETURN;
            END IF;
            IF link.link_kind = 'salary_payment' THEN
                IF linked_event.event_type <> 'salary_payment'
                   OR link.source_payment_event_id IS NOT NULL
                   OR link.source_open_item_id IS NULL THEN
                    RAISE EXCEPTION 'salary payment event link has an invalid shape';
                END IF;
                SELECT * INTO source_item
                  FROM open_items
                 WHERE id = link.source_open_item_id AND org_id = link.org_id;
                IF NOT FOUND OR source_item.item_type <> 'payable'
                   OR source_item.payable_category <> 'salary'
                   OR NOT EXISTS (
                       SELECT 1 FROM settlements
                        WHERE org_id = link.org_id
                          AND payment_event_id = link.event_id
                          AND open_item_id = source_item.id
                          AND reversed IS FALSE
                   )
                   OR NOT EXISTS (
                       SELECT 1
                         FROM payroll_event_links AS accrual
                        WHERE accrual.org_id = link.org_id
                          AND accrual.event_id = source_item.source_event_id
                          AND accrual.payroll_batch_id = link.payroll_batch_id
                          AND accrual.link_kind = 'payroll_accrual'
                   ) THEN
                    RAISE EXCEPTION 'salary payment event link must prove its payroll salary settlement';
                END IF;
                RETURN;
            END IF;
            IF link.link_kind = 'statutory_payment' THEN
                IF linked_event.event_type NOT IN (
                    'social_insurance_payment', 'housing_fund_payment', 'individual_income_tax_payment'
                ) OR link.source_payment_event_id IS NULL OR link.source_open_item_id IS NULL THEN
                    RAISE EXCEPTION 'statutory payment event link has an invalid shape';
                END IF;
                SELECT * INTO source_event
                  FROM business_events
                 WHERE id = link.source_payment_event_id AND org_id = link.org_id;
                SELECT * INTO source_item
                  FROM open_items
                 WHERE id = link.source_open_item_id AND org_id = link.org_id;
                IF source_event.id IS NULL OR source_item.id IS NULL
                   OR source_event.event_type <> 'salary_payment'
                   OR source_item.source_event_id <> source_event.id
                   OR NOT EXISTS (
                       SELECT 1 FROM settlements
                        WHERE org_id = link.org_id
                          AND payment_event_id = link.event_id
                          AND open_item_id = source_item.id
                          AND reversed IS FALSE
                   ) THEN
                    RAISE EXCEPTION 'statutory payment event link must prove its source payroll payment';
                END IF;
                RETURN;
            END IF;
            IF link.link_kind = 'reversal' THEN
                IF linked_event.event_type NOT IN ('reversal', 'payroll_accrual')
                   OR link.source_payment_event_id IS NULL
                   OR link.source_open_item_id IS NOT NULL THEN
                    RAISE EXCEPTION 'payroll reversal event link has an invalid shape';
                END IF;
                SELECT * INTO source_event
                  FROM business_events
                 WHERE id = link.source_payment_event_id AND org_id = link.org_id;
                IF source_event.id IS NULL
                   OR linked_event.facts ->> 'original_event_id' <> source_event.id::text THEN
                    RAISE EXCEPTION 'payroll reversal event link must name its reversed event';
                END IF;
                RETURN;
            END IF;
            RAISE EXCEPTION 'payroll event link has an unsupported link kind';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_event_link()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_payroll_event_link(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_final_payroll_event_link
        BEFORE INSERT OR UPDATE OR DELETE ON payroll_event_links
        FOR EACH ROW EXECUTE FUNCTION finance_block_payroll_event_link_mutation();

        CREATE CONSTRAINT TRIGGER payroll_event_link_shape_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_event_links
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_event_link();

        CREATE OR REPLACE FUNCTION finance_block_payroll_batch_evidence_mutation()
        RETURNS trigger AS $$
        DECLARE target_batch_id uuid;
        DECLARE target_status varchar;
        BEGIN
            target_batch_id := COALESCE(NEW.payroll_batch_id, OLD.payroll_batch_id);
            SELECT status INTO target_status
              FROM payroll_batches
             WHERE id = target_batch_id AND org_id = COALESCE(NEW.org_id, OLD.org_id);
            IF target_status <> 'draft' THEN
                RAISE EXCEPTION 'payroll batch evidence is immutable once the draft is sealed';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_sealed_payroll_batch_evidence
        BEFORE INSERT OR UPDATE OR DELETE ON payroll_batch_evidence
        FOR EACH ROW EXECUTE FUNCTION finance_block_payroll_batch_evidence_mutation();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_bank_transaction_match(target_match_id uuid)
        RETURNS void AS $$
        DECLARE match_row bank_transaction_matches%ROWTYPE;
        DECLARE matched_event business_events%ROWTYPE;
        DECLARE invalidation business_events%ROWTYPE;
        DECLARE legacy_pointer uuid;
        BEGIN
            SELECT * INTO match_row FROM bank_transaction_matches WHERE id = target_match_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT * INTO matched_event
              FROM business_events
             WHERE id = match_row.event_id AND org_id = match_row.org_id;
            -- A historical edge remains valid after its original event has
            -- been formally reversed.  It must still point to a final event;
            -- the separate invalidation check below proves the reversal link.
            IF NOT FOUND OR matched_event.status NOT IN ('posted', 'reversed') THEN
                RAISE EXCEPTION 'bank match requires a final same-organization business event';
            END IF;
            IF match_row.invalidated_by_event_id IS NOT NULL THEN
                SELECT * INTO invalidation
                  FROM business_events
                 WHERE id = match_row.invalidated_by_event_id AND org_id = match_row.org_id;
                IF NOT FOUND OR invalidation.status <> 'posted'
                   OR invalidation.facts ->> 'original_event_id' <> match_row.event_id::text THEN
                    RAISE EXCEPTION 'bank match invalidation must be a formal reversal of its event';
                END IF;
            END IF;
            SELECT matched_event_id INTO legacy_pointer
              FROM bank_transactions
             WHERE id = match_row.bank_transaction_id AND org_id = match_row.org_id;
            IF match_row.invalidated_by_event_id IS NULL AND legacy_pointer <> match_row.event_id THEN
                RAISE EXCEPTION 'bank transaction current match must mirror immutable match history';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_bank_transaction_current_match(target_transaction_id uuid)
        RETURNS void AS $$
        DECLARE transaction_row bank_transactions%ROWTYPE;
        DECLARE active_match_id uuid;
        BEGIN
            SELECT * INTO transaction_row FROM bank_transactions WHERE id = target_transaction_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT id INTO active_match_id
              FROM bank_transaction_matches
             WHERE org_id = transaction_row.org_id
               AND bank_transaction_id = transaction_row.id
               AND invalidated_by_event_id IS NULL;
            IF transaction_row.matched_event_id IS DISTINCT FROM (
                SELECT event_id FROM bank_transaction_matches WHERE id = active_match_id
            ) THEN
                RAISE EXCEPTION 'bank transaction pointer must mirror one current immutable match';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_bank_transaction_match_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'bank transaction match history is append-only';
            END IF;
            IF TG_OP = 'UPDATE' AND (
                NEW.id <> OLD.id
                OR NEW.org_id <> OLD.org_id
                OR NEW.bank_transaction_id <> OLD.bank_transaction_id
                OR NEW.event_id <> OLD.event_id
                OR NEW.created_at <> OLD.created_at
                OR OLD.invalidated_by_event_id IS NOT NULL
                OR NEW.invalidated_by_event_id IS NULL
                OR NEW.invalidated_at IS NULL
            ) THEN
                RAISE EXCEPTION 'bank transaction match is immutable except formal invalidation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_bank_transaction_match()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_bank_transaction_match(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_bank_transaction_current_match()
        RETURNS trigger AS $$
        BEGIN
            PERFORM finance_assert_bank_transaction_current_match(COALESCE(NEW.id, OLD.id));
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_bank_transaction_match
        BEFORE UPDATE OR DELETE ON bank_transaction_matches
        FOR EACH ROW EXECUTE FUNCTION finance_block_bank_transaction_match_mutation();

        CREATE CONSTRAINT TRIGGER bank_transaction_match_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON bank_transaction_matches
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_bank_transaction_match();

        CREATE CONSTRAINT TRIGGER bank_transaction_current_match_invariant_deferred
        AFTER INSERT OR UPDATE ON bank_transactions
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_bank_transaction_current_match();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_payroll_version_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'payroll version rows are immutable; create a successor';
            END IF;
            IF TG_OP = 'UPDATE' AND to_jsonb(NEW) <> to_jsonb(OLD) THEN
                RAISE EXCEPTION 'payroll version rows are immutable; create a successor';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE TRIGGER immutable_employee_payroll_profile_version
        BEFORE UPDATE OR DELETE ON employee_payroll_profile_versions
        FOR EACH ROW EXECUTE FUNCTION finance_block_payroll_version_mutation();
        CREATE TRIGGER immutable_payroll_policy_version
        BEFORE UPDATE OR DELETE ON payroll_policy_versions
        FOR EACH ROW EXECUTE FUNCTION finance_block_payroll_version_mutation();
        CREATE TRIGGER immutable_payroll_opening_state
        BEFORE UPDATE OR DELETE ON payroll_opening_states
        FOR EACH ROW EXECUTE FUNCTION finance_block_payroll_version_mutation();

        -- Force the inherited deferred checks across existing rows.  The query
        -- intentionally runs after all functions are installed, so a head
        -- database cannot retain pre-existing status pollution merely because
        -- no row happened to be updated after the migration.
        SELECT finance_assert_open_item_settlement(id) FROM open_items;
        """
    )


def downgrade() -> None:
    _assert_round3_downgrade_safe()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for statement in (
            "DROP TRIGGER IF EXISTS immutable_payroll_opening_state ON payroll_opening_states",
            "DROP TRIGGER IF EXISTS immutable_payroll_policy_version ON payroll_policy_versions",
            "DROP TRIGGER IF EXISTS immutable_employee_payroll_profile_version ON employee_payroll_profile_versions",
            "DROP FUNCTION IF EXISTS finance_block_payroll_version_mutation()",
            "DROP TRIGGER IF EXISTS bank_transaction_current_match_invariant_deferred ON bank_transactions",
            "DROP TRIGGER IF EXISTS bank_transaction_match_invariant_deferred ON bank_transaction_matches",
            "DROP TRIGGER IF EXISTS immutable_bank_transaction_match ON bank_transaction_matches",
            "DROP FUNCTION IF EXISTS finance_validate_bank_transaction_current_match()",
            "DROP FUNCTION IF EXISTS finance_validate_bank_transaction_match()",
            "DROP FUNCTION IF EXISTS finance_block_bank_transaction_match_mutation()",
            "DROP FUNCTION IF EXISTS finance_assert_bank_transaction_current_match(uuid)",
            "DROP FUNCTION IF EXISTS finance_assert_bank_transaction_match(uuid)",
            "DROP TRIGGER IF EXISTS immutable_sealed_payroll_batch_evidence ON payroll_batch_evidence",
            "DROP FUNCTION IF EXISTS finance_block_payroll_batch_evidence_mutation()",
            "DROP TRIGGER IF EXISTS payroll_event_link_shape_deferred ON payroll_event_links",
            "DROP TRIGGER IF EXISTS immutable_final_payroll_event_link ON payroll_event_links",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_event_link()",
            "DROP FUNCTION IF EXISTS finance_assert_payroll_event_link(uuid)",
            "DROP FUNCTION IF EXISTS finance_block_payroll_event_link_mutation()",
            "DROP TRIGGER IF EXISTS payroll_tax_state_slot_shape_deferred ON payroll_tax_state_slots",
            "DROP TRIGGER IF EXISTS immutable_payroll_tax_state_slot ON payroll_tax_state_slots",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_tax_state_slot()",
            "DROP FUNCTION IF EXISTS finance_block_payroll_tax_state_slot_mutation()",
            "DROP FUNCTION IF EXISTS finance_assert_deleted_payroll_tax_state_slot(uuid, uuid, uuid)",
            "DROP FUNCTION IF EXISTS finance_assert_payroll_tax_state_slot(uuid)",
            "DROP TRIGGER IF EXISTS payroll_withholding_payment_r3_invariant_deferred ON payroll_withholding_payment_allocations",
            "DROP TRIGGER IF EXISTS payroll_withholding_entitlement_shape_deferred ON payroll_withholding_entitlements",
            "DROP TRIGGER IF EXISTS payroll_withholding_line_invariant_deferred ON payroll_lines",
            "DROP TRIGGER IF EXISTS payroll_withholding_batch_invariant_deferred ON payroll_batches",
            "DROP TRIGGER IF EXISTS immutable_payroll_withholding_payment_allocation ON payroll_withholding_payment_allocations",
            "DROP TRIGGER IF EXISTS immutable_final_payroll_withholding_entitlement ON payroll_withholding_entitlements",
            "DROP FUNCTION IF EXISTS finance_block_payroll_withholding_payment_mutation()",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_withholding_payment_r3()",
            "DROP FUNCTION IF EXISTS finance_assert_payroll_withholding_payment(uuid)",
            "DROP FUNCTION IF EXISTS finance_block_final_payroll_withholding_entitlement_mutation()",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_withholding_batch_from_entitlement()",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_withholding_batch_from_line()",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_withholding_batch()",
            "DROP FUNCTION IF EXISTS finance_assert_payroll_withholding_batch(uuid)",
            "DROP TRIGGER IF EXISTS final_business_event_voucher_invariant_deferred ON vouchers",
            "DROP TRIGGER IF EXISTS final_business_event_invariant_deferred ON business_events",
            "DROP TRIGGER IF EXISTS immutable_final_business_event ON business_events",
            "DROP FUNCTION IF EXISTS finance_validate_final_business_event_from_voucher()",
            "DROP FUNCTION IF EXISTS finance_validate_final_business_event()",
            "DROP FUNCTION IF EXISTS finance_assert_final_business_event(uuid)",
        ):
            op.execute(statement)
        # Restore the R2 trigger contract that this revision replaced.
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
                            SELECT 1 FROM business_events AS reversal
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

    for index_name in (
        "uq_payroll_event_link_reversal_source",
        "uq_payroll_event_link_payment_source",
        "uq_payroll_event_link_salary_source",
        "uq_payroll_event_link_without_source",
    ):
        op.drop_index(index_name, table_name="payroll_event_links")
    op.drop_index("uq_bank_transaction_match_current", table_name="bank_transaction_matches")
    op.drop_index("ix_bank_transaction_matches_event_id", table_name="bank_transaction_matches")
    op.drop_index(
        "ix_bank_transaction_matches_bank_transaction_id", table_name="bank_transaction_matches"
    )
    op.drop_index("ix_bank_transaction_matches_org_id", table_name="bank_transaction_matches")
    op.drop_table("bank_transaction_matches")
    op.drop_table("payroll_tax_year_guards")

    with op.batch_alter_table("payroll_event_links") as batch_op:
        batch_op.drop_constraint("fk_payroll_event_link_org_source_open_item", type_="foreignkey")
        batch_op.create_unique_constraint("uq_payroll_event_link", ["org_id", "event_id", "link_kind"])
        batch_op.drop_column("source_open_item_id")
    with op.batch_alter_table("payroll_withholding_payment_allocations") as batch_op:
        batch_op.drop_constraint("fk_withholding_payment_org_reversal_event", type_="foreignkey")
        batch_op.drop_column("reversed_by_event_id")
    with op.batch_alter_table("bank_transactions") as batch_op:
        batch_op.drop_constraint("uq_bank_transaction_org_id", type_="unique")
    with op.batch_alter_table("payroll_batches") as batch_op:
        batch_op.drop_constraint("ck_payroll_batch_status", type_="check")
        batch_op.create_check_constraint(
            "ck_payroll_batch_status",
            "status IN ('calculated','posted','reversed','superseded')",
        )
    with op.batch_alter_table("business_events") as batch_op:
        batch_op.drop_constraint("ck_event_status", type_="check")
        batch_op.create_check_constraint(
            "ck_event_status",
            "status IN ('posted','needs_information','rejected','reversed')",
        )
