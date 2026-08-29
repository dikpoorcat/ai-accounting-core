"""Allow evidenced accounting wages to differ from reported wage-tax income.

Revision ID: 0004_payroll_wage_tax_difference
Revises: 0003_historical_tax_rules
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_payroll_wage_tax_difference"
down_revision = "0003_historical_tax_rules"
branch_labels = None
depends_on = None

_NEW_NONNEGATIVE = (
    "(tax_reported_salary_fen IS NULL OR tax_reported_salary_fen >= 0) AND "
    "special_additional_deduction_fen >= 0 AND "
    "other_legal_deduction_fen >= 0 AND annual_bonus_fen >= 0 AND "
    "employee_social_insurance_fen >= 0 AND employer_social_insurance_fen >= 0 AND "
    "employee_housing_fund_fen >= 0 AND employer_housing_fund_fen >= 0 AND "
    "individual_income_tax_fen >= 0 AND gross_salary_fen >= 0"
)

_OLD_NONNEGATIVE = (
    "(tax_reported_salary_fen IS NULL OR tax_reported_salary_fen >= 0) AND "
    "special_additional_deduction_fen >= 0 AND "
    "other_legal_deduction_fen >= 0 AND annual_bonus_fen >= 0 AND "
    "employee_social_insurance_fen >= 0 AND employer_social_insurance_fen >= 0 AND "
    "employee_housing_fund_fen >= 0 AND employer_housing_fund_fen >= 0 AND "
    "individual_income_tax_fen >= 0"
)

_NEW_GROSS_SHAPE = (
    "((wage_tax_declaration_state = 'declared' AND "
    "tax_reported_salary_fen IS NOT NULL AND annual_bonus_fen = 0 AND "
    "((gross_salary_fen = tax_reported_salary_fen AND "
    "tax_reporting_difference_reason IS NULL) OR "
    "(gross_salary_fen <> tax_reported_salary_fen AND "
    "tax_reporting_difference_reason IS NOT NULL AND "
    "length(trim(tax_reporting_difference_reason)) BETWEEN 1 AND 2000))) OR "
    "(wage_tax_declaration_state = 'not_declared' AND "
    "tax_reported_salary_fen IS NULL AND annual_bonus_fen = 0 AND "
    "gross_salary_fen = 0 AND tax_reporting_difference_reason IS NULL) OR "
    "(wage_tax_declaration_state = 'not_applicable' AND "
    "tax_reported_salary_fen IS NULL AND annual_bonus_fen > 0 AND "
    "gross_salary_fen = annual_bonus_fen AND "
    "tax_reporting_difference_reason IS NULL))"
)

_OLD_GROSS_SHAPE = (
    "((wage_tax_declaration_state = 'declared' AND "
    "tax_reported_salary_fen IS NOT NULL AND annual_bonus_fen = 0 AND "
    "gross_salary_fen = tax_reported_salary_fen) OR "
    "(wage_tax_declaration_state = 'not_declared' AND "
    "tax_reported_salary_fen IS NULL AND annual_bonus_fen = 0 AND "
    "gross_salary_fen = 0) OR "
    "(wage_tax_declaration_state = 'not_applicable' AND "
    "tax_reported_salary_fen IS NULL AND annual_bonus_fen > 0 AND "
    "gross_salary_fen = annual_bonus_fen))"
)


def _replace_constraints(*, nonnegative: str, gross_shape: str) -> None:
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("payroll_lines", recreate="always") as batch_op:
            batch_op.drop_constraint(
                "ck_payroll_line_nonnegative_amounts", type_="check"
            )
            batch_op.drop_constraint("ck_payroll_line_gross_salary", type_="check")
            batch_op.create_check_constraint(
                "ck_payroll_line_nonnegative_amounts", nonnegative
            )
            batch_op.create_check_constraint("ck_payroll_line_gross_salary", gross_shape)
        return
    op.drop_constraint(
        "ck_payroll_line_nonnegative_amounts", "payroll_lines", type_="check"
    )
    op.drop_constraint("ck_payroll_line_gross_salary", "payroll_lines", type_="check")
    op.create_check_constraint(
        "ck_payroll_line_nonnegative_amounts", "payroll_lines", nonnegative
    )
    op.create_check_constraint(
        "ck_payroll_line_gross_salary", "payroll_lines", gross_shape
    )


def upgrade() -> None:
    op.add_column(
        "payroll_lines",
        sa.Column("tax_reporting_difference_reason", sa.Text(), nullable=True),
    )
    _replace_constraints(nonnegative=_NEW_NONNEGATIVE, gross_shape=_NEW_GROSS_SHAPE)


def downgrade() -> None:
    _replace_constraints(nonnegative=_OLD_NONNEGATIVE, gross_shape=_OLD_GROSS_SHAPE)
    bind = op.get_bind()
    if bind.dialect.name == "sqlite":
        with op.batch_alter_table("payroll_lines", recreate="always") as batch_op:
            batch_op.drop_column("tax_reporting_difference_reason")
        return
    op.drop_column("payroll_lines", "tax_reporting_difference_reason")
