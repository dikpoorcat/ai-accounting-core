"""Separate withholding start and make tax-reported salary the sole wage fact.

Revision ID: 0017_payroll_reported_salary
Revises: 0016_close_labor_module
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0017_payroll_reported_salary"
down_revision = "0016_close_labor_module"
branch_labels = None
depends_on = None


_OLD_GROSS_CHECK = (
    "gross_salary_fen = base_salary_fen + performance_pay_fen + taxable_allowance_fen + "
    "tax_exempt_income_fen + annual_bonus_fen - attendance_deduction_fen AND "
    "gross_salary_fen > 0"
)
_NEW_GROSS_CHECK = (
    "((tax_reported_salary_fen IS NOT NULL AND annual_bonus_fen = 0 AND "
    "gross_salary_fen = tax_reported_salary_fen) OR "
    "(tax_reported_salary_fen IS NULL AND annual_bonus_fen > 0 AND "
    "gross_salary_fen = annual_bonus_fen))"
)
_OLD_NONNEGATIVE_CHECK = (
    "base_salary_fen >= 0 AND performance_pay_fen >= 0 AND "
    "taxable_allowance_fen >= 0 AND tax_exempt_income_fen >= 0 AND "
    "attendance_deduction_fen >= 0 AND special_additional_deduction_fen >= 0 AND "
    "other_legal_deduction_fen >= 0 AND annual_bonus_fen >= 0 AND "
    "employee_social_insurance_fen >= 0 AND employer_social_insurance_fen >= 0 AND "
    "employee_housing_fund_fen >= 0 AND employer_housing_fund_fen >= 0 AND "
    "individual_income_tax_fen >= 0"
)
_NEW_NONNEGATIVE_CHECK = (
    "(tax_reported_salary_fen IS NULL OR tax_reported_salary_fen >= 0) AND "
    "special_additional_deduction_fen >= 0 AND other_legal_deduction_fen >= 0 AND "
    "annual_bonus_fen >= 0 AND employee_social_insurance_fen >= 0 AND "
    "employer_social_insurance_fen >= 0 AND employee_housing_fund_fen >= 0 AND "
    "employer_housing_fund_fen >= 0 AND individual_income_tax_fen >= 0"
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


def _upgrade_postgresql_tax_period_functions() -> None:
    op.execute(
        """
        CREATE FUNCTION finance_payroll_tax_date_0017(
            batch_kind text, payroll_period text, payment_date date
        ) RETURNS date
        LANGUAGE sql IMMUTABLE STRICT PARALLEL SAFE
        AS $$
            SELECT CASE
                WHEN batch_kind = 'regular' THEN
                    (make_date(substr(payroll_period, 1, 4)::integer,
                               substr(payroll_period, 6, 2)::integer, 1)
                     + INTERVAL '1 month - 1 day')::date
                ELSE payment_date
            END
        $$
        """
    )
    tax_date = (
        "finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date)"
    )
    target_date = (
        "finance_payroll_tax_date_0017(target_batch.batch_kind, "
        "target_batch.payroll_period, target_batch.payment_date)"
    )
    regular_date = (
        "finance_payroll_tax_date_0017(regular.batch_kind, regular.payroll_period, "
        "regular.payment_date)"
    )
    final_date = (
        "finance_payroll_tax_date_0017(final_batch.batch_kind, final_batch.payroll_period, "
        "final_batch.payment_date)"
    )
    new_date = "finance_payroll_tax_date_0017(NEW.batch_kind, NEW.payroll_period, NEW.payment_date)"

    _replace_postgresql_function(
        "finance_assert_final_statutory_payment_compatibility(uuid)",
        (("to_char(batch.payment_date, 'YYYY-MM')", "to_char(" + tax_date + ", 'YYYY-MM')", 1),),
    )
    _replace_postgresql_function(
        "finance_assert_opening_correction_dependencies(uuid,uuid,integer)",
        (("batch.payment_date", tax_date, 2),),
    )
    _replace_postgresql_function(
        "finance_assert_payroll_batch_tax_state(uuid)",
        (("target_batch.payment_date", target_date, 4),),
    )
    _replace_postgresql_function(
        "finance_assert_payroll_tax_state_slot(uuid)",
        (
            ("regular.payment_date", regular_date, 2),
            ("final_batch.payment_date", final_date, 2),
            ("same payment month", "same tax month", 1),
        ),
    )

    # Preserve the CTE column name while changing its value from bank-payment
    # date to the controlled tax-period date.
    _replace_postgresql_function(
        "finance_assert_policy_correction_dependencies(uuid,text)",
        (
            (
                "batch.status AS batch_status, batch.payment_date",
                "batch.status AS batch_status, __PAYROLL_TAX_DATE__ AS payment_date",
                1,
            ),
            ("batch.payment_date", "__PAYROLL_TAX_DATE__", 4),
            ("__PAYROLL_TAX_DATE__", tax_date, 5),
        ),
    )
    _replace_postgresql_function(
        "finance_assert_profile_correction_dependencies(uuid,uuid)",
        (
            (
                "batch.id AS batch_id, batch.status AS batch_status, batch.payment_date,",
                "batch.id AS batch_id, batch.status AS batch_status, "
                "__PAYROLL_TAX_DATE__ AS payment_date,",
                1,
            ),
            ("batch.payment_date", "__PAYROLL_TAX_DATE__", 3),
            ("__PAYROLL_TAX_DATE__", tax_date, 4),
        ),
    )
    _replace_postgresql_function(
        "finance_lock_final_payroll_dependency_guards()",
        (("NEW.payment_date", new_date, 2),),
    )
    _replace_postgresql_function(
        "finance_lock_final_payroll_line_dependency_guards()",
        (("final_batch.payment_date", final_date, 2),),
    )
    _replace_postgresql_function(
        "finance_validate_final_payroll_dependencies_from_batch()",
        (("batch.payment_date", tax_date, 2),),
    )
    _replace_postgresql_function(
        "finance_validate_final_payroll_dependencies_from_line()",
        (("batch.payment_date", tax_date, 2),),
    )


def _downgrade_postgresql_tax_period_functions() -> None:
    tax_date = (
        "finance_payroll_tax_date_0017(batch.batch_kind, batch.payroll_period, batch.payment_date)"
    )
    target_date = (
        "finance_payroll_tax_date_0017(target_batch.batch_kind, "
        "target_batch.payroll_period, target_batch.payment_date)"
    )
    regular_date = (
        "finance_payroll_tax_date_0017(regular.batch_kind, regular.payroll_period, "
        "regular.payment_date)"
    )
    final_date = (
        "finance_payroll_tax_date_0017(final_batch.batch_kind, final_batch.payroll_period, "
        "final_batch.payment_date)"
    )
    new_date = "finance_payroll_tax_date_0017(NEW.batch_kind, NEW.payroll_period, NEW.payment_date)"
    _replace_postgresql_function(
        "finance_assert_final_statutory_payment_compatibility(uuid)",
        ((tax_date, "batch.payment_date", 1),),
    )
    _replace_postgresql_function(
        "finance_assert_opening_correction_dependencies(uuid,uuid,integer)",
        ((tax_date, "batch.payment_date", 2),),
    )
    _replace_postgresql_function(
        "finance_assert_payroll_batch_tax_state(uuid)",
        ((target_date, "target_batch.payment_date", 4),),
    )
    _replace_postgresql_function(
        "finance_assert_payroll_tax_state_slot(uuid)",
        (
            (regular_date, "regular.payment_date", 2),
            (final_date, "final_batch.payment_date", 2),
            ("same tax month", "same payment month", 1),
        ),
    )
    _replace_postgresql_function(
        "finance_assert_policy_correction_dependencies(uuid,text)",
        ((tax_date, "batch.payment_date", 5),),
    )
    _replace_postgresql_function(
        "finance_assert_policy_correction_dependencies(uuid,text)",
        (("batch.payment_date AS payment_date", "batch.payment_date", 1),),
    )
    _replace_postgresql_function(
        "finance_assert_profile_correction_dependencies(uuid,uuid)",
        ((tax_date, "batch.payment_date", 4),),
    )
    _replace_postgresql_function(
        "finance_assert_profile_correction_dependencies(uuid,uuid)",
        (("batch.payment_date AS payment_date", "batch.payment_date", 1),),
    )
    _replace_postgresql_function(
        "finance_lock_final_payroll_dependency_guards()",
        ((new_date, "NEW.payment_date", 2),),
    )
    _replace_postgresql_function(
        "finance_lock_final_payroll_line_dependency_guards()",
        ((final_date, "final_batch.payment_date", 2),),
    )
    _replace_postgresql_function(
        "finance_validate_final_payroll_dependencies_from_batch()",
        ((tax_date, "batch.payment_date", 2),),
    )
    _replace_postgresql_function(
        "finance_validate_final_payroll_dependencies_from_line()",
        ((tax_date, "batch.payment_date", 2),),
    )
    op.execute("DROP FUNCTION finance_payroll_tax_date_0017(text,text,date)")


def upgrade() -> None:
    with op.batch_alter_table("employees") as batch:
        batch.add_column(sa.Column("tax_withholding_start_date", sa.Date(), nullable=True))
    # Preserve the legacy start-date assumption for pre-migration employees.
    # New employees may leave the fact unset, in which case payroll returns
    # needs_information instead of inferring it.
    op.execute(
        "UPDATE employees SET tax_withholding_start_date = employment_start_date "
        "WHERE tax_withholding_start_date IS NULL"
    )
    with op.batch_alter_table("employees") as batch:
        batch.create_check_constraint(
            "ck_employee_tax_withholding_start",
            "tax_withholding_start_date IS NULL OR "
            "employment_start_date <= tax_withholding_start_date",
        )
    with op.batch_alter_table("payroll_lines") as batch:
        batch.add_column(sa.Column("tax_reported_salary_fen", sa.BigInteger(), nullable=True))
    # The legacy line's gross amount is the only lossless wage fact available.
    # Annual-bonus lines remain distinguished by a null tax-reported salary.
    op.execute(
        "UPDATE payroll_lines SET tax_reported_salary_fen = gross_salary_fen "
        "WHERE payroll_batch_id IN "
        "(SELECT id FROM payroll_batches WHERE batch_kind = 'regular')"
    )
    with op.batch_alter_table("payroll_lines") as batch:
        batch.drop_constraint("ck_payroll_line_nonnegative_amounts", type_="check")
        batch.drop_constraint("ck_payroll_line_gross_salary", type_="check")
        batch.create_check_constraint("ck_payroll_line_nonnegative_amounts", _NEW_NONNEGATIVE_CHECK)
        batch.create_check_constraint("ck_payroll_line_gross_salary", _NEW_GROSS_CHECK)
        batch.drop_column("attendance_deduction_fen")
        batch.drop_column("tax_exempt_income_fen")
        batch.drop_column("taxable_allowance_fen")
        batch.drop_column("performance_pay_fen")
        batch.drop_column("base_salary_fen")
    if op.get_bind().dialect.name == "postgresql":
        _upgrade_postgresql_tax_period_functions()


def downgrade() -> None:
    zero_salary_count = int(
        op.get_bind().scalar(
            sa.text("SELECT count(*) FROM payroll_lines WHERE gross_salary_fen = 0")
        )
        or 0
    )
    if zero_salary_count:
        raise RuntimeError("PAYROLL_REPORTED_SALARY_DOWNGRADE_UNSAFE")
    if op.get_bind().dialect.name == "postgresql":
        _downgrade_postgresql_tax_period_functions()
    with op.batch_alter_table("payroll_lines") as batch:
        batch.drop_constraint("ck_payroll_line_gross_salary", type_="check")
        batch.drop_constraint("ck_payroll_line_nonnegative_amounts", type_="check")
        batch.add_column(
            sa.Column("base_salary_fen", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("performance_pay_fen", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("taxable_allowance_fen", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column("tax_exempt_income_fen", sa.BigInteger(), nullable=False, server_default="0")
        )
        batch.add_column(
            sa.Column(
                "attendance_deduction_fen",
                sa.BigInteger(),
                nullable=False,
                server_default="0",
            )
        )
    op.execute(
        "UPDATE payroll_lines SET base_salary_fen = tax_reported_salary_fen "
        "WHERE tax_reported_salary_fen IS NOT NULL"
    )
    with op.batch_alter_table("payroll_lines") as batch:
        batch.create_check_constraint("ck_payroll_line_nonnegative_amounts", _OLD_NONNEGATIVE_CHECK)
        batch.create_check_constraint("ck_payroll_line_gross_salary", _OLD_GROSS_CHECK)
        batch.drop_column("tax_reported_salary_fen")
    with op.batch_alter_table("employees") as batch:
        batch.drop_constraint("ck_employee_tax_withholding_start", type_="check")
        batch.drop_column("tax_withholding_start_date")
