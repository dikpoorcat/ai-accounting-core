"""Separate external declarations from later cash payments.

Revision ID: 0019_declaration_only
Revises: 0018_historical_obligation
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0019_declaration_only"
down_revision = "0018_historical_obligation"
branch_labels = None
depends_on = None

_TABLE = "payroll_contribution_assessment_confirmations"

_NEW_DECLARATION_STATUS = (
    "declaration_status IN "
    "('declared','declared_paid','declared_unpaid','not_declared')"
)
_OLD_DECLARATION_STATUS = (
    "declaration_status IN ('declared_paid','declared_unpaid','not_declared')"
)
_NEW_PAYMENT_STATUS = "payment_status IN ('not_tracked','paid','unpaid','not_applicable')"
_OLD_PAYMENT_STATUS = "payment_status IN ('paid','unpaid','not_applicable')"
_NEW_STATUS_DATES = (
    "(declaration_status = 'declared' AND declaration_date IS NOT NULL "
    "AND payment_status = 'not_tracked' AND payment_date IS NULL) OR "
    "(declaration_status = 'declared_paid' AND declaration_date IS NOT NULL "
    "AND payment_status = 'paid' AND payment_date IS NOT NULL) OR "
    "(declaration_status = 'declared_unpaid' AND declaration_date IS NOT NULL "
    "AND payment_status = 'unpaid' AND payment_date IS NULL) OR "
    "(declaration_status = 'not_declared' AND declaration_date IS NULL "
    "AND payment_status = 'not_applicable' AND payment_date IS NULL)"
)
_OLD_STATUS_DATES = (
    "(declaration_status = 'declared_paid' AND declaration_date IS NOT NULL "
    "AND payment_status = 'paid' AND payment_date IS NOT NULL) OR "
    "(declaration_status = 'declared_unpaid' AND declaration_date IS NOT NULL "
    "AND payment_status = 'unpaid' AND payment_date IS NULL) OR "
    "(declaration_status = 'not_declared' AND declaration_date IS NULL "
    "AND payment_status = 'not_applicable' AND payment_date IS NULL)"
)


def _replace_constraints(
    *, declaration_status: str, payment_status: str, status_dates: str
) -> None:
    constraints = (
        (
            "ck_contribution_assessment_declaration_status",
            declaration_status,
        ),
        ("ck_contribution_assessment_payment_status", payment_status),
        ("ck_contribution_assessment_status_dates", status_dates),
    )
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(_TABLE, recreate="always") as batch_op:
            for name, _condition in constraints:
                batch_op.drop_constraint(name, type_="check")
            for name, condition in constraints:
                batch_op.create_check_constraint(name, condition)
        return
    for name, _condition in constraints:
        op.drop_constraint(name, _TABLE, type_="check")
    for name, condition in constraints:
        op.create_check_constraint(name, _TABLE, condition)


def upgrade() -> None:
    _replace_constraints(
        declaration_status=_NEW_DECLARATION_STATUS,
        payment_status=_NEW_PAYMENT_STATUS,
        status_dates=_NEW_STATUS_DATES,
    )


def downgrade() -> None:
    if op.get_bind().scalar(
        sa.text(
            f"SELECT COUNT(*) FROM {_TABLE} WHERE declaration_status = 'declared'"
        )
    ):
        raise RuntimeError(
            "cannot downgrade 0019 while declaration-only confirmations exist"
        )
    _replace_constraints(
        declaration_status=_OLD_DECLARATION_STATUS,
        payment_status=_OLD_PAYMENT_STATUS,
        status_dates=_OLD_STATUS_DATES,
    )
