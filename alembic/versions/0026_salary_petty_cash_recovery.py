"""Add controlled salary-withholding recovery to an off-ledger petty-cash pool.

Revision ID: 0026_salary_petty_recovery
Revises: 0025_payroll_tax_declaration
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0026_salary_petty_recovery"
down_revision = "0025_payroll_tax_declaration"
branch_labels = None
depends_on = None


def _replace_postgresql_function(
    regprocedure: str, replacements: tuple[tuple[str, str, int], ...]
) -> None:
    connection = op.get_bind()
    definition = connection.scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:identity AS regprocedure))"),
        {"identity": regprocedure},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"required PostgreSQL function is missing: {regprocedure}")
    for old, new, expected_count in replacements:
        if definition.count(old) != expected_count:
            raise RuntimeError(f"unexpected PostgreSQL function shape for {regprocedure}: {old}")
        definition = definition.replace(old, new)
    connection.exec_driver_sql(definition.replace("%", "%%"))


def _run_totals_check() -> str:
    return (
        "gross_total_fen > 0 AND withholding_total_fen >= 0 AND net_total_fen > 0 "
        "AND salary_petty_cash_recovery_total_fen >= 0 "
        "AND salary_petty_cash_recovery_total_fen <= withholding_total_fen "
        "AND net_total_fen = gross_total_fen - withholding_total_fen "
        "+ salary_petty_cash_recovery_total_fen"
    )


def _source_kind_check() -> str:
    return (
        "(item_kind = 'salary' AND payroll_line_id IS NOT NULL AND labor_line_id IS NULL "
        "AND settlement_mode = 'not_applicable') OR "
        "(item_kind = 'labor' AND payroll_line_id IS NULL AND labor_line_id IS NOT NULL "
        "AND actual_salary_deduction_fen = 0 "
        "AND salary_petty_cash_recovery_fen = 0 AND settlement_mode IN "
        "('net_after_withholding','gross_paid_without_withholding'))"
    )


def _item_totals_check() -> str:
    return (
        "gross_amount_fen > 0 AND employee_social_insurance_fen >= 0 "
        "AND employee_housing_fund_fen >= 0 AND individual_income_tax_fen >= 0 "
        "AND actual_salary_deduction_fen >= 0 "
        "AND salary_petty_cash_recovery_fen >= 0 "
        "AND salary_petty_cash_recovery_fen <= employee_social_insurance_fen "
        "+ employee_housing_fund_fen + individual_income_tax_fen "
        "AND theoretical_individual_income_tax_fen >= individual_income_tax_fen "
        "AND unwithheld_individual_income_tax_fen = "
        "theoretical_individual_income_tax_fen - individual_income_tax_fen "
        "AND net_amount_fen = gross_amount_fen - employee_social_insurance_fen "
        "- employee_housing_fund_fen - individual_income_tax_fen "
        "- actual_salary_deduction_fen + salary_petty_cash_recovery_fen "
        "AND net_amount_fen >= 0"
    )


def _petty_recovery_check() -> str:
    return (
        "salary_petty_cash_recovery_fen = 0 OR "
        "(item_kind = 'salary' AND actual_salary_deduction_fen = 0 "
        "AND salary_petty_cash_recovery_fen = employee_social_insurance_fen "
        "+ employee_housing_fund_fen + individual_income_tax_fen)"
    )


def _upgrade_postgresql_invariant() -> None:
    _replace_postgresql_function(
        "finance_assert_unified_payout_0013(uuid)",
        (
            (
                "DECLARE item_withholding bigint;\nDECLARE item_net bigint;",
                "DECLARE item_withholding bigint;\n"
                "DECLARE item_recovery bigint;\nDECLARE item_net bigint;",
                1,
            ),
            (
                "                      + actual_salary_deduction_fen),0),\n"
                "           coalesce(sum(net_amount_fen),0)\n"
                "      INTO item_gross, item_withholding, item_net",
                "                      + actual_salary_deduction_fen),0),\n"
                "           coalesce(sum(salary_petty_cash_recovery_fen),0),\n"
                "           coalesce(sum(net_amount_fen),0)\n"
                "      INTO item_gross, item_withholding, item_recovery, item_net",
                1,
            ),
            (
                "       OR item_withholding <> target.withholding_total_fen\n"
                "       OR item_net <> target.net_total_fen THEN",
                "       OR item_withholding <> target.withholding_total_fen\n"
                "       OR item_recovery <> target.salary_petty_cash_recovery_total_fen\n"
                "       OR item_net <> target.net_total_fen THEN",
                1,
            ),
            (
                "                           'payroll_service_cost'\n"
                "                       ) AND voucher_line.debit_fen = 0 "
                "AND voucher_line.credit_fen > 0)",
                "                           'payroll_service_cost'\n"
                "                       ) AND voucher_line.debit_fen = 0 "
                "AND voucher_line.credit_fen > 0)\n"
                "                   OR (account.org_id = target.org_id\n"
                "                       AND account.system_role = 'general_expense'\n"
                "                       AND voucher_line.debit_fen > 0\n"
                "                       AND voucher_line.credit_fen = 0)",
                1,
            ),
            (
                "       OR coalesce((\n"
                "            SELECT sum(voucher_line.credit_fen)\n"
                "              FROM vouchers AS voucher\n"
                "              JOIN voucher_lines AS voucher_line ON "
                "voucher_line.voucher_id = voucher.id\n"
                "              JOIN accounts AS account ON account.id = voucher_line.account_id\n"
                "             WHERE voucher.event_id = target.business_event_id\n"
                "               AND account.system_role = 'withheld_employee_social_payable'",
                "       OR coalesce((\n"
                "            SELECT sum(voucher_line.debit_fen)\n"
                "              FROM vouchers AS voucher\n"
                "              JOIN voucher_lines AS voucher_line\n"
                "                ON voucher_line.voucher_id = voucher.id\n"
                "              JOIN accounts AS account ON account.id = voucher_line.account_id\n"
                "             WHERE voucher.event_id = target.business_event_id\n"
                "               AND account.system_role = 'general_expense'\n"
                "       ), 0) <> target.salary_petty_cash_recovery_total_fen\n"
                "       OR coalesce((\n"
                "            SELECT sum(voucher_line.credit_fen)\n"
                "              FROM vouchers AS voucher\n"
                "              JOIN voucher_lines AS voucher_line\n"
                "                ON voucher_line.voucher_id = voucher.id\n"
                "              JOIN accounts AS account ON account.id = voucher_line.account_id\n"
                "             WHERE voucher.event_id = target.business_event_id\n"
                "               AND account.system_role = 'withheld_employee_social_payable'",
                1,
            ),
        ),
    )


def upgrade() -> None:
    with op.batch_alter_table("unified_payout_runs") as batch:
        batch.drop_constraint("ck_payout_run_totals", type_="check")
        batch.add_column(
            sa.Column(
                "salary_petty_cash_recovery_total_fen",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch.create_check_constraint("ck_payout_run_totals", _run_totals_check())

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER unified_payout_run_items_immutability_guard "
            "ON unified_payout_run_items"
        )
    with op.batch_alter_table("unified_payout_run_items") as batch:
        batch.drop_constraint("ck_payout_item_source_kind", type_="check")
        batch.drop_constraint("ck_payout_item_totals", type_="check")
        batch.add_column(
            sa.Column(
                "salary_petty_cash_recovery_fen",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
        batch.create_check_constraint("ck_payout_item_source_kind", _source_kind_check())
        batch.create_check_constraint("ck_payout_item_totals", _item_totals_check())
        batch.create_check_constraint(
            "ck_payout_item_petty_recovery", _petty_recovery_check()
        )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER unified_payout_run_items_immutability_guard "
            "BEFORE UPDATE OR DELETE ON unified_payout_run_items FOR EACH ROW "
            "EXECUTE FUNCTION finance_block_final_labor_graph_0013()"
        )
        _upgrade_postgresql_invariant()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        raise RuntimeError(
            "0026 downgrade requires restoring the previous PostgreSQL invariant function; "
            "use a pre-migration backup"
        )

    with op.batch_alter_table("unified_payout_run_items") as batch:
        batch.drop_constraint("ck_payout_item_petty_recovery", type_="check")
        batch.drop_constraint("ck_payout_item_source_kind", type_="check")
        batch.drop_constraint("ck_payout_item_totals", type_="check")
        batch.drop_column("salary_petty_cash_recovery_fen")
        batch.create_check_constraint(
            "ck_payout_item_source_kind",
            "(item_kind = 'salary' AND payroll_line_id IS NOT NULL "
            "AND labor_line_id IS NULL AND settlement_mode = 'not_applicable') OR "
            "(item_kind = 'labor' AND payroll_line_id IS NULL "
            "AND labor_line_id IS NOT NULL AND actual_salary_deduction_fen = 0 "
            "AND settlement_mode IN "
            "('net_after_withholding','gross_paid_without_withholding'))",
        )
        batch.create_check_constraint(
            "ck_payout_item_totals",
            "gross_amount_fen > 0 AND employee_social_insurance_fen >= 0 "
            "AND employee_housing_fund_fen >= 0 AND individual_income_tax_fen >= 0 "
            "AND actual_salary_deduction_fen >= 0 "
            "AND theoretical_individual_income_tax_fen >= individual_income_tax_fen "
            "AND unwithheld_individual_income_tax_fen = "
            "theoretical_individual_income_tax_fen - individual_income_tax_fen "
            "AND net_amount_fen = gross_amount_fen - employee_social_insurance_fen "
            "- employee_housing_fund_fen - individual_income_tax_fen "
            "- actual_salary_deduction_fen AND net_amount_fen >= 0",
        )

    with op.batch_alter_table("unified_payout_runs") as batch:
        batch.drop_constraint("ck_payout_run_totals", type_="check")
        batch.drop_column("salary_petty_cash_recovery_total_fen")
        batch.create_check_constraint(
            "ck_payout_run_totals",
            "gross_total_fen > 0 AND withholding_total_fen >= 0 "
            "AND net_total_fen > 0 "
            "AND net_total_fen = gross_total_fen - withholding_total_fen",
        )
