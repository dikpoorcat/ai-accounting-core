"""Allow evidenced contribution-only payroll without a wage-tax state.

Revision ID: 0025_payroll_tax_declaration
Revises: 0024_first_wage_tax
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0025_payroll_tax_declaration"
down_revision = "0024_first_wage_tax"
branch_labels = None
depends_on = None


_OLD_GROSS_CHECK = (
    "((tax_reported_salary_fen IS NOT NULL AND annual_bonus_fen = 0 AND "
    "gross_salary_fen = tax_reported_salary_fen) OR "
    "(tax_reported_salary_fen IS NULL AND annual_bonus_fen > 0 AND "
    "gross_salary_fen = annual_bonus_fen))"
)
_NEW_GROSS_CHECK = (
    "((wage_tax_declaration_state = 'declared' AND "
    "tax_reported_salary_fen IS NOT NULL AND annual_bonus_fen = 0 AND "
    "gross_salary_fen = tax_reported_salary_fen) OR "
    "(wage_tax_declaration_state = 'not_declared' AND "
    "tax_reported_salary_fen IS NULL AND annual_bonus_fen = 0 AND gross_salary_fen = 0) OR "
    "(wage_tax_declaration_state = 'not_applicable' AND "
    "tax_reported_salary_fen IS NULL AND annual_bonus_fen > 0 AND "
    "gross_salary_fen = annual_bonus_fen))"
)


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


def _upgrade_postgresql_functions() -> None:
    _replace_postgresql_function(
        "finance_assert_payroll_batch_tax_state(uuid)",
        (
            (
                "IF target_batch.batch_kind = 'regular' THEN\n"
                "                SELECT EXISTS (\n"
                "                    SELECT 1\n"
                "                      FROM payroll_lines AS line\n"
                "                     WHERE line.org_id = target_batch.org_id\n"
                "                       AND line.payroll_batch_id = target_batch.id\n"
                "                       AND 1 <> (",
                "IF target_batch.batch_kind = 'regular' THEN\n"
                "                SELECT EXISTS (\n"
                "                    SELECT 1\n"
                "                      FROM payroll_lines AS line\n"
                "                     WHERE line.org_id = target_batch.org_id\n"
                "                       AND line.payroll_batch_id = target_batch.id\n"
                "                       AND CASE line.wage_tax_declaration_state\n"
                "                               WHEN 'declared' THEN 1 ELSE 0\n"
                "                           END <> (",
                1,
            ),
            (
                "final regular payroll requires exactly one tax state slot per employee",
                "final regular payroll requires one tax state slot per declared employee "
                "and none for a not-declared employee",
                1,
            ),
        ),
    )
    _replace_postgresql_function(
        "finance_assert_opening_correction_dependencies(uuid,uuid,integer)",
        (
            (
                "AND batch.reversal_of_batch_id IS NULL\n"
                "                   AND EXTRACT(YEAR FROM",
                "AND batch.reversal_of_batch_id IS NULL\n"
                "                   AND (batch.batch_kind <> 'regular' OR "
                "line.wage_tax_declaration_state = 'declared')\n"
                "                   AND EXTRACT(YEAR FROM",
                1,
            ),
        ),
    )


def _downgrade_postgresql_functions() -> None:
    _replace_postgresql_function(
        "finance_assert_opening_correction_dependencies(uuid,uuid,integer)",
        (
            (
                "AND batch.reversal_of_batch_id IS NULL\n"
                "                   AND (batch.batch_kind <> 'regular' OR "
                "line.wage_tax_declaration_state = 'declared')\n"
                "                   AND EXTRACT(YEAR FROM",
                "AND batch.reversal_of_batch_id IS NULL\n"
                "                   AND EXTRACT(YEAR FROM",
                1,
            ),
        ),
    )
    _replace_postgresql_function(
        "finance_assert_payroll_batch_tax_state(uuid)",
        (
            (
                "IF target_batch.batch_kind = 'regular' THEN\n"
                "                SELECT EXISTS (\n"
                "                    SELECT 1\n"
                "                      FROM payroll_lines AS line\n"
                "                     WHERE line.org_id = target_batch.org_id\n"
                "                       AND line.payroll_batch_id = target_batch.id\n"
                "                       AND CASE line.wage_tax_declaration_state\n"
                "                               WHEN 'declared' THEN 1 ELSE 0\n"
                "                           END <> (",
                "IF target_batch.batch_kind = 'regular' THEN\n"
                "                SELECT EXISTS (\n"
                "                    SELECT 1\n"
                "                      FROM payroll_lines AS line\n"
                "                     WHERE line.org_id = target_batch.org_id\n"
                "                       AND line.payroll_batch_id = target_batch.id\n"
                "                       AND 1 <> (",
                1,
            ),
            (
                "final regular payroll requires one tax state slot per declared employee "
                "and none for a not-declared employee",
                "final regular payroll requires exactly one tax state slot per employee",
                1,
            ),
        ),
    )


def upgrade() -> None:
    with op.batch_alter_table("employee_payroll_profile_versions") as batch:
        batch.alter_column("resident_employee", existing_type=sa.Boolean(), nullable=True)

    with op.batch_alter_table("payroll_lines") as batch:
        batch.add_column(
            sa.Column(
                "wage_tax_declaration_state",
                sa.String(length=20),
                nullable=False,
                server_default="declared",
            )
        )
    op.execute(
        "UPDATE payroll_lines SET wage_tax_declaration_state = 'not_applicable' "
        "WHERE annual_bonus_fen > 0"
    )
    with op.batch_alter_table("payroll_lines") as batch:
        batch.drop_constraint("ck_payroll_line_gross_salary", type_="check")
        batch.create_check_constraint("ck_payroll_line_gross_salary", _NEW_GROSS_CHECK)
        batch.create_check_constraint(
            "ck_payroll_line_wage_tax_declaration_state",
            "wage_tax_declaration_state IN ('declared','not_declared','not_applicable')",
        )
        batch.alter_column("wage_tax_declaration_state", server_default=None)

    if op.get_bind().dialect.name == "postgresql":
        _upgrade_postgresql_functions()


def downgrade() -> None:
    connection = op.get_bind()
    if int(
        connection.scalar(
            sa.text(
                "SELECT count(*) FROM payroll_lines "
                "WHERE wage_tax_declaration_state = 'not_declared'"
            )
        )
        or 0
    ):
        raise RuntimeError("PAYROLL_TAX_DECLARATION_DOWNGRADE_UNSAFE")
    if int(
        connection.scalar(
            sa.text(
                "SELECT count(*) FROM employee_payroll_profile_versions "
                "WHERE resident_employee IS NULL"
            )
        )
        or 0
    ):
        raise RuntimeError("PAYROLL_RESIDENCY_DOWNGRADE_UNSAFE")

    if connection.dialect.name == "postgresql":
        _downgrade_postgresql_functions()

    with op.batch_alter_table("payroll_lines") as batch:
        batch.drop_constraint("ck_payroll_line_wage_tax_declaration_state", type_="check")
        batch.drop_constraint("ck_payroll_line_gross_salary", type_="check")
        batch.create_check_constraint("ck_payroll_line_gross_salary", _OLD_GROSS_CHECK)
        batch.drop_column("wage_tax_declaration_state")
    with op.batch_alter_table("employee_payroll_profile_versions") as batch:
        batch.alter_column("resident_employee", existing_type=sa.Boolean(), nullable=False)
