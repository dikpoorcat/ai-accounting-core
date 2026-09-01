"""Separate regular payroll accrual from later cash settlement.

Revision ID: 0020_payroll_accrual_date
Revises: 0019_declaration_only
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0020_payroll_accrual_date"
down_revision = "0019_declaration_only"
branch_labels = None
depends_on = None

_BONUS_PAYMENT_DATE_CHECK = "batch_kind = 'regular' OR payment_date IS NOT NULL"


def _replace_tax_date_function(*, strict: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    strict_clause = " STRICT" if strict else ""
    op.execute(
        sa.text(
            f"""
            CREATE OR REPLACE FUNCTION finance_payroll_tax_date_0017(
                batch_kind text,
                payroll_period text,
                payment_date date
            ) RETURNS date
            LANGUAGE sql IMMUTABLE{strict_clause} PARALLEL SAFE
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
    )


def upgrade() -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("payroll_batches", recreate="always") as batch_op:
            batch_op.alter_column(
                "payment_date",
                existing_type=sa.Date(),
                nullable=True,
            )
            batch_op.create_check_constraint(
                "ck_payroll_batch_bonus_payment_date",
                _BONUS_PAYMENT_DATE_CHECK,
            )
    else:
        op.alter_column(
            "payroll_batches",
            "payment_date",
            existing_type=sa.Date(),
            nullable=True,
        )
        op.create_check_constraint(
            "ck_payroll_batch_bonus_payment_date",
            "payroll_batches",
            _BONUS_PAYMENT_DATE_CHECK,
        )
    # The baseline helper was STRICT, which returned NULL before evaluating
    # the regular-payroll branch when payment_date became NULL.
    _replace_tax_date_function(strict=False)


def downgrade() -> None:
    missing = op.get_bind().scalar(
        sa.text("SELECT COUNT(*) FROM payroll_batches WHERE payment_date IS NULL")
    )
    if missing:
        raise RuntimeError(
            "cannot downgrade 0020 while payroll batches without payment dates exist"
        )
    _replace_tax_date_function(strict=True)
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table("payroll_batches", recreate="always") as batch_op:
            batch_op.drop_constraint("ck_payroll_batch_bonus_payment_date", type_="check")
            batch_op.alter_column(
                "payment_date",
                existing_type=sa.Date(),
                nullable=False,
            )
    else:
        op.drop_constraint(
            "ck_payroll_batch_bonus_payment_date",
            "payroll_batches",
            type_="check",
        )
        op.alter_column(
            "payroll_batches",
            "payment_date",
            existing_type=sa.Date(),
            nullable=False,
        )
