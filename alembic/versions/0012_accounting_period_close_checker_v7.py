"""Accept accounting-period close checker v7 snapshots.

Revision ID: 0012_close_checker_v7
Revises: 0011_expense_recovery_received
Create Date: 2026-08-31
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0012_close_checker_v7"
down_revision = "0011_expense_recovery_received"
branch_labels = None
depends_on = None

_V5 = "accounting_period_close_checker_2026.5"
_V6 = "accounting_period_close_checker_2026.6"
_V7 = "accounting_period_close_checker_2026.7"


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("ACCOUNTING_PERIOD_CLOSE_VALIDATOR_VERSION_MISMATCH")
    return source.replace(old, new, 1)


def _replace_regex_count(
    source: str,
    pattern: str,
    replacement: str,
    *,
    expected_count: int,
) -> str:
    result, count = re.subn(pattern, replacement, source)
    if count != expected_count:
        raise RuntimeError("ACCOUNTING_PERIOD_CLOSE_VALIDATOR_VERSION_MISMATCH")
    return result


def _function_definition() -> str:
    return op.get_bind().execute(
        sa.text(
            "SELECT pg_get_functiondef("
            "'public.finance_assert_accounting_period_close(uuid)'::regprocedure)"
        )
    ).scalar_one()


def _install(definition: str) -> None:
    op.execute(sa.text(definition))


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _function_definition()
    definition = _replace_regex_count(
        definition,
        rf"'{re.escape(_V5)}'\s*,\s*'{re.escape(_V6)}'(\s*\))",
        f"'{_V5}', '{_V6}', '{_V7}'" + r"\1",
        expected_count=5,
    )
    definition = _replace_once(
        definition,
        f"IF target_close.checker_version = '{_V6}'\n"
        "                   AND COALESCE((",
        f"IF target_close.checker_version IN ('{_V6}', '{_V7}')\n"
        "                   AND COALESCE((",
    )
    definition = _replace_once(
        definition,
        f"IF target_close.checker_version = '{_V6}' AND (\n",
        f"IF target_close.checker_version IN ('{_V6}', '{_V7}') AND (\n",
    )
    _install(definition)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _function_definition()
    definition = _replace_regex_count(
        definition,
        rf"'{re.escape(_V5)}'\s*,\s*'{re.escape(_V6)}'\s*,\s*"
        rf"'{re.escape(_V7)}'(\s*\))",
        f"'{_V5}', '{_V6}'" + r"\1",
        expected_count=5,
    )
    definition = _replace_once(
        definition,
        f"IF target_close.checker_version IN ('{_V6}', '{_V7}')\n"
        "                   AND COALESCE((",
        f"IF target_close.checker_version = '{_V6}'\n"
        "                   AND COALESCE((",
    )
    definition = _replace_once(
        definition,
        f"IF target_close.checker_version IN ('{_V6}', '{_V7}') AND (\n",
        f"IF target_close.checker_version = '{_V6}' AND (\n",
    )
    _install(definition)
