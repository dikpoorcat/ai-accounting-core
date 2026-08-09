"""Close the fourth-round payroll relational-integrity gaps.

Revision ID: 0005_payroll_round4_integrity
Revises: 0004_payroll_round3_integrity
Create Date: 2026-08-10
"""

# ruff: noqa: E501

from __future__ import annotations

import importlib.util
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0005_payroll_round4_integrity"
down_revision = "0004_payroll_round3_integrity"
branch_labels = None
depends_on = None


def _assert_event_evidence_organizations() -> None:
    """Refuse historical evidence edges whose two parents disagree on organization."""

    polluted = op.get_bind().execute(
        sa.text(
            """
            SELECT edge.event_id
              FROM event_evidence AS edge
              JOIN business_events AS event ON event.id = edge.event_id
              JOIN evidence AS item ON item.id = edge.evidence_id
             WHERE event.org_id <> item.org_id
             LIMIT 1
            """
        )
    ).scalar_one_or_none()
    if polluted is not None:
        raise RuntimeError("EVENT_EVIDENCE_ORGANIZATION_INVARIANT_VIOLATION")


def _assert_round4_downgrade_safe() -> None:
    """Do not silently discard R3/R4 source, allocation, evidence, or bank facts."""

    bind = op.get_bind()
    unsafe_checks = (
        (
            "SELECT 1 FROM payroll_event_links WHERE source_open_item_id IS NOT NULL LIMIT 1",
            "PAYROLL_DOWNGRADE_UNSAFE: source open-item lineage exists",
        ),
        (
            "SELECT 1 FROM payroll_withholding_payment_allocations "
            "WHERE reversed_by_event_id IS NOT NULL LIMIT 1",
            "PAYROLL_DOWNGRADE_UNSAFE: withholding reversal lineage exists",
        ),
        (
            "SELECT 1 FROM payroll_tax_year_guards LIMIT 1",
            "PAYROLL_DOWNGRADE_UNSAFE: tax-year guards exist",
        ),
        (
            "SELECT 1 FROM bank_transaction_matches LIMIT 1",
            "PAYROLL_DOWNGRADE_UNSAFE: bank match history exists",
        ),
        (
            "SELECT 1 FROM event_evidence LIMIT 1",
            "PAYROLL_DOWNGRADE_UNSAFE: organization-bound event evidence exists",
        ),
    )
    for statement, message in unsafe_checks:
        if bind.execute(sa.text(statement)).scalar_one_or_none() is not None:
            raise RuntimeError(message)


def _upgrade_event_evidence() -> None:
    """Attach every evidence edge to its event organization without guessing."""

    with op.batch_alter_table("event_evidence") as batch_op:
        batch_op.add_column(sa.Column("org_id", sa.Uuid(), nullable=True))
        batch_op.add_column(
            sa.Column(
                "relation_kind",
                sa.String(length=30),
                nullable=False,
                server_default=sa.text("'supporting'"),
            )
        )
    op.execute(
        """
        UPDATE event_evidence
           SET org_id = (
               SELECT event.org_id
                 FROM business_events AS event
                WHERE event.id = event_evidence.event_id
           )
        """
    )
    with op.batch_alter_table("event_evidence") as batch_op:
        batch_op.alter_column("org_id", existing_type=sa.Uuid(), nullable=False)
        batch_op.create_foreign_key(
            "fk_event_evidence_org_event",
            "business_events",
            ["org_id", "event_id"],
            ["org_id", "id"],
            ondelete="CASCADE",
        )
        batch_op.create_foreign_key(
            "fk_event_evidence_org_evidence",
            "evidence",
            ["org_id", "evidence_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )
        batch_op.create_check_constraint(
            "ck_event_evidence_relation_kind",
            "relation_kind IN ('supporting','inherited','reversal_reason')",
        )
    op.create_index("ix_event_evidence_org_id", "event_evidence", ["org_id"])


def _backfill_determinable_payroll_event_link_sources() -> None:
    """Fill only the one legacy source edge that can be proved from settlement."""

    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            """
            SELECT link.id, link.org_id, link.event_id
              FROM payroll_event_links AS link
              JOIN business_events AS event
                ON event.id = link.event_id AND event.org_id = link.org_id
             WHERE event.status IN ('posted', 'reversed')
               AND link.link_kind = 'salary_payment'
               AND link.source_open_item_id IS NULL
            """
        )
    ).mappings()
    for row in rows:
        item_ids = list(
            bind.execute(
                sa.text(
                    """
                    SELECT settlement.open_item_id
                      FROM settlements AS settlement
                      JOIN open_items AS item
                        ON item.id = settlement.open_item_id
                       AND item.org_id = settlement.org_id
                     WHERE settlement.org_id = :org_id
                       AND settlement.payment_event_id = :event_id
                       AND settlement.reversed IS FALSE
                       AND item.item_type = 'payable'
                       AND item.payable_category = 'salary'
                    """
                ),
                {"org_id": row["org_id"], "event_id": row["event_id"]},
            ).scalars()
        )
        if len(item_ids) != 1:
            raise RuntimeError(
                "PAYROLL_EVENT_LINK_SOURCE_OPEN_ITEM_CANNOT_BE_DETERMINED"
            )
        bind.execute(
            sa.text(
                """
                UPDATE payroll_event_links
                   SET source_open_item_id = :item_id
                 WHERE id = :link_id
                """
            ),
            {"item_id": item_ids[0], "link_id": row["id"]},
        )


def upgrade() -> None:
    # This scan is deliberately before DDL.  An ambiguous cross-enterprise
    # edge has no deterministic organization to backfill, so the old revision
    # and all source rows must remain untouched.
    _assert_event_evidence_organizations()
    # 0004 correctly froze final links, but 0005 must repair the one
    # historically provable NULL source before it installs the stricter
    # complete-source contract.  The two 0004 triggers remain disabled only
    # until `_install_postgresql_round4_invariants` drops and replaces them;
    # re-enabling a PostgreSQL deferred trigger while its repair update is
    # pending is not legal.  A failed migration rolls this DDL back.
    is_postgresql = op.get_bind().dialect.name == "postgresql"
    if is_postgresql:
        op.execute(
            "ALTER TABLE payroll_event_links DISABLE TRIGGER "
            "immutable_final_payroll_event_link"
        )
        op.execute(
            "ALTER TABLE payroll_event_links DISABLE TRIGGER "
            "payroll_event_link_shape_deferred"
        )
    _backfill_determinable_payroll_event_link_sources()
    _upgrade_event_evidence()
    if op.get_bind().dialect.name == "postgresql":
        _install_postgresql_round4_invariants()


def _install_postgresql_round4_invariants() -> None:
    """Install the deferred, commit-point PostgreSQL assertions for round four."""

    # R3's immediate lineage triggers use a one-hop notion of supersession.
    # R4 deliberately allows an ancestor successor to extend into its own
    # range, then checks the complete recursive graph at commit.  Preserve the
    # old functions for downgrade, but remove their eager triggers here.
    op.execute(
        """
        DROP TRIGGER IF EXISTS payroll_profile_version_chain
          ON employee_payroll_profile_versions;
        DROP TRIGGER IF EXISTS payroll_policy_version_chain
          ON payroll_policy_versions;
        DROP TRIGGER IF EXISTS payroll_opening_state_version_chain
          ON payroll_opening_states;
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
               OR target_batch.status NOT IN ('posted', 'reversed', 'superseded')
               -- A superseded preview may never have been confirmed.  It has
               -- no final business event and therefore no immutable payroll
               -- facts to validate; confirmed superseded batches remain in
               -- scope through their event id.
               OR target_batch.business_event_id IS NULL THEN
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

        CREATE OR REPLACE FUNCTION finance_validate_payroll_withholding_batch_from_entitlement()
        RETURNS trigger AS $$
        DECLARE old_batch_id uuid;
        DECLARE new_batch_id uuid;
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                SELECT payroll_batch_id INTO old_batch_id
                  FROM payroll_lines
                 WHERE org_id = OLD.org_id AND id = OLD.payroll_line_id;
                PERFORM finance_assert_payroll_withholding_batch(old_batch_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                SELECT payroll_batch_id INTO new_batch_id
                  FROM payroll_lines
                 WHERE org_id = NEW.org_id AND id = NEW.payroll_line_id;
                PERFORM finance_assert_payroll_withholding_batch(new_batch_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_final_payroll_withholding_entitlement_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
                SELECT 1
                  FROM payroll_lines AS line
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE line.org_id = OLD.org_id
                   AND line.id = OLD.payroll_line_id
                   AND batch.status IN ('posted', 'reversed', 'superseded')
            ) THEN
                RAISE EXCEPTION 'final payroll withholding entitlements are immutable';
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
                SELECT 1
                  FROM payroll_lines AS line
                  JOIN payroll_batches AS batch
                    ON batch.id = line.payroll_batch_id AND batch.org_id = line.org_id
                 WHERE line.org_id = NEW.org_id
                   AND line.id = NEW.payroll_line_id
                   AND batch.status IN ('posted', 'reversed', 'superseded')
            ) THEN
                RAISE EXCEPTION 'final payroll withholding entitlements are immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_payroll_batch_tax_state(target_batch_id uuid)
        RETURNS void AS $$
        DECLARE target_batch payroll_batches%ROWTYPE;
        DECLARE invalid_slots boolean;
        BEGIN
            SELECT * INTO target_batch FROM payroll_batches WHERE id = target_batch_id;
            IF NOT FOUND
               OR target_batch.status <> 'posted'
               OR target_batch.reversal_of_batch_id IS NOT NULL THEN
                RETURN;
            END IF;
            IF target_batch.batch_kind = 'regular' THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM payroll_lines AS line
                     WHERE line.org_id = target_batch.org_id
                       AND line.payroll_batch_id = target_batch.id
                       AND 1 <> (
                           SELECT COUNT(*) FROM payroll_tax_state_slots AS slot
                            WHERE slot.org_id = target_batch.org_id
                              AND slot.employee_id = line.employee_id
                              AND slot.tax_year = EXTRACT(YEAR FROM target_batch.payment_date)::integer
                              AND slot.tax_month = EXTRACT(MONTH FROM target_batch.payment_date)::integer
                              AND slot.regular_batch_id = target_batch.id
                       )
                ) INTO invalid_slots;
                IF invalid_slots THEN
                    RAISE EXCEPTION 'final regular payroll requires exactly one tax state slot per employee';
                END IF;
                RETURN;
            END IF;
            IF target_batch.tax_method = 'combined' THEN
                SELECT EXISTS (
                    SELECT 1
                      FROM payroll_lines AS line
                     WHERE line.org_id = target_batch.org_id
                       AND line.payroll_batch_id = target_batch.id
                       AND (
                           line.regular_payroll_batch_id IS NULL
                           OR 1 <> (
                               SELECT COUNT(*) FROM payroll_tax_state_slots AS slot
                                WHERE slot.org_id = target_batch.org_id
                                  AND slot.employee_id = line.employee_id
                                  AND slot.tax_year = EXTRACT(YEAR FROM target_batch.payment_date)::integer
                                  AND slot.tax_month = EXTRACT(MONTH FROM target_batch.payment_date)::integer
                                  AND slot.regular_batch_id = line.regular_payroll_batch_id
                                  AND slot.final_batch_id = target_batch.id
                           )
                       )
                ) INTO invalid_slots;
                IF invalid_slots THEN
                    RAISE EXCEPTION 'final combined annual bonus requires exactly one employee tax state slot';
                END IF;
                RETURN;
            END IF;
            IF target_batch.tax_method = 'separate' AND EXISTS (
                SELECT 1 FROM payroll_tax_state_slots AS slot
                 WHERE slot.org_id = target_batch.org_id
                   AND slot.final_batch_id = target_batch.id
            ) THEN
                RAISE EXCEPTION 'separate annual bonus must not occupy a combined tax state slot';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_batch_tax_state_from_batch()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_batch_tax_state_from_line()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(OLD.payroll_batch_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(NEW.payroll_batch_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_batch_tax_state_from_slot()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(OLD.regular_batch_id);
                PERFORM finance_assert_payroll_batch_tax_state(OLD.final_batch_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_payroll_batch_tax_state(NEW.regular_batch_id);
                PERFORM finance_assert_payroll_batch_tax_state(NEW.final_batch_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS payroll_withholding_entitlement_shape_deferred
            ON payroll_withholding_entitlements;
        CREATE CONSTRAINT TRIGGER payroll_withholding_entitlement_shape_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_withholding_entitlements
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_withholding_batch_from_entitlement();

        DROP TRIGGER IF EXISTS immutable_final_payroll_withholding_entitlement
            ON payroll_withholding_entitlements;
        CREATE TRIGGER immutable_final_payroll_withholding_entitlement
        BEFORE INSERT OR UPDATE OR DELETE ON payroll_withholding_entitlements
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_payroll_withholding_entitlement_mutation();

        CREATE CONSTRAINT TRIGGER payroll_batch_tax_state_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_batches
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_batch_tax_state_from_batch();

        CREATE CONSTRAINT TRIGGER payroll_line_tax_state_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_lines
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_batch_tax_state_from_line();

        CREATE CONSTRAINT TRIGGER payroll_tax_slot_batch_coverage_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_tax_state_slots
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_batch_tax_state_from_slot();
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
            IF NOT FOUND THEN
                RAISE EXCEPTION 'BANK_MATCH_CURRENT_EVENT_NOT_POSTED';
            END IF;
            SELECT matched_event_id INTO legacy_pointer
              FROM bank_transactions
             WHERE id = match_row.bank_transaction_id AND org_id = match_row.org_id;
            IF match_row.invalidated_by_event_id IS NULL THEN
                IF matched_event.status <> 'posted' THEN
                    RAISE EXCEPTION 'BANK_MATCH_CURRENT_EVENT_NOT_POSTED';
                END IF;
                IF legacy_pointer IS DISTINCT FROM match_row.event_id THEN
                    RAISE EXCEPTION 'BANK_TRANSACTION_POINTER_MIRROR_VIOLATION';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO invalidation
              FROM business_events
             WHERE id = match_row.invalidated_by_event_id AND org_id = match_row.org_id;
            IF matched_event.status <> 'reversed'
               OR NOT FOUND
               OR invalidation.status <> 'posted'
               OR invalidation.event_type <> 'reversal'
               OR invalidation.facts ->> 'original_event_id' <> match_row.event_id::text THEN
                RAISE EXCEPTION 'BANK_MATCH_INVALIDATION_NOT_CANONICAL_REVERSAL';
            END IF;
            IF legacy_pointer = match_row.event_id THEN
                RAISE EXCEPTION 'BANK_TRANSACTION_POINTER_MIRROR_VIOLATION';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_bank_transaction_current_match(target_transaction_id uuid)
        RETURNS void AS $$
        DECLARE target_bank bank_transactions%ROWTYPE;
        DECLARE active_event_id uuid;
        DECLARE active_count integer;
        BEGIN
            SELECT * INTO target_bank FROM bank_transactions WHERE id = target_transaction_id;
            IF NOT FOUND THEN
                RETURN;
            END IF;
            SELECT COUNT(*), (array_agg(event_id))[1]
              INTO active_count, active_event_id
              FROM bank_transaction_matches
             WHERE org_id = target_bank.org_id
               AND bank_transaction_id = target_bank.id
               AND invalidated_by_event_id IS NULL;
            IF active_count > 1
               OR target_bank.matched_event_id IS DISTINCT FROM active_event_id THEN
                RAISE EXCEPTION 'BANK_TRANSACTION_POINTER_MIRROR_VIOLATION';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_bank_transaction_match()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_bank_transaction_match(OLD.id);
                PERFORM finance_assert_bank_transaction_current_match(OLD.bank_transaction_id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_bank_transaction_match(NEW.id);
                PERFORM finance_assert_bank_transaction_current_match(NEW.bank_transaction_id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS bank_transaction_match_invariant_deferred
            ON bank_transaction_matches;
        CREATE CONSTRAINT TRIGGER bank_transaction_match_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON bank_transaction_matches
        DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_bank_transaction_match();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_exact_reversal_voucher(
            target_event_id uuid,
            original_event_id uuid
        ) RETURNS void AS $$
        DECLARE target_voucher vouchers%ROWTYPE;
        DECLARE original_voucher vouchers%ROWTYPE;
        BEGIN
            SELECT * INTO target_voucher FROM vouchers WHERE event_id = target_event_id;
            SELECT * INTO original_voucher FROM vouchers WHERE event_id = original_event_id;
            IF NOT FOUND OR target_voucher.org_id <> original_voucher.org_id
               OR target_voucher.reversal_of_voucher_id IS DISTINCT FROM original_voucher.id THEN
                RAISE EXCEPTION 'reversal voucher must link to the same-organization original voucher';
            END IF;
            IF EXISTS (
                (SELECT account_id, counterparty_id, debit_fen, credit_fen
                   FROM voucher_lines WHERE voucher_id = target_voucher.id)
                EXCEPT ALL
                (SELECT account_id, counterparty_id, credit_fen, debit_fen
                   FROM voucher_lines WHERE voucher_id = original_voucher.id)
            ) OR EXISTS (
                (SELECT account_id, counterparty_id, credit_fen, debit_fen
                   FROM voucher_lines WHERE voucher_id = original_voucher.id)
                EXCEPT ALL
                (SELECT account_id, counterparty_id, debit_fen, credit_fen
                   FROM voucher_lines WHERE voucher_id = target_voucher.id)
            ) THEN
                RAISE EXCEPTION 'reversal voucher lines must exactly reverse the original voucher';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_final_business_event(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE original_event business_events%ROWTYPE;
        DECLARE reversal_event business_events%ROWTYPE;
        DECLARE target_batch payroll_batches%ROWTYPE;
        DECLARE original_batch payroll_batches%ROWTYPE;
        DECLARE final_voucher_id uuid;
        DECLARE original_event_id uuid;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type NOT IN (
                'service_cash_sale', 'service_credit_sale', 'service_fulfillment',
                'customer_receipt', 'customer_advance', 'customer_refund',
                'expense_cash', 'expense_payable', 'supplier_payment',
                'employee_reimbursement', 'owner_loan_received',
                'owner_contribution_received', 'owner_repayment', 'bank_fee',
                'internal_transfer', 'tax_payment', 'tax_relief',
                'salary_payment', 'social_insurance_payment', 'housing_fund_payment',
                'individual_income_tax_payment', 'payroll_accrual', 'reversal'
            ) THEN RAISE EXCEPTION 'final business event has an unsupported event type'; END IF;
            SELECT voucher.id INTO final_voucher_id FROM vouchers AS voucher
             WHERE voucher.org_id = target_event.org_id AND voucher.event_id = target_event.id
               AND voucher.status IN ('posted', 'reversed');
            IF final_voucher_id IS NULL THEN
                RAISE EXCEPTION 'final business event requires a complete final voucher';
            END IF;
            PERFORM finance_assert_final_voucher(final_voucher_id);
            IF target_event.status = 'reversed' THEN
                IF target_event.reversed_by_event_id IS NULL THEN
                    RAISE EXCEPTION 'reversed business event requires an explicit reversal event';
                END IF;
                SELECT * INTO reversal_event FROM business_events
                 WHERE id = target_event.reversed_by_event_id AND org_id = target_event.org_id;
                IF NOT FOUND OR reversal_event.status <> 'posted'
                   OR reversal_event.facts ->> 'original_event_id' <> target_event.id::text
                   OR (target_event.event_type = 'payroll_accrual'
                       AND reversal_event.event_type <> 'payroll_accrual')
                   OR (target_event.event_type <> 'payroll_accrual'
                       AND reversal_event.event_type <> 'reversal') THEN
                    RAISE EXCEPTION 'reversed business event requires a canonical same-organization reversal';
                END IF;
            ELSIF target_event.reversed_by_event_id IS NOT NULL THEN
                RAISE EXCEPTION 'posted business event cannot name a reversal event';
            END IF;
            IF target_event.facts::jsonb ? 'original_event_id' THEN
                original_event_id := (target_event.facts ->> 'original_event_id')::uuid;
                SELECT * INTO original_event FROM business_events
                 WHERE id = original_event_id AND org_id = target_event.org_id;
                IF NOT FOUND OR original_event.id = target_event.id
                   OR target_event.status <> 'posted'
                   OR original_event.status <> 'reversed'
                   OR original_event.reversed_by_event_id <> target_event.id THEN
                    RAISE EXCEPTION 'reversal event must bind one reversed same-organization original event';
                END IF;
                PERFORM finance_assert_exact_reversal_voucher(target_event.id, original_event.id);
                IF target_event.event_type = 'reversal' THEN
                    IF original_event.event_type = 'payroll_accrual' THEN
                        RAISE EXCEPTION 'ordinary reversal cannot reverse payroll accrual';
                    END IF;
                ELSIF target_event.event_type = 'payroll_accrual' THEN
                    SELECT * INTO target_batch FROM payroll_batches
                     WHERE org_id = target_event.org_id AND business_event_id = target_event.id
                       AND reversal_of_batch_id IS NOT NULL;
                    SELECT * INTO original_batch FROM payroll_batches
                     WHERE org_id = target_event.org_id AND business_event_id = original_event.id;
                    IF target_batch.id IS NULL OR original_batch.id IS NULL
                       OR original_event.event_type <> 'payroll_accrual'
                       OR target_batch.reversal_of_batch_id <> original_batch.id
                       OR original_batch.status <> 'reversed' THEN
                        RAISE EXCEPTION 'payroll accrual reversal requires its exact payroll reversal batch';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'only canonical reversal events may name an original event';
                END IF;
            ELSIF target_event.event_type = 'payroll_accrual' THEN
                SELECT * INTO target_batch FROM payroll_batches
                 WHERE org_id = target_event.org_id AND business_event_id = target_event.id;
                IF NOT FOUND OR target_batch.reversal_of_batch_id IS NOT NULL
                   OR NOT EXISTS (SELECT 1 FROM payroll_event_links
                                  WHERE org_id = target_event.org_id AND event_id = target_event.id
                                    AND payroll_batch_id = target_batch.id
                                    AND link_kind = 'payroll_accrual') THEN
                    RAISE EXCEPTION 'normal payroll accrual requires its exact payroll batch source edge';
                END IF;
            ELSIF target_event.event_type = 'reversal' THEN
                RAISE EXCEPTION 'reversal event requires an original event id';
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_business_event_from_voucher()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_business_event(OLD.event_id);
                PERFORM finance_assert_final_business_event(event_id)
                  FROM vouchers WHERE id = OLD.reversal_of_voucher_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_business_event(NEW.event_id);
                PERFORM finance_assert_final_business_event(event_id)
                  FROM vouchers WHERE id = NEW.reversal_of_voucher_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_business_event_from_voucher_line()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_business_event(event_id)
                  FROM vouchers WHERE id = OLD.voucher_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_business_event(event_id)
                  FROM vouchers WHERE id = NEW.voucher_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER final_business_event_voucher_line_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON voucher_lines DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_business_event_from_voucher_line();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_payroll_event_link_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
                SELECT 1 FROM business_events
                 WHERE id = OLD.event_id AND org_id = OLD.org_id
                   AND status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'payroll event links are immutable after event finalization';
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
                SELECT 1 FROM business_events
                 WHERE id = NEW.event_id AND org_id = NEW.org_id
                   AND status IN ('posted', 'reversed')
            ) THEN
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
        DECLARE claim_batch payroll_batches%ROWTYPE;
        BEGIN
            SELECT * INTO link FROM payroll_event_links WHERE id = target_link_id;
            IF NOT FOUND THEN RETURN; END IF;
            SELECT * INTO linked_event FROM business_events
             WHERE id = link.event_id AND org_id = link.org_id;
            IF NOT FOUND OR linked_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            -- A reversed payment keeps its immutable historical source edge
            -- while its settlements are themselves reversed.  The formal
            -- reversal relationship is checked by the final-event invariant;
            -- re-requiring an active settlement here would make that legal
            -- reversal impossible to commit.
            IF linked_event.status = 'reversed' THEN RETURN; END IF;
            IF link.link_kind = 'payroll_accrual' THEN
                IF linked_event.event_type <> 'payroll_accrual'
                   OR link.source_payment_event_id IS NOT NULL
                   OR link.source_open_item_id IS NOT NULL
                   OR NOT EXISTS (SELECT 1 FROM payroll_batches
                                   WHERE id = link.payroll_batch_id AND org_id = link.org_id
                                     AND business_event_id = linked_event.id) THEN
                    RAISE EXCEPTION 'payroll accrual event link has an invalid shape';
                END IF;
                RETURN;
            END IF;
            IF link.link_kind = 'salary_payment' THEN
                IF linked_event.event_type <> 'salary_payment'
                   OR link.source_payment_event_id IS NOT NULL
                   OR link.source_open_item_id IS NULL
                   OR NOT EXISTS (
                       SELECT 1 FROM open_items AS item
                        WHERE item.id = link.source_open_item_id AND item.org_id = link.org_id
                          AND item.item_type = 'payable' AND item.payable_category = 'salary'
                          AND EXISTS (SELECT 1 FROM settlements AS settlement
                                      WHERE settlement.org_id = link.org_id
                                        AND settlement.open_item_id = item.id
                                        AND settlement.payment_event_id = linked_event.id
                                        AND settlement.reversed IS FALSE)
                          AND EXISTS (SELECT 1 FROM payroll_event_links AS accrual
                                      WHERE accrual.org_id = link.org_id
                                        AND accrual.event_id = item.source_event_id
                                        AND accrual.payroll_batch_id = link.payroll_batch_id
                                        AND accrual.link_kind = 'payroll_accrual')
                   ) THEN
                    RAISE EXCEPTION 'salary payment event link must prove its payroll salary settlement';
                END IF;
                RETURN;
            END IF;
            IF link.link_kind = 'statutory_payment' THEN
                IF linked_event.event_type NOT IN ('social_insurance_payment','housing_fund_payment','individual_income_tax_payment')
                   OR link.source_payment_event_id IS NULL OR link.source_open_item_id IS NULL THEN
                    RAISE EXCEPTION 'statutory payment event link has an invalid shape';
                END IF;
                SELECT * INTO source_event FROM business_events
                 WHERE id = link.source_payment_event_id AND org_id = link.org_id;
                SELECT * INTO source_item FROM open_items
                 WHERE id = link.source_open_item_id AND org_id = link.org_id;
                IF NOT FOUND OR source_event.id IS NULL
                   OR source_item.item_type <> 'payable'
                   OR source_item.source_event_id <> source_event.id
                   OR NOT EXISTS (SELECT 1 FROM settlements AS settlement
                                  WHERE settlement.org_id = link.org_id
                                    AND settlement.open_item_id = source_item.id
                                    AND settlement.payment_event_id = linked_event.id
                                    AND settlement.reversed IS FALSE) THEN
                    RAISE EXCEPTION 'statutory payment event link must prove its settled source open item';
                END IF;
                IF (linked_event.event_type = 'social_insurance_payment'
                    AND source_item.payable_category NOT IN ('employer_social','withheld_employee_social'))
                   OR (linked_event.event_type = 'housing_fund_payment'
                    AND source_item.payable_category NOT IN ('employer_housing','withheld_employee_housing'))
                   OR (linked_event.event_type = 'individual_income_tax_payment'
                    AND source_item.payable_category <> 'individual_income_tax') THEN
                    RAISE EXCEPTION 'statutory payment event link has an incompatible payable category';
                END IF;
                SELECT * INTO claim_batch FROM payroll_batches
                 WHERE id = link.payroll_batch_id AND org_id = link.org_id;
                IF NOT FOUND OR source_item.payable_agency_code IS DISTINCT FROM
                   (claim_batch.policy_snapshot::jsonb -> 'parameters' -> 'payment_targets' ->
                    CASE WHEN linked_event.event_type = 'social_insurance_payment' THEN 'social_insurance'
                         WHEN linked_event.event_type = 'housing_fund_payment' THEN 'housing_fund'
                         ELSE 'individual_income_tax' END ->> 'agency_code')
                   OR NOT EXISTS (
                       SELECT 1 FROM counterparties AS agency
                        WHERE agency.id = source_item.counterparty_id AND agency.org_id = link.org_id
                          AND agency.external_ref = (claim_batch.policy_snapshot::jsonb -> 'parameters' -> 'payment_targets' ->
                              CASE WHEN linked_event.event_type = 'social_insurance_payment' THEN 'social_insurance'
                                   WHEN linked_event.event_type = 'housing_fund_payment' THEN 'housing_fund'
                                   ELSE 'individual_income_tax' END ->> 'agency_code')
                   ) THEN
                    RAISE EXCEPTION 'statutory payment source does not match its frozen policy agency';
                END IF;
                IF source_event.event_type = 'salary_payment' THEN
                    IF source_item.payable_category IN ('employer_social','employer_housing')
                       OR NOT EXISTS (
                           SELECT 1 FROM payroll_event_links AS salary
                            JOIN open_items AS salary_item
                              ON salary_item.id = salary.source_open_item_id
                             AND salary_item.org_id = salary.org_id
                           WHERE salary.org_id = link.org_id
                             AND salary.event_id = source_event.id
                             AND salary.link_kind = 'salary_payment'
                             AND salary.payroll_batch_id = link.payroll_batch_id
                             AND EXISTS (SELECT 1 FROM settlements AS salary_settlement
                                          WHERE salary_settlement.org_id = link.org_id
                                            AND salary_settlement.open_item_id = salary_item.id
                                            AND salary_settlement.payment_event_id = source_event.id
                                            AND salary_settlement.reversed IS FALSE)
                       ) THEN
                        RAISE EXCEPTION 'statutory payment salary source must prove the same payroll batch';
                    END IF;
                    IF NOT EXISTS (
                        SELECT 1
                          FROM payroll_withholding_payment_allocations AS allocation
                          JOIN payroll_withholding_entitlements AS entitlement
                            ON entitlement.id = allocation.entitlement_id
                           AND entitlement.org_id = allocation.org_id
                          JOIN payroll_lines AS line
                            ON line.id = entitlement.payroll_line_id
                           AND line.org_id = entitlement.org_id
                         WHERE allocation.org_id = link.org_id
                           AND allocation.payment_event_id = source_event.id
                           AND allocation.reversed IS FALSE
                           AND line.payroll_batch_id = link.payroll_batch_id
                           AND ((source_item.payable_category = 'withheld_employee_social'
                                 AND entitlement.contribution_group = 'employee_social_insurance'
                                 AND entitlement.insurance_kind = source_item.insurance_kind)
                             OR (source_item.payable_category = 'withheld_employee_housing'
                                 AND entitlement.contribution_group = 'employee_housing_fund'
                                 AND entitlement.insurance_kind = source_item.insurance_kind)
                             OR (source_item.payable_category = 'individual_income_tax'
                                 AND entitlement.contribution_group = 'individual_income_tax'
                                 AND entitlement.insurance_kind = 'individual_income_tax'))
                    ) THEN
                        RAISE EXCEPTION 'statutory payment withholding source lacks its employee and insurance entitlement';
                    END IF;
                ELSIF source_event.event_type = 'payroll_accrual' THEN
                    IF source_item.payable_category NOT IN ('employer_social','employer_housing')
                       OR NOT EXISTS (
                           SELECT 1 FROM payroll_event_links AS accrual
                            WHERE accrual.org_id = link.org_id
                              AND accrual.event_id = source_event.id
                              AND accrual.payroll_batch_id = link.payroll_batch_id
                              AND accrual.link_kind = 'payroll_accrual'
                       ) THEN
                        RAISE EXCEPTION 'statutory payment employer source must prove the claimed payroll batch';
                    END IF;
                    IF (source_item.payable_category = 'employer_social' AND NOT EXISTS (
                            SELECT 1 FROM payroll_lines AS line
                             CROSS JOIN LATERAL jsonb_each_text(line.employer_social_insurance_items::jsonb) AS part(kind, amount)
                             WHERE line.org_id = link.org_id AND line.payroll_batch_id = link.payroll_batch_id
                               AND part.kind = source_item.insurance_kind
                             GROUP BY part.kind HAVING SUM(part.amount::bigint) = source_item.original_amount_fen
                        )) OR (source_item.payable_category = 'employer_housing' AND NOT EXISTS (
                            SELECT 1 FROM payroll_lines AS line
                             CROSS JOIN LATERAL jsonb_each_text(line.employer_housing_fund_items::jsonb) AS part(kind, amount)
                             WHERE line.org_id = link.org_id AND line.payroll_batch_id = link.payroll_batch_id
                               AND part.kind = source_item.insurance_kind
                             GROUP BY part.kind HAVING SUM(part.amount::bigint) = source_item.original_amount_fen
                        )) THEN
                        RAISE EXCEPTION 'statutory payment employer source lacks its batch insurance fact';
                    END IF;
                ELSE
                    RAISE EXCEPTION 'statutory payment has an unsupported source event';
                END IF;
                RETURN;
            END IF;
            IF link.link_kind = 'reversal' THEN
                IF linked_event.event_type NOT IN ('reversal','payroll_accrual')
                   OR link.source_payment_event_id IS NULL OR link.source_open_item_id IS NOT NULL
                   OR NOT EXISTS (SELECT 1 FROM business_events AS original
                                  WHERE original.id = link.source_payment_event_id
                                    AND original.org_id = link.org_id
                                    AND linked_event.facts ->> 'original_event_id' = original.id::text) THEN
                    RAISE EXCEPTION 'payroll reversal event link has an invalid shape';
                END IF;
                RETURN;
            END IF;
            RAISE EXCEPTION 'payroll event link has an unsupported link kind';
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_event_link()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN PERFORM finance_assert_payroll_event_link(OLD.id); END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN PERFORM finance_assert_payroll_event_link(NEW.id); END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_event_links_from_event()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_payroll_event_links(OLD.id);
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_payroll_event_links(NEW.id);
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_final_payroll_event_links(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE expected_count integer;
        DECLARE actual_count integer;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            -- A formal reversal preserves the original immutable edges but
            -- reverses their settlements.  The reversal event's own posted
            -- edge is checked below; the original no longer has active
            -- settlements to cover once it is reversed.
            IF target_event.status = 'reversed' THEN RETURN; END IF;
            -- The event-level cover test below catches omitted edges.  Re-run the
            -- per-edge proof here too, so a malformed edge cannot be masked by
            -- constraint-trigger execution order at COMMIT.
            PERFORM finance_assert_payroll_event_link(id) FROM payroll_event_links
             WHERE org_id = target_event.org_id AND event_id = target_event.id;
            IF target_event.event_type = 'payroll_accrual' THEN
                SELECT COUNT(*) INTO actual_count FROM payroll_event_links
                 WHERE org_id = target_event.org_id AND event_id = target_event.id
                   AND link_kind = CASE WHEN target_event.facts::jsonb ? 'original_event_id'
                                    THEN 'reversal' ELSE 'payroll_accrual' END;
                IF actual_count <> 1 THEN
                    RAISE EXCEPTION 'final payroll accrual requires exactly one normalized source edge';
                END IF;
            ELSIF target_event.event_type = 'salary_payment' THEN
                SELECT COUNT(*) INTO expected_count FROM settlements AS settlement
                  JOIN open_items AS item ON item.id = settlement.open_item_id
                   AND item.org_id = settlement.org_id
                 WHERE settlement.org_id = target_event.org_id
                   AND settlement.payment_event_id = target_event.id
                   AND settlement.reversed IS FALSE AND item.payable_category = 'salary';
                SELECT COUNT(*) INTO actual_count FROM payroll_event_links
                 WHERE org_id = target_event.org_id AND event_id = target_event.id
                   AND link_kind = 'salary_payment';
                IF expected_count <> actual_count THEN
                    RAISE EXCEPTION 'final salary payment source edges must exactly cover settled salary items';
                END IF;
            ELSIF target_event.event_type IN ('social_insurance_payment','housing_fund_payment','individual_income_tax_payment') THEN
                SELECT COUNT(*) INTO expected_count FROM settlements AS settlement
                  JOIN open_items AS item ON item.id = settlement.open_item_id
                   AND item.org_id = settlement.org_id
                 WHERE settlement.org_id = target_event.org_id
                   AND settlement.payment_event_id = target_event.id
                   AND settlement.reversed IS FALSE
                   AND ((target_event.event_type = 'social_insurance_payment'
                         AND item.payable_category IN ('employer_social','withheld_employee_social'))
                     OR (target_event.event_type = 'housing_fund_payment'
                         AND item.payable_category IN ('employer_housing','withheld_employee_housing'))
                     OR (target_event.event_type = 'individual_income_tax_payment'
                         AND item.payable_category = 'individual_income_tax'));
                SELECT COUNT(*) INTO actual_count FROM payroll_event_links
                 WHERE org_id = target_event.org_id AND event_id = target_event.id
                   AND link_kind = 'statutory_payment';
                IF expected_count <> actual_count THEN
                    RAISE EXCEPTION 'final statutory payment source edges must exactly cover settled statutory items';
                END IF;
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_block_payroll_batch_evidence_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
                SELECT 1 FROM payroll_batches
                 WHERE id = OLD.payroll_batch_id AND org_id = OLD.org_id AND status <> 'draft'
            ) THEN RAISE EXCEPTION 'payroll batch evidence is immutable once the draft is sealed'; END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
                SELECT 1 FROM payroll_batches
                 WHERE id = NEW.payroll_batch_id AND org_id = NEW.org_id AND status <> 'draft'
            ) THEN RAISE EXCEPTION 'payroll batch evidence is immutable once the draft is sealed'; END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        DROP TRIGGER IF EXISTS immutable_final_payroll_event_link ON payroll_event_links;
        CREATE TRIGGER immutable_final_payroll_event_link BEFORE INSERT OR UPDATE OR DELETE
          ON payroll_event_links FOR EACH ROW EXECUTE FUNCTION finance_block_payroll_event_link_mutation();
        DROP TRIGGER IF EXISTS payroll_event_link_shape_deferred ON payroll_event_links;
        CREATE CONSTRAINT TRIGGER payroll_event_link_shape_deferred AFTER INSERT OR UPDATE OR DELETE
          ON payroll_event_links DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
          EXECUTE FUNCTION finance_validate_payroll_event_link();
        CREATE CONSTRAINT TRIGGER payroll_event_link_event_shape_deferred AFTER INSERT OR UPDATE OR DELETE
          ON business_events DEFERRABLE INITIALLY DEFERRED FOR EACH ROW
          EXECUTE FUNCTION finance_validate_payroll_event_links_from_event();
        DROP TRIGGER IF EXISTS immutable_sealed_payroll_batch_evidence ON payroll_batch_evidence;
        CREATE TRIGGER immutable_sealed_payroll_batch_evidence BEFORE INSERT OR UPDATE OR DELETE
          ON payroll_batch_evidence FOR EACH ROW EXECUTE FUNCTION finance_block_payroll_batch_evidence_mutation();

        CREATE OR REPLACE FUNCTION finance_block_final_payroll_source_open_item_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (
                SELECT 1 FROM payroll_event_links AS link
                  JOIN business_events AS event ON event.id = link.event_id AND event.org_id = link.org_id
                 WHERE link.org_id = OLD.org_id AND link.source_open_item_id = OLD.id
                   AND event.status IN ('posted', 'reversed')
            ) AND (
                NEW.org_id IS DISTINCT FROM OLD.org_id
                OR NEW.counterparty_id IS DISTINCT FROM OLD.counterparty_id
                OR NEW.source_event_id IS DISTINCT FROM OLD.source_event_id
                OR NEW.item_type IS DISTINCT FROM OLD.item_type
                OR NEW.original_amount_fen IS DISTINCT FROM OLD.original_amount_fen
                OR NEW.payable_category IS DISTINCT FROM OLD.payable_category
                OR NEW.payable_agency_code IS DISTINCT FROM OLD.payable_agency_code
                OR NEW.insurance_kind IS DISTINCT FROM OLD.insurance_kind
            ) THEN
                RAISE EXCEPTION 'final payroll source open item identity is immutable';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER immutable_final_payroll_source_open_item BEFORE UPDATE ON open_items
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_payroll_source_open_item_mutation();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_block_final_event_evidence_mutation()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') AND EXISTS (
                SELECT 1 FROM business_events
                 WHERE id = OLD.event_id AND org_id = OLD.org_id
                   AND status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'final event evidence is immutable';
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') AND EXISTS (
                SELECT 1 FROM business_events
                 WHERE id = NEW.event_id AND org_id = NEW.org_id
                   AND status IN ('posted', 'reversed')
            ) THEN
                RAISE EXCEPTION 'final event evidence is immutable';
            END IF;
            RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_final_event_evidence(target_event_id uuid)
        RETURNS void AS $$
        DECLARE target_event business_events%ROWTYPE;
        DECLARE target_batch payroll_batches%ROWTYPE;
        BEGIN
            SELECT * INTO target_event FROM business_events WHERE id = target_event_id;
            IF NOT FOUND OR target_event.status NOT IN ('posted', 'reversed') THEN RETURN; END IF;
            IF target_event.event_type <> 'payroll_accrual' THEN
                IF EXISTS (SELECT 1 FROM event_evidence
                            WHERE event_id = target_event.id
                              AND relation_kind = 'reversal_reason'
                              AND target_event.event_type <> 'reversal') THEN
                    RAISE EXCEPTION 'only reversal events may attach reversal reason evidence';
                END IF;
                RETURN;
            END IF;
            SELECT * INTO target_batch FROM payroll_batches
             WHERE org_id = target_event.org_id AND business_event_id = target_event.id;
            IF NOT FOUND THEN RETURN; END IF;
            IF EXISTS (
                (SELECT evidence_id FROM payroll_batch_evidence
                  WHERE org_id = target_batch.org_id AND payroll_batch_id = target_batch.id)
                EXCEPT ALL
                (SELECT evidence_id FROM event_evidence
                  WHERE org_id = target_event.org_id AND event_id = target_event.id
                    AND relation_kind IN ('supporting', 'inherited'))
            ) OR EXISTS (
                (SELECT evidence_id FROM event_evidence
                  WHERE org_id = target_event.org_id AND event_id = target_event.id
                    AND relation_kind IN ('supporting', 'inherited'))
                EXCEPT ALL
                (SELECT evidence_id FROM payroll_batch_evidence
                  WHERE org_id = target_batch.org_id AND payroll_batch_id = target_batch.id)
            ) THEN
                RAISE EXCEPTION 'final payroll accrual event evidence must exactly equal payroll batch evidence';
            END IF;
            IF target_batch.reversal_of_batch_id IS NULL THEN
                IF EXISTS (SELECT 1 FROM event_evidence
                            WHERE org_id = target_event.org_id AND event_id = target_event.id
                              AND relation_kind <> 'supporting') THEN
                    RAISE EXCEPTION 'normal payroll accrual evidence must be supporting evidence';
                END IF;
            ELSE
                IF target_event.facts ->> 'original_event_id' IS NULL
                   OR EXISTS (SELECT 1 FROM event_evidence
                              WHERE org_id = target_event.org_id AND event_id = target_event.id
                                AND relation_kind = 'supporting') THEN
                    RAISE EXCEPTION 'payroll reversal evidence must inherit the original evidence set';
                END IF;
            END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_event_evidence()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN PERFORM finance_assert_final_event_evidence(OLD.event_id); END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN PERFORM finance_assert_final_event_evidence(NEW.event_id); END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_event_evidence_from_batch()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_event_evidence(business_event_id)
                  FROM payroll_batches WHERE id = OLD.id AND org_id = OLD.org_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_event_evidence(business_event_id)
                  FROM payroll_batches WHERE id = NEW.id AND org_id = NEW.org_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_final_event_evidence_from_batch_edge()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP IN ('UPDATE', 'DELETE') THEN
                PERFORM finance_assert_final_event_evidence(business_event_id)
                  FROM payroll_batches WHERE id = OLD.payroll_batch_id AND org_id = OLD.org_id;
            END IF;
            IF TG_OP IN ('INSERT', 'UPDATE') THEN
                PERFORM finance_assert_final_event_evidence(business_event_id)
                  FROM payroll_batches WHERE id = NEW.payroll_batch_id AND org_id = NEW.org_id;
            END IF;
            RETURN NULL;
        END;
        $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER final_event_evidence_invariant_deferred
        AFTER INSERT OR UPDATE OR DELETE ON event_evidence DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_event_evidence();
        CREATE TRIGGER immutable_final_event_evidence BEFORE INSERT OR UPDATE OR DELETE ON event_evidence
        FOR EACH ROW EXECUTE FUNCTION finance_block_final_event_evidence_mutation();
        CREATE CONSTRAINT TRIGGER final_payroll_batch_event_evidence_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_batches DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_event_evidence_from_batch();
        CREATE CONSTRAINT TRIGGER final_payroll_batch_edge_evidence_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_batch_evidence DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_final_event_evidence_from_batch_edge();
        """
    )

    op.execute(
        """
        CREATE OR REPLACE FUNCTION finance_assert_payroll_profile_version_lineage(target_id uuid)
        RETURNS void AS $$
        DECLARE target employee_payroll_profile_versions%ROWTYPE;
        BEGIN
            SELECT * INTO target FROM employee_payroll_profile_versions WHERE id = target_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL
                    SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM employee_payroll_profile_versions AS parent
                      JOIN lineage ON parent.id = lineage.supersedes_id
                     WHERE NOT lineage.cycle
                ) SELECT 1 FROM lineage WHERE cycle LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_PROFILE_VERSION_SUCCESSOR_CYCLE'; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM employee_payroll_profile_versions AS parent
                      JOIN lineage ON parent.id = lineage.supersedes_id WHERE NOT lineage.cycle
                ), ancestors AS (SELECT id FROM lineage WHERE id <> target.id)
                SELECT 1 FROM employee_payroll_profile_versions AS candidate
                 WHERE candidate.id <> target.id AND candidate.org_id = target.org_id
                   AND candidate.employee_id = target.employee_id
                   AND NOT EXISTS (SELECT 1 FROM ancestors WHERE ancestors.id = candidate.id)
                   AND NOT EXISTS (
                       WITH RECURSIVE candidate_lineage(id, supersedes_id, path, cycle) AS (
                           SELECT candidate.id, candidate.supersedes_id, ARRAY[candidate.id], false
                           UNION ALL
                           SELECT parent.id, parent.supersedes_id,
                                  candidate_lineage.path || parent.id,
                                  parent.id = ANY(candidate_lineage.path)
                             FROM employee_payroll_profile_versions AS parent
                             JOIN candidate_lineage
                               ON parent.id = candidate_lineage.supersedes_id
                            WHERE NOT candidate_lineage.cycle
                       )
                       SELECT 1 FROM candidate_lineage WHERE id = target.id
                   )
                   AND candidate.effective_from <= COALESCE(target.effective_to, 'infinity'::date)
                   AND target.effective_from <= COALESCE(candidate.effective_to, 'infinity'::date)
                 LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_PROFILE_VERSION_NON_ANCESTOR_OVERLAP'; END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_payroll_policy_version_lineage(target_id uuid)
        RETURNS void AS $$
        DECLARE target payroll_policy_versions%ROWTYPE;
        BEGIN
            SELECT * INTO target FROM payroll_policy_versions WHERE id = target_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM payroll_policy_versions AS parent JOIN lineage ON parent.id = lineage.supersedes_id
                     WHERE NOT lineage.cycle
                ) SELECT 1 FROM lineage WHERE cycle LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_POLICY_VERSION_SUCCESSOR_CYCLE'; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM payroll_policy_versions AS parent JOIN lineage ON parent.id = lineage.supersedes_id
                     WHERE NOT lineage.cycle
                ), ancestors AS (SELECT id FROM lineage WHERE id <> target.id)
                SELECT 1 FROM payroll_policy_versions AS candidate
                 WHERE candidate.id <> target.id AND candidate.org_id = target.org_id
                   AND candidate.region = target.region
                   AND NOT EXISTS (SELECT 1 FROM ancestors WHERE ancestors.id = candidate.id)
                   AND NOT EXISTS (
                       WITH RECURSIVE candidate_lineage(id, supersedes_id, path, cycle) AS (
                           SELECT candidate.id, candidate.supersedes_id, ARRAY[candidate.id], false
                           UNION ALL
                           SELECT parent.id, parent.supersedes_id,
                                  candidate_lineage.path || parent.id,
                                  parent.id = ANY(candidate_lineage.path)
                             FROM payroll_policy_versions AS parent
                             JOIN candidate_lineage
                               ON parent.id = candidate_lineage.supersedes_id
                            WHERE NOT candidate_lineage.cycle
                       )
                       SELECT 1 FROM candidate_lineage WHERE id = target.id
                   )
                   AND candidate.effective_from <= COALESCE(target.effective_to, 'infinity'::date)
                   AND target.effective_from <= COALESCE(candidate.effective_to, 'infinity'::date)
                 LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_POLICY_VERSION_NON_ANCESTOR_OVERLAP'; END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_assert_payroll_opening_state_lineage(target_id uuid)
        RETURNS void AS $$
        DECLARE target payroll_opening_states%ROWTYPE;
        BEGIN
            SELECT * INTO target FROM payroll_opening_states WHERE id = target_id;
            IF NOT FOUND THEN RETURN; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM payroll_opening_states AS parent JOIN lineage ON parent.id = lineage.supersedes_id
                     WHERE NOT lineage.cycle
                ) SELECT 1 FROM lineage WHERE cycle LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_OPENING_STATE_SUCCESSOR_CYCLE'; END IF;
            IF EXISTS (
                WITH RECURSIVE lineage(id, supersedes_id, path, cycle) AS (
                    SELECT target.id, target.supersedes_id, ARRAY[target.id], false
                    UNION ALL SELECT parent.id, parent.supersedes_id, lineage.path || parent.id,
                           parent.id = ANY(lineage.path)
                      FROM payroll_opening_states AS parent JOIN lineage ON parent.id = lineage.supersedes_id
                     WHERE NOT lineage.cycle
                ), ancestors AS (SELECT id FROM lineage WHERE id <> target.id)
                SELECT 1 FROM payroll_opening_states AS candidate
                 WHERE candidate.id <> target.id AND candidate.org_id = target.org_id
                   AND candidate.employee_id = target.employee_id AND candidate.tax_year = target.tax_year
                   AND candidate.through_month = target.through_month
                   AND NOT EXISTS (SELECT 1 FROM ancestors WHERE ancestors.id = candidate.id)
                   AND NOT EXISTS (
                       WITH RECURSIVE candidate_lineage(id, supersedes_id, path, cycle) AS (
                           SELECT candidate.id, candidate.supersedes_id, ARRAY[candidate.id], false
                           UNION ALL
                           SELECT parent.id, parent.supersedes_id,
                                  candidate_lineage.path || parent.id,
                                  parent.id = ANY(candidate_lineage.path)
                             FROM payroll_opening_states AS parent
                             JOIN candidate_lineage
                               ON parent.id = candidate_lineage.supersedes_id
                            WHERE NOT candidate_lineage.cycle
                       )
                       SELECT 1 FROM candidate_lineage WHERE id = target.id
                   )
                 LIMIT 1
            ) THEN RAISE EXCEPTION 'PAYROLL_OPENING_STATE_NON_ANCESTOR_OVERLAP'; END IF;
        END;
        $$ LANGUAGE plpgsql;

        CREATE OR REPLACE FUNCTION finance_validate_payroll_profile_version_lineage()
        RETURNS trigger AS $$ BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN PERFORM finance_assert_payroll_profile_version_lineage(OLD.id); END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN PERFORM finance_assert_payroll_profile_version_lineage(NEW.id); END IF;
            RETURN NULL;
        END; $$ LANGUAGE plpgsql;
        CREATE OR REPLACE FUNCTION finance_validate_payroll_policy_version_lineage()
        RETURNS trigger AS $$ BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN PERFORM finance_assert_payroll_policy_version_lineage(OLD.id); END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN PERFORM finance_assert_payroll_policy_version_lineage(NEW.id); END IF;
            RETURN NULL;
        END; $$ LANGUAGE plpgsql;
        CREATE OR REPLACE FUNCTION finance_validate_payroll_opening_state_lineage()
        RETURNS trigger AS $$ BEGIN
            IF TG_OP IN ('UPDATE','DELETE') THEN PERFORM finance_assert_payroll_opening_state_lineage(OLD.id); END IF;
            IF TG_OP IN ('INSERT','UPDATE') THEN PERFORM finance_assert_payroll_opening_state_lineage(NEW.id); END IF;
            RETURN NULL;
        END; $$ LANGUAGE plpgsql;

        CREATE CONSTRAINT TRIGGER payroll_profile_version_lineage_deferred
        AFTER INSERT OR UPDATE OR DELETE ON employee_payroll_profile_versions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_profile_version_lineage();
        CREATE CONSTRAINT TRIGGER payroll_policy_version_lineage_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_policy_versions DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_policy_version_lineage();
        CREATE CONSTRAINT TRIGGER payroll_opening_state_lineage_deferred
        AFTER INSERT OR UPDATE OR DELETE ON payroll_opening_states DEFERRABLE INITIALLY DEFERRED
        FOR EACH ROW EXECUTE FUNCTION finance_validate_payroll_opening_state_lineage();
        """
    )

    # All data is explicitly rechecked after the functions exist.  This is in
    # the same PostgreSQL migration transaction as the DDL, so a failure
    # leaves both the data and alembic revision on 0004.
    op.execute(
        """
        SELECT finance_assert_payroll_withholding_batch(id) FROM payroll_batches;
        SELECT finance_assert_payroll_batch_tax_state(id) FROM payroll_batches;
        SELECT finance_assert_payroll_event_link(id) FROM payroll_event_links;
        SELECT finance_assert_final_payroll_event_links(id) FROM business_events;
        SELECT finance_assert_final_event_evidence(id) FROM business_events;
        SELECT finance_assert_final_business_event(id) FROM business_events;
        SELECT finance_assert_payroll_profile_version_lineage(id)
          FROM employee_payroll_profile_versions;
        SELECT finance_assert_payroll_policy_version_lineage(id) FROM payroll_policy_versions;
        SELECT finance_assert_payroll_opening_state_lineage(id) FROM payroll_opening_states;
        SELECT finance_assert_bank_transaction_match(id) FROM bank_transaction_matches;
        SELECT finance_assert_bank_transaction_current_match(id) FROM bank_transactions;
        """
    )


def downgrade() -> None:
    _assert_round4_downgrade_safe()
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        for statement in (
            "DROP TRIGGER IF EXISTS final_business_event_voucher_line_invariant_deferred ON voucher_lines",
            "DROP TRIGGER IF EXISTS payroll_profile_version_lineage_deferred ON employee_payroll_profile_versions",
            "DROP TRIGGER IF EXISTS payroll_policy_version_lineage_deferred ON payroll_policy_versions",
            "DROP TRIGGER IF EXISTS payroll_opening_state_lineage_deferred ON payroll_opening_states",
            "DROP TRIGGER IF EXISTS final_payroll_batch_edge_evidence_deferred ON payroll_batch_evidence",
            "DROP TRIGGER IF EXISTS final_payroll_batch_event_evidence_deferred ON payroll_batches",
            "DROP TRIGGER IF EXISTS immutable_final_event_evidence ON event_evidence",
            "DROP TRIGGER IF EXISTS final_event_evidence_invariant_deferred ON event_evidence",
            "DROP TRIGGER IF EXISTS payroll_event_link_event_shape_deferred ON business_events",
            "DROP TRIGGER IF EXISTS immutable_final_payroll_source_open_item ON open_items",
            "DROP TRIGGER IF EXISTS payroll_tax_slot_batch_coverage_deferred ON payroll_tax_state_slots",
            "DROP TRIGGER IF EXISTS payroll_line_tax_state_invariant_deferred ON payroll_lines",
            "DROP TRIGGER IF EXISTS payroll_batch_tax_state_invariant_deferred ON payroll_batches",
            "DROP TRIGGER IF EXISTS immutable_final_payroll_withholding_entitlement ON payroll_withholding_entitlements",
            "DROP TRIGGER IF EXISTS payroll_withholding_entitlement_shape_deferred ON payroll_withholding_entitlements",
            "DROP TRIGGER IF EXISTS immutable_final_payroll_event_link ON payroll_event_links",
            "DROP TRIGGER IF EXISTS payroll_event_link_shape_deferred ON payroll_event_links",
            "DROP TRIGGER IF EXISTS immutable_sealed_payroll_batch_evidence ON payroll_batch_evidence",
            "DROP TRIGGER IF EXISTS immutable_bank_transaction_match ON bank_transaction_matches",
            "DROP TRIGGER IF EXISTS bank_transaction_match_invariant_deferred ON bank_transaction_matches",
            "DROP TRIGGER IF EXISTS bank_transaction_current_match_invariant_deferred ON bank_transactions",
            "DROP TRIGGER IF EXISTS final_business_event_voucher_invariant_deferred ON vouchers",
            "DROP TRIGGER IF EXISTS final_business_event_invariant_deferred ON business_events",
            "DROP TRIGGER IF EXISTS immutable_final_business_event ON business_events",
            "DROP TRIGGER IF EXISTS immutable_payroll_tax_state_slot ON payroll_tax_state_slots",
            "DROP TRIGGER IF EXISTS payroll_tax_state_slot_shape_deferred ON payroll_tax_state_slots",
            "DROP TRIGGER IF EXISTS payroll_withholding_payment_r3_invariant_deferred ON payroll_withholding_payment_allocations",
            "DROP TRIGGER IF EXISTS payroll_withholding_line_invariant_deferred ON payroll_lines",
            "DROP TRIGGER IF EXISTS payroll_withholding_batch_invariant_deferred ON payroll_batches",
            "DROP TRIGGER IF EXISTS immutable_payroll_withholding_payment_allocation ON payroll_withholding_payment_allocations",
            "DROP TRIGGER IF EXISTS immutable_employee_payroll_profile_version ON employee_payroll_profile_versions",
            "DROP TRIGGER IF EXISTS immutable_payroll_policy_version ON payroll_policy_versions",
            "DROP TRIGGER IF EXISTS immutable_payroll_opening_state ON payroll_opening_states",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_batch_tax_state_from_slot()",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_batch_tax_state_from_line()",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_batch_tax_state_from_batch()",
            "DROP FUNCTION IF EXISTS finance_assert_payroll_batch_tax_state(uuid)",
            "DROP FUNCTION IF EXISTS finance_validate_final_business_event_from_voucher_line()",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_profile_version_lineage()",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_policy_version_lineage()",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_opening_state_lineage()",
            "DROP FUNCTION IF EXISTS finance_assert_payroll_profile_version_lineage(uuid)",
            "DROP FUNCTION IF EXISTS finance_assert_payroll_policy_version_lineage(uuid)",
            "DROP FUNCTION IF EXISTS finance_assert_payroll_opening_state_lineage(uuid)",
            "DROP FUNCTION IF EXISTS finance_assert_exact_reversal_voucher(uuid, uuid)",
            "DROP FUNCTION IF EXISTS finance_validate_final_event_evidence_from_batch_edge()",
            "DROP FUNCTION IF EXISTS finance_validate_final_event_evidence_from_batch()",
            "DROP FUNCTION IF EXISTS finance_validate_final_event_evidence()",
            "DROP FUNCTION IF EXISTS finance_assert_final_event_evidence(uuid)",
            "DROP FUNCTION IF EXISTS finance_block_final_event_evidence_mutation()",
            "DROP FUNCTION IF EXISTS finance_assert_final_payroll_event_links(uuid)",
            "DROP FUNCTION IF EXISTS finance_validate_payroll_event_links_from_event()",
            "DROP FUNCTION IF EXISTS finance_block_final_payroll_source_open_item_mutation()",
        ):
            op.execute(statement)
    op.drop_index("ix_event_evidence_org_id", table_name="event_evidence")
    with op.batch_alter_table("event_evidence") as batch_op:
        batch_op.drop_constraint("ck_event_evidence_relation_kind", type_="check")
        batch_op.drop_constraint("fk_event_evidence_org_evidence", type_="foreignkey")
        batch_op.drop_constraint("fk_event_evidence_org_event", type_="foreignkey")
        batch_op.drop_column("relation_kind")
        batch_op.drop_column("org_id")
    if bind.dialect.name == "postgresql":
        # R4 replaced several R3 functions in-place.  Reinstalling the prior
        # revision's complete trigger set after the R4-only structures are
        # removed gives a safe empty-data downgrade the exact 0004 contract.
        module_path = Path(__file__).with_name("0004_payroll_round3_integrity.py")
        spec = importlib.util.spec_from_file_location("round3_restore", module_path)
        assert spec is not None and spec.loader is not None
        round3 = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(round3)
        round3._install_postgresql_invariants()
        # 0005 removed only the R3 trigger bindings, not their functions.
        # Recreate the exact 0004 immediate contract after R4 structures have
        # been removed.
        op.execute(
            """
            CREATE TRIGGER payroll_profile_version_chain
            BEFORE INSERT OR UPDATE ON employee_payroll_profile_versions
            FOR EACH ROW EXECUTE FUNCTION finance_validate_profile_version_chain();
            CREATE TRIGGER payroll_policy_version_chain
            BEFORE INSERT OR UPDATE ON payroll_policy_versions
            FOR EACH ROW EXECUTE FUNCTION finance_validate_policy_version_chain();
            CREATE TRIGGER payroll_opening_state_version_chain
            BEFORE INSERT OR UPDATE ON payroll_opening_states
            FOR EACH ROW EXECUTE FUNCTION finance_validate_opening_state_version_chain();
            """
        )
