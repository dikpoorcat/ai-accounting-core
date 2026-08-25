"""Add actual salary deductions and multi-row unified payout matching.

Revision ID: 0020_salary_deduction_payout
Revises: 0019_deferred_output_vat
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0020_salary_deduction_payout"
down_revision = "0019_deferred_output_vat"
branch_labels = None
depends_on = None


def _replace_postgresql_function(
    regprocedure: str, replacements: tuple[tuple[str, str], ...]
) -> None:
    connection = op.get_bind()
    definition = connection.scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:identity AS regprocedure))"),
        {"identity": regprocedure},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"required PostgreSQL function is missing: {regprocedure}")
    for old, new in replacements:
        if definition.count(old) != 1:
            raise RuntimeError(f"unexpected PostgreSQL function shape for {regprocedure}: {old}")
        definition = definition.replace(old, new)
    connection.exec_driver_sql(definition.replace("%", "%%"))


def _new_source_kind_check() -> str:
    return (
        "(item_kind = 'salary' AND payroll_line_id IS NOT NULL AND labor_line_id IS NULL "
        "AND settlement_mode = 'not_applicable') OR "
        "(item_kind = 'labor' AND payroll_line_id IS NULL AND labor_line_id IS NOT NULL "
        "AND actual_salary_deduction_fen = 0 AND settlement_mode IN "
        "('net_after_withholding','gross_paid_without_withholding'))"
    )


def _new_totals_check() -> str:
    return (
        "gross_amount_fen > 0 AND employee_social_insurance_fen >= 0 "
        "AND employee_housing_fund_fen >= 0 AND individual_income_tax_fen >= 0 "
        "AND actual_salary_deduction_fen >= 0 "
        "AND theoretical_individual_income_tax_fen >= individual_income_tax_fen "
        "AND unwithheld_individual_income_tax_fen = "
        "theoretical_individual_income_tax_fen - individual_income_tax_fen "
        "AND net_amount_fen = gross_amount_fen - employee_social_insurance_fen "
        "- employee_housing_fund_fen - individual_income_tax_fen "
        "- actual_salary_deduction_fen AND net_amount_fen >= 0"
    )


_OLD_BANK_INVARIANT = """    IF target.status = 'posted' AND (
        NOT EXISTS (
            SELECT 1 FROM bank_transactions AS bank
             WHERE bank.org_id = target.org_id AND bank.id = target.bank_transaction_id
               AND bank.import_action_id IS NOT NULL
               AND bank.bank_account_code = target.bank_account_code
               AND bank.amount_fen = -target.net_total_fen
               AND bank.booking_date = target.payment_date
               AND bank.matched_event_id = target.business_event_id
        ) OR (
            SELECT count(*) FROM bank_transaction_matches AS match
             WHERE match.org_id = target.org_id
               AND match.bank_transaction_id = target.bank_transaction_id
               AND match.event_id = target.business_event_id
               AND match.invalidated_by_event_id IS NULL
        ) <> 1
    ) THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_BANK_MATCH_MISMATCH';
    END IF;"""


_NEW_BANK_AND_DEDUCTION_INVARIANT = """    IF target.status = 'posted' AND (
        EXISTS (
            SELECT 1
              FROM unified_payout_run_items AS run_item
             WHERE run_item.org_id = target.org_id
               AND run_item.payout_run_id = target.id
               AND run_item.item_kind = 'salary'
               AND run_item.actual_salary_deduction_fen <> coalesce((
                    SELECT sum(allocation.amount_fen)
                      FROM payroll_salary_actual_deduction_allocations AS allocation
                     WHERE allocation.org_id = target.org_id
                       AND allocation.payment_event_id = target.business_event_id
                       AND allocation.payroll_line_id = run_item.payroll_line_id
                       AND allocation.reversed IS FALSE
               ), 0)
        ) OR EXISTS (
            SELECT 1
              FROM payroll_salary_actual_deduction_allocations AS allocation
             WHERE allocation.org_id = target.org_id
               AND allocation.payment_event_id = target.business_event_id
               AND allocation.reversed IS FALSE
               AND NOT EXISTS (
                    SELECT 1 FROM unified_payout_run_items AS run_item
                     WHERE run_item.org_id = target.org_id
                       AND run_item.payout_run_id = target.id
                       AND run_item.payroll_line_id = allocation.payroll_line_id
                       AND run_item.actual_salary_deduction_fen = allocation.amount_fen
               )
        )
    ) THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_SALARY_DEDUCTION_MISMATCH';
    END IF;
    IF target.status = 'posted' AND (
        NOT EXISTS (
            SELECT 1 FROM unified_payout_run_bank_transactions AS relation
             WHERE relation.org_id = target.org_id AND relation.payout_run_id = target.id
               AND relation.bank_transaction_id = target.bank_transaction_id
        ) OR EXISTS (
            SELECT 1
              FROM unified_payout_run_bank_transactions AS relation
              LEFT JOIN bank_transactions AS bank
                ON bank.org_id = relation.org_id
               AND bank.id = relation.bank_transaction_id
              LEFT JOIN bank_transaction_matches AS match
                ON match.org_id = relation.org_id
               AND match.bank_transaction_id = relation.bank_transaction_id
               AND match.event_id = target.business_event_id
               AND match.invalidated_by_event_id IS NULL
             WHERE relation.org_id = target.org_id AND relation.payout_run_id = target.id
               AND (bank.id IS NULL OR bank.import_action_id IS NULL
                    OR bank.bank_account_code <> target.bank_account_code
                    OR bank.booking_date <> target.payment_date
                    OR bank.matched_event_id <> target.business_event_id
                    OR match.id IS NULL)
        ) OR coalesce((
            SELECT sum(bank.amount_fen)
              FROM unified_payout_run_bank_transactions AS relation
              JOIN bank_transactions AS bank
                ON bank.org_id = relation.org_id
               AND bank.id = relation.bank_transaction_id
             WHERE relation.org_id = target.org_id AND relation.payout_run_id = target.id
        ), 0) <> -target.net_total_fen
        OR (SELECT count(*) FROM bank_transaction_matches AS match
             WHERE match.org_id = target.org_id
               AND match.event_id = target.business_event_id
               AND match.invalidated_by_event_id IS NULL)
           <> (SELECT count(*) FROM unified_payout_run_bank_transactions AS relation
                WHERE relation.org_id = target.org_id
                  AND relation.payout_run_id = target.id)
    ) THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_BANK_MATCH_MISMATCH';
    END IF;"""


_SALARY_DEDUCTION_FUNCTIONS = r"""
CREATE OR REPLACE FUNCTION finance_guard_salary_actual_deduction_0020()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE source payroll_salary_actual_deduction_allocations%ROWTYPE;
DECLARE payment_status text;
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'SALARY_ACTUAL_DEDUCTION_APPEND_ONLY';
    END IF;
    source := NEW;
    SELECT status INTO payment_status FROM business_events
     WHERE org_id = source.org_id AND id = source.payment_event_id;
    IF TG_OP = 'INSERT' THEN
        IF payment_status IS DISTINCT FROM 'draft' THEN
            RAISE EXCEPTION 'SALARY_ACTUAL_DEDUCTION_REQUIRES_DRAFT_PAYMENT';
        END IF;
        RETURN NEW;
    END IF;
    IF OLD.reversed IS FALSE AND NEW.reversed IS TRUE
       AND OLD.reversed_by_event_id IS NULL AND NEW.reversed_by_event_id IS NOT NULL
       AND OLD.org_id = NEW.org_id AND OLD.payroll_line_id = NEW.payroll_line_id
       AND OLD.payment_event_id = NEW.payment_event_id
       AND OLD.amount_fen = NEW.amount_fen AND OLD.expense_role = NEW.expense_role THEN
        RETURN NEW;
    END IF;
    RAISE EXCEPTION 'SALARY_ACTUAL_DEDUCTION_IMMUTABLE';
END;
$$;

CREATE OR REPLACE FUNCTION finance_assert_salary_actual_deduction_0020(target_event_id uuid)
RETURNS void
LANGUAGE plpgsql
AS $$
DECLARE target business_events%ROWTYPE;
DECLARE active_total bigint;
DECLARE expected_total bigint;
BEGIN
    SELECT * INTO target FROM business_events WHERE id = target_event_id;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT coalesce(sum(amount_fen) FILTER (WHERE reversed IS FALSE), 0)
      INTO active_total
      FROM payroll_salary_actual_deduction_allocations
     WHERE org_id = target.org_id AND payment_event_id = target.id;
    SELECT coalesce(
        NULLIF(target.facts::jsonb #>> '{derived,actual_salary_deduction_fen}', '')::bigint,
        NULLIF(target.facts::jsonb ->> 'actual_salary_deduction_fen', '')::bigint,
        0
    ) INTO expected_total;
    IF active_total = 0 AND expected_total = 0 THEN RETURN; END IF;
    IF target.event_type NOT IN ('salary_payment','unified_payout_run')
       OR target.status NOT IN ('posted','reversed') THEN
        RAISE EXCEPTION 'SALARY_ACTUAL_DEDUCTION_PAYMENT_EVENT_INVALID';
    END IF;
    IF target.status = 'posted' AND (active_total <= 0 OR active_total <> expected_total) THEN
        RAISE EXCEPTION 'SALARY_ACTUAL_DEDUCTION_EVENT_TOTAL_MISMATCH';
    END IF;
    IF target.status = 'posted' AND EXISTS (
        SELECT 1
          FROM payroll_salary_actual_deduction_allocations AS allocation
          JOIN payroll_lines AS line
            ON line.org_id = allocation.org_id AND line.id = allocation.payroll_line_id
          JOIN payroll_batches AS batch
            ON batch.org_id = line.org_id AND batch.id = line.payroll_batch_id
          JOIN employee_payroll_profile_versions AS profile
            ON profile.org_id = line.org_id
           AND profile.employee_id = line.employee_id
           AND profile.id = line.employee_payroll_profile_version_id
         WHERE allocation.org_id = target.org_id
           AND allocation.payment_event_id = target.id
           AND allocation.reversed IS FALSE
           AND (batch.status <> 'posted' OR allocation.expense_role <> profile.expense_role
                OR NOT EXISTS (
                    SELECT 1
                      FROM settlements AS settlement
                      JOIN open_items AS item
                        ON item.org_id = settlement.org_id
                       AND item.id = settlement.open_item_id
                      JOIN employees AS employee
                        ON employee.org_id = item.org_id
                       AND employee.counterparty_id = item.counterparty_id
                     WHERE settlement.org_id = allocation.org_id
                       AND settlement.payment_event_id = allocation.payment_event_id
                       AND settlement.reversed IS FALSE
                       AND item.payable_category = 'salary'
                       AND item.source_event_id = batch.business_event_id
                       AND employee.id = line.employee_id
                       AND settlement.amount_fen >= allocation.amount_fen
                ))
    ) THEN
        RAISE EXCEPTION 'SALARY_ACTUAL_DEDUCTION_SOURCE_MISMATCH';
    END IF;
    IF target.status = 'posted' AND EXISTS (
        WITH allocation_total AS (
            SELECT expense_role, sum(amount_fen)::bigint AS amount_fen
              FROM payroll_salary_actual_deduction_allocations
             WHERE org_id = target.org_id AND payment_event_id = target.id
               AND reversed IS FALSE
             GROUP BY expense_role
        ), voucher_total AS (
            SELECT account.system_role AS expense_role,
                   sum(line.credit_fen)::bigint AS amount_fen
              FROM vouchers AS voucher
              JOIN voucher_lines AS line ON line.voucher_id = voucher.id
              JOIN accounts AS account ON account.id = line.account_id
             WHERE voucher.event_id = target.id
               AND account.system_role IN (
                   'payroll_management_expense','payroll_sales_expense','payroll_service_cost'
               )
             GROUP BY account.system_role
        )
        SELECT 1 FROM allocation_total
        FULL JOIN voucher_total USING (expense_role)
         WHERE coalesce(allocation_total.amount_fen, 0)
               <> coalesce(voucher_total.amount_fen, 0)
    ) THEN
        RAISE EXCEPTION 'SALARY_ACTUAL_DEDUCTION_VOUCHER_MISMATCH';
    END IF;
    IF target.status = 'reversed' AND EXISTS (
        SELECT 1 FROM payroll_salary_actual_deduction_allocations AS allocation
         WHERE allocation.org_id = target.org_id
           AND allocation.payment_event_id = target.id
           AND (allocation.reversed IS FALSE OR allocation.reversed_by_event_id IS NULL
                OR NOT EXISTS (
                    SELECT 1 FROM business_events AS reversal
                     WHERE reversal.org_id = allocation.org_id
                       AND reversal.id = allocation.reversed_by_event_id
                       AND reversal.status = 'posted'
                       AND reversal.facts ->> 'original_event_id' = target.id::text
                ))
    ) THEN
        RAISE EXCEPTION 'SALARY_ACTUAL_DEDUCTION_REVERSAL_MISMATCH';
    END IF;
END;
$$;

CREATE OR REPLACE FUNCTION finance_validate_salary_actual_deduction_0020()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE target_event_id uuid;
BEGIN
    IF TG_OP = 'DELETE' THEN
        target_event_id := OLD.payment_event_id;
    ELSE
        target_event_id := NEW.payment_event_id;
    END IF;
    PERFORM finance_assert_salary_actual_deduction_0020(target_event_id);
    RETURN NULL;
END;
$$;

CREATE OR REPLACE FUNCTION finance_guard_payout_bank_relation_0020()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE parent_status text;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_BANK_RELATION_APPEND_ONLY';
    END IF;
    SELECT status INTO parent_status FROM unified_payout_runs
     WHERE org_id = NEW.org_id AND id = NEW.payout_run_id;
    IF parent_status IS DISTINCT FROM 'calculated' THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_BANK_RELATION_REQUIRES_CALCULATED_RUN';
    END IF;
    RETURN NEW;
END;
$$;
"""


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER unified_payout_run_items_immutability_guard "
            "ON unified_payout_run_items"
        )
    with op.batch_alter_table("unified_payout_run_items") as batch:
        batch.add_column(
            sa.Column(
                "actual_salary_deduction_fen",
                sa.BigInteger(),
                nullable=True,
                server_default="0",
            )
        )
    op.execute(
        "UPDATE unified_payout_run_items SET actual_salary_deduction_fen = 0 "
        "WHERE actual_salary_deduction_fen IS NULL"
    )
    with op.batch_alter_table("unified_payout_run_items") as batch:
        batch.drop_constraint("ck_payout_item_source_kind", type_="check")
        batch.drop_constraint("ck_payout_item_totals", type_="check")
        batch.alter_column(
            "actual_salary_deduction_fen",
            existing_type=sa.BigInteger(),
            nullable=False,
            server_default=None,
        )
        batch.create_check_constraint("ck_payout_item_source_kind", _new_source_kind_check())
        batch.create_check_constraint("ck_payout_item_totals", _new_totals_check())
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER unified_payout_run_items_immutability_guard "
            "BEFORE UPDATE OR DELETE ON unified_payout_run_items FOR EACH ROW "
            "EXECUTE FUNCTION finance_block_final_labor_graph_0013()"
        )

    op.create_table(
        "payroll_salary_actual_deduction_allocations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_line_id", sa.Uuid(), nullable=False),
        sa.Column("payment_event_id", sa.Uuid(), nullable=False),
        sa.Column("amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("expense_role", sa.String(50), nullable=False),
        sa.Column("reversed", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("reversed_by_event_id", sa.Uuid(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_line_id"],
            ["payroll_lines.org_id", "payroll_lines.id"],
            name="fk_salary_actual_deduction_org_line",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_salary_actual_deduction_org_payment_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "reversed_by_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_salary_actual_deduction_org_reversal_event",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "org_id",
            "payroll_line_id",
            "payment_event_id",
            name="uq_salary_actual_deduction_line_event",
        ),
        sa.CheckConstraint("amount_fen > 0", name="ck_salary_actual_deduction_positive"),
        sa.CheckConstraint(
            "expense_role IN ('payroll_management_expense','payroll_sales_expense',"
            "'payroll_service_cost')",
            name="ck_salary_actual_deduction_expense_role",
        ),
        sa.CheckConstraint(
            "(reversed IS FALSE AND reversed_by_event_id IS NULL) OR "
            "(reversed IS TRUE AND reversed_by_event_id IS NOT NULL)",
            name="ck_salary_actual_deduction_reversal",
        ),
    )
    op.create_index(
        "ix_salary_actual_deduction_org_line",
        "payroll_salary_actual_deduction_allocations",
        ["org_id", "payroll_line_id"],
    )
    op.create_index(
        "ix_salary_actual_deduction_org_event",
        "payroll_salary_actual_deduction_allocations",
        ["org_id", "payment_event_id"],
    )

    op.create_table(
        "unified_payout_run_bank_transactions",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("payout_run_id", sa.Uuid(), nullable=False),
        sa.Column("bank_transaction_id", sa.Uuid(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("org_id", "payout_run_id", "bank_transaction_id"),
        sa.ForeignKeyConstraint(
            ["org_id", "payout_run_id"],
            ["unified_payout_runs.org_id", "unified_payout_runs.id"],
            name="fk_payout_bank_org_run",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "bank_transaction_id"],
            ["bank_transactions.org_id", "bank_transactions.id"],
            name="fk_payout_bank_org_transaction",
            ondelete="RESTRICT",
        ),
        sa.UniqueConstraint(
            "payout_run_id",
            "bank_transaction_id",
            name="uq_payout_bank_run_transaction",
        ),
    )
    op.execute(
        "INSERT INTO unified_payout_run_bank_transactions "
        "(org_id, payout_run_id, bank_transaction_id, created_at) "
        "SELECT org_id, id, bank_transaction_id, created_at FROM unified_payout_runs"
    )

    if op.get_bind().dialect.name != "postgresql":
        return

    _replace_postgresql_function(
        "finance_assert_unified_payout_0013(uuid)",
        (
            (
                "coalesce(sum(employee_social_insurance_fen\n"
                "                      + employee_housing_fund_fen "
                "+ individual_income_tax_fen),0),",
                "coalesce(sum(employee_social_insurance_fen\n"
                "                      + employee_housing_fund_fen + individual_income_tax_fen\n"
                "                      + actual_salary_deduction_fen),0),",
            ),
            (_OLD_BANK_INVARIANT, _NEW_BANK_AND_DEDUCTION_INVARIANT),
            (
                "'withheld_employee_housing_fund_payable',\n"
                "                           'individual_income_tax_payable'",
                "'withheld_employee_housing_fund_payable',\n"
                "                           'individual_income_tax_payable',\n"
                "                           'payroll_management_expense',\n"
                "                           'payroll_sales_expense',\n"
                "                           'payroll_service_cost'",
            ),
            (
                "       OR EXISTS (\n"
                "            SELECT 1 FROM unified_payout_run_items AS run_item\n"
                "             WHERE run_item.org_id = target.org_id\n"
                "               AND run_item.payout_run_id = target.id "
                "AND run_item.item_kind = 'labor'",
                "       OR EXISTS (\n"
                "            WITH allocation_total AS (\n"
                "                SELECT expense_role, sum(amount_fen)::bigint AS amount_fen\n"
                "                  FROM payroll_salary_actual_deduction_allocations\n"
                "                 WHERE org_id = target.org_id\n"
                "                   AND payment_event_id = target.business_event_id\n"
                "                   AND reversed IS FALSE GROUP BY expense_role\n"
                "            ), voucher_total AS (\n"
                "                SELECT account.system_role AS expense_role,\n"
                "                       sum(voucher_line.credit_fen)::bigint AS amount_fen\n"
                "                  FROM vouchers AS voucher\n"
                "                  JOIN voucher_lines AS voucher_line\n"
                "                    ON voucher_line.voucher_id = voucher.id\n"
                "                  JOIN accounts AS account "
                "ON account.id = voucher_line.account_id\n"
                "                 WHERE voucher.event_id = target.business_event_id\n"
                "                   AND account.system_role IN (\n"
                "                       'payroll_management_expense','payroll_sales_expense',\n"
                "                       'payroll_service_cost')\n"
                "                 GROUP BY account.system_role\n"
                "            ) SELECT 1 FROM allocation_total\n"
                "              FULL JOIN voucher_total USING (expense_role)\n"
                "             WHERE coalesce(allocation_total.amount_fen, 0)\n"
                "                   <> coalesce(voucher_total.amount_fen, 0)\n"
                "       )\n"
                "       OR EXISTS (\n"
                "            SELECT 1 FROM unified_payout_run_items AS run_item\n"
                "             WHERE run_item.org_id = target.org_id\n"
                "               AND run_item.payout_run_id = target.id "
                "AND run_item.item_kind = 'labor'",
            ),
        ),
    )

    op.execute(_SALARY_DEDUCTION_FUNCTIONS)
    op.execute(
        "CREATE TRIGGER salary_actual_deduction_guard_0020 "
        "BEFORE INSERT OR UPDATE OR DELETE ON payroll_salary_actual_deduction_allocations "
        "FOR EACH ROW EXECUTE FUNCTION finance_guard_salary_actual_deduction_0020()"
    )
    op.execute(
        "CREATE CONSTRAINT TRIGGER salary_actual_deduction_invariant_0020 "
        "AFTER INSERT OR UPDATE OR DELETE ON payroll_salary_actual_deduction_allocations "
        "DEFERRABLE INITIALLY DEFERRED FOR EACH ROW "
        "EXECUTE FUNCTION finance_validate_salary_actual_deduction_0020()"
    )
    op.execute(
        "CREATE TRIGGER unified_payout_bank_relation_guard_0020 "
        "BEFORE INSERT OR UPDATE OR DELETE ON unified_payout_run_bank_transactions "
        "FOR EACH ROW EXECUTE FUNCTION finance_guard_payout_bank_relation_0020()"
    )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        raise RuntimeError(
            "0020 downgrade requires restoring the previous PostgreSQL invariant function; "
            "use a pre-migration backup"
        )

    op.drop_table("unified_payout_run_bank_transactions")
    op.drop_index(
        "ix_salary_actual_deduction_org_event",
        table_name="payroll_salary_actual_deduction_allocations",
    )
    op.drop_index(
        "ix_salary_actual_deduction_org_line",
        table_name="payroll_salary_actual_deduction_allocations",
    )
    op.drop_table("payroll_salary_actual_deduction_allocations")
    with op.batch_alter_table("unified_payout_run_items") as batch:
        batch.drop_constraint("ck_payout_item_source_kind", type_="check")
        batch.drop_constraint("ck_payout_item_totals", type_="check")
        batch.drop_column("actual_salary_deduction_fen")
        batch.create_check_constraint(
            "ck_payout_item_source_kind",
            "(item_kind = 'salary' AND payroll_line_id IS NOT NULL "
            "AND labor_line_id IS NULL AND settlement_mode = 'not_applicable') OR "
            "(item_kind = 'labor' AND payroll_line_id IS NULL "
            "AND labor_line_id IS NOT NULL AND settlement_mode IN "
            "('net_after_withholding','gross_paid_without_withholding'))",
        )
        batch.create_check_constraint(
            "ck_payout_item_totals",
            "gross_amount_fen > 0 AND employee_social_insurance_fen >= 0 "
            "AND employee_housing_fund_fen >= 0 AND individual_income_tax_fen >= 0 "
            "AND theoretical_individual_income_tax_fen >= individual_income_tax_fen "
            "AND unwithheld_individual_income_tax_fen = "
            "theoretical_individual_income_tax_fen - individual_income_tax_fen "
            "AND net_amount_fen = gross_amount_fen - employee_social_insurance_fen "
            "- employee_housing_fund_fen - individual_income_tax_fen "
            "AND net_amount_fen >= 0",
        )
