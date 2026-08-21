"""Allow controlled labor events through the final-event wrapper.

Revision ID: 0015_labor_final_events
Revises: 0014_labor_gross_unwithheld
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0015_labor_final_events"
down_revision = "0014_labor_gross_unwithheld"
branch_labels = None
depends_on = None


_OLD_SPECIALIZED_EVENTS = (
    "                'borrowing_interest_accrual','borrowing_interest_payment',\n"
    "                'borrowing_principal_repayment'"
)

_NEW_SPECIALIZED_EVENTS = (
    "                'borrowing_interest_accrual','borrowing_interest_payment',\n"
    "                'borrowing_principal_repayment','labor_remuneration_accrual',\n"
    "                'unified_payout_run','labor_withholding_tax_payment'"
)


def _replace_final_event_wrapper(*, upgrade: bool) -> None:
    connection = op.get_bind()
    definition = connection.scalar(
        sa.text(
            "SELECT pg_get_functiondef("
            "'finance_assert_final_business_event_0014(uuid)'::regprocedure)"
        )
    )
    if not isinstance(definition, str):
        raise RuntimeError(
            "required PostgreSQL function is missing: "
            "finance_assert_final_business_event_0014(uuid)"
        )
    old = _OLD_SPECIALIZED_EVENTS if upgrade else _NEW_SPECIALIZED_EVENTS
    new = _NEW_SPECIALIZED_EVENTS if upgrade else _OLD_SPECIALIZED_EVENTS
    if definition.count(old) != 1:
        raise RuntimeError("unexpected PostgreSQL final-event wrapper shape")
    connection.exec_driver_sql(definition.replace(old, new).replace("%", "%%"))


def upgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _replace_final_event_wrapper(upgrade=True)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _replace_final_event_wrapper(upgrade=False)
