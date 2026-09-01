"""Make explicit-bank invariants conditional for inventory-cash reimbursement.

Revision ID: 0015_cash_reimbursement
Revises: 0014_person_reimbursement
Create Date: 2026-09-01
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0015_cash_reimbursement"
down_revision = "0014_person_reimbursement"
branch_labels = None
depends_on = None


def _function_definition(signature: str) -> str:
    return op.get_bind().execute(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    ).scalar_one()


def _explicit_bank_definition(*, cash_reimbursement_enabled: bool) -> str:
    definition = _function_definition(
        "public.finance_assert_explicit_bank_settlement_0015(uuid)"
    )
    event_pattern = (
        r"('customer_refund'\s*,\s*'expense_cash'\s*,\s*'supplier_payment'\s*,)"
        r"\s*'employee_reimbursement_payment'\s*,"
    )
    branch_pattern = r"""ELSIF target_event\.event_type = 'employee_reimbursement_payment'
          AND COALESCE\(
              target_event\.facts::jsonb #>> '\{details,settlement_method\}', 'bank'
          \) = 'bank' THEN
        uses_bank := true;
        expected_bank_amount := -amount_fen;
    """
    employee_anchor = (
        r"(ELSIF target_event\.event_type = 'employee_reimbursement'\s+"
        r"AND target_event\.facts::jsonb #>> '\{details,paid_now\}' = 'true' THEN)"
    )
    if cash_reimbursement_enabled:
        definition, event_count = re.subn(event_pattern, r"\1", definition, count=1)
        definition, branch_count = re.subn(
            employee_anchor,
            (
                "ELSIF target_event.event_type = 'employee_reimbursement_payment'\n"
                "          AND COALESCE(\n"
                "              target_event.facts::jsonb "
                "#>> '{details,settlement_method}', 'bank'\n"
                "          ) = 'bank' THEN\n"
                "        uses_bank := true;\n"
                "        expected_bank_amount := -amount_fen;\n"
                "    \\1"
            ),
            definition,
            count=1,
        )
    else:
        definition, branch_count = re.subn(branch_pattern, "", definition, count=1)
        restore_pattern = (
            r"('customer_refund'\s*,\s*'expense_cash'\s*,\s*'supplier_payment'\s*,)"
        )
        definition, event_count = re.subn(
            restore_pattern,
            r"\1\n        'employee_reimbursement_payment',",
            definition,
            count=1,
        )
    if event_count != 1 or branch_count != 1:
        raise RuntimeError("CASH_REIMBURSEMENT_EXPLICIT_BANK_VALIDATOR_MISMATCH")
    return definition


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(_explicit_bank_definition(cash_reimbursement_enabled=True))
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    in_use = bind.scalar(
        sa.text(
            """
            SELECT count(*) FROM business_events
             WHERE event_type = 'employee_reimbursement_payment'
               AND facts::jsonb #>> '{details,settlement_method}' = 'cash'
            """
        )
    )
    if in_use:
        raise RuntimeError("CASH_REIMBURSEMENT_SETTLEMENT_IN_USE")
    op.execute(
        sa.text(_explicit_bank_definition(cash_reimbursement_enabled=False))
    )
