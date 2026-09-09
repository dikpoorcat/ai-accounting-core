"""Accounting date precision and optional management facts.

Revision ID: 0004_fact_precision
Revises: 0003_essential_accounting
"""

import re
from pathlib import Path

import sqlalchemy as sa

from alembic import op

revision = "0004_fact_precision"
down_revision = "0003_essential_accounting"
branch_labels = None
depends_on = None

GROSS = (
    "((wage_tax_scope = 'wage_income' AND tax_reported_salary_fen IS NOT NULL "
    "AND annual_bonus_fen = 0) OR (wage_tax_scope = 'contributions_only' "
    "AND tax_reported_salary_fen IS NULL AND annual_bonus_fen = 0 AND gross_salary_fen = 0) "
    "OR (wage_tax_scope = 'not_applicable' AND tax_reported_salary_fen IS NULL "
    "AND annual_bonus_fen > 0 AND gross_salary_fen = annual_bonus_fen)) AND "
    "(tax_reporting_difference_reason IS NULL OR "
    "length(trim(tax_reporting_difference_reason)) BETWEEN 1 AND 2000)"
)
LABOR_GROSS = (
    "gross_remuneration_fen > 0 AND ((fixed_fee_fen IS NULL AND commission_fen IS NULL) "
    "OR (fixed_fee_fen IS NOT NULL AND commission_fen IS NOT NULL "
    "AND fixed_fee_fen >= 0 AND commission_fen >= 0 "
    "AND gross_remuneration_fen = fixed_fee_fen + commission_fen))"
)


def upgrade():
    bind = op.get_bind()
    postgres = bind.dialect.name == "postgresql"
    with op.batch_alter_table("organization_profile_versions") as batch:
        batch.drop_constraint("ck_org_profile_confirmation_note", type_="check")
        batch.create_check_constraint(
            "ck_org_profile_confirmation_note", "length(confirmation_note) <= 2000"
        )
    with op.batch_alter_table("borrowing_payments") as batch:
        batch.drop_constraint("ck_borrowing_payment_posting_date", type_="check")
        batch.create_check_constraint(
            "ck_borrowing_payment_posting_date", "posting_date >= payment_date"
        )
    for table in (
        "payroll_first_wage_tax_treatments",
        "payroll_contribution_actual_sets",
        "enterprise_income_tax_results",
    ):
        with op.batch_alter_table(table) as batch:
            batch.alter_column("declaration_date", existing_type=sa.Date(), nullable=True)
    with op.batch_alter_table("labor_remuneration_lines") as batch:
        batch.drop_constraint("ck_labor_line_gross", type_="check")
        for field in ("fixed_fee_fen", "commission_fen"):
            batch.alter_column(field, existing_type=sa.BigInteger(), nullable=True)
        batch.create_check_constraint("ck_labor_line_gross", LABOR_GROSS)
    with op.batch_alter_table("payroll_lines") as batch:
        batch.drop_constraint("ck_payroll_line_gross_salary", type_="check")
        batch.drop_constraint("ck_payroll_line_wage_tax_declaration_state", type_="check")
        batch.alter_column(
            "wage_tax_declaration_state",
            new_column_name="wage_tax_scope",
            existing_type=sa.String(20),
        )
    if postgres:
        op.execute("ALTER TABLE payroll_lines DISABLE TRIGGER USER")
    # Rename the normalized applicability values only. Immutable event JSON,
    # recorded calculation hashes and close snapshots are deliberately untouched.
    op.execute(
        "UPDATE payroll_lines SET wage_tax_scope = CASE wage_tax_scope "
        "WHEN 'declared' THEN 'wage_income' WHEN 'not_declared' THEN 'contributions_only' "
        "ELSE wage_tax_scope END"
    )
    if postgres:
        op.execute("ALTER TABLE payroll_lines ENABLE TRIGGER USER")
    with op.batch_alter_table("payroll_lines") as batch:
        batch.create_check_constraint("ck_payroll_line_gross_salary", GROSS)
        batch.create_check_constraint(
            "ck_payroll_line_wage_tax_scope",
            "wage_tax_scope IN ('wage_income','contributions_only','not_applicable')",
        )
    if postgres:
        functions = (
            bind.execute(
                sa.text(
                    "SELECT pg_get_functiondef(p.oid) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid=p.pronamespace "
                    "WHERE n.nspname='public' AND p.prokind='f' "
                    "AND p.proname LIKE 'finance_%'"
                )
            )
            .scalars()
            .all()
        )
        for definition in functions:
            if "wage_tax_declaration_state" not in definition:
                continue
            updated = definition.replace("wage_tax_declaration_state", "wage_tax_scope")
            updated = re.sub(r"(wage_tax_scope'?\s*=\s*)'declared'", r"\1'wage_income'", updated)
            updated = re.sub(
                r"(wage_tax_scope'?\s*=\s*)'not_declared'", r"\1'contributions_only'", updated
            )
            updated = re.sub(
                r"(CASE line.wage_tax_scope\s+WHEN )'declared'", r"\1'wage_income'", updated
            )
            with bind.connection.driver_connection.cursor() as cursor:
                cursor.execute(updated)
        with bind.connection.driver_connection.cursor() as cursor:
            cursor.execute(Path(__file__).with_name("0004_fact_precision.sql").read_text("utf-8"))


def downgrade():
    raise RuntimeError("FACT_PRECISION_FORWARD_ONLY")
