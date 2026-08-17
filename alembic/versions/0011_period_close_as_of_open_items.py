"""Version period-close open-item review as an end-of-period snapshot.

Revision ID: 0011_close_as_of_items
Revises: 0010_depreciation_batch
Create Date: 2026-08-18
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0011_close_as_of_items"
down_revision = "0010_depreciation_batch"
branch_labels = None
depends_on = None


_CHECKER_ALLOWLIST_OLD = """               OR target_close.checker_version NOT IN (
                  'accounting_period_close_checker_2026.1',
                  'accounting_period_close_checker_2026.2'
               )"""
_CHECKER_ALLOWLIST_NEW = """               OR target_close.checker_version NOT IN (
                  'accounting_period_close_checker_2026.1',
                  'accounting_period_close_checker_2026.2',
                  'accounting_period_close_checker_2026.3'
               )"""

_OPEN_ITEM_COUNT_OLD = """            SELECT count(*) INTO open_item_count FROM open_items
             WHERE org_id = target_period.org_id AND status IN ('open','partial');"""
_OPEN_ITEM_COUNT_NEW = """            SELECT count(*) INTO open_item_count FROM open_items
             WHERE org_id = target_period.org_id AND status IN ('open','partial');
            IF target_close.checker_version = 'accounting_period_close_checker_2026.3' THEN
                SELECT finance_open_item_count_as_of_0011(
                    target_period.org_id,
                    target_period.end_date
                ) INTO open_item_count;
            END IF;"""

_AS_OF_FUNCTION = r"""
CREATE OR REPLACE FUNCTION finance_open_item_count_as_of_0011(
    target_org_id uuid,
    target_period_end date
) RETURNS bigint
LANGUAGE sql
STABLE
AS $$
    SELECT count(*)
      FROM open_items AS item
      JOIN business_events AS source_event
        ON source_event.org_id = item.org_id
       AND source_event.id = item.source_event_id
      LEFT JOIN business_events AS source_reversal
        ON source_reversal.org_id = source_event.org_id
       AND source_reversal.id = source_event.reversed_by_event_id
     WHERE item.org_id = target_org_id
       AND source_event.posting_date <= target_period_end
       AND source_event.status IN ('posted','reversed')
       AND (
           source_event.reversed_by_event_id IS NULL
           OR source_reversal.id IS NULL
           OR source_reversal.posting_date > target_period_end
       )
       AND item.original_amount_fen > COALESCE((
           SELECT sum(settlement.amount_fen)
             FROM settlements AS settlement
             JOIN business_events AS payment_event
               ON payment_event.org_id = settlement.org_id
              AND payment_event.id = settlement.payment_event_id
             LEFT JOIN business_events AS settlement_reversal
               ON settlement_reversal.org_id = settlement.org_id
              AND settlement_reversal.id = settlement.reversed_by_event_id
            WHERE settlement.org_id = item.org_id
              AND settlement.open_item_id = item.id
              AND payment_event.posting_date <= target_period_end
              AND payment_event.status IN ('posted','reversed')
              AND (
                  settlement.reversed_by_event_id IS NULL
                  OR settlement_reversal.id IS NULL
                  OR settlement_reversal.posting_date > target_period_end
              )
       ), 0);
$$
"""


def _function_definition(signature: str) -> str:
    definition = op.get_bind().scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"PERIOD_CLOSE_FUNCTION_NOT_FOUND:{signature}")
    return definition


def _execute_function(definition: str) -> None:
    op.get_bind().exec_driver_sql(definition.replace("%", "%%"))


def _rewrite_assertion(*, upgrade: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _function_definition("finance_assert_accounting_period_close(uuid)")
    replacements = (
        (
            (_CHECKER_ALLOWLIST_OLD, _CHECKER_ALLOWLIST_NEW),
            (_OPEN_ITEM_COUNT_OLD, _OPEN_ITEM_COUNT_NEW),
        )
        if upgrade
        else (
            (_CHECKER_ALLOWLIST_NEW, _CHECKER_ALLOWLIST_OLD),
            (_OPEN_ITEM_COUNT_NEW, _OPEN_ITEM_COUNT_OLD),
        )
    )
    for old, new in replacements:
        if definition.count(old) != 1:
            raise RuntimeError("PERIOD_CLOSE_AS_OF_FUNCTION_VERSION_MISMATCH")
        definition = definition.replace(old, new, 1)
    _execute_function(definition)


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _execute_function(_AS_OF_FUNCTION)
    _rewrite_assertion(upgrade=True)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    unsafe = op.get_bind().scalar(
        sa.text(
            "SELECT EXISTS (SELECT 1 FROM accounting_period_closes "
            "WHERE checker_version = 'accounting_period_close_checker_2026.3')"
        )
    )
    if unsafe:
        raise RuntimeError("PERIOD_CLOSE_AS_OF_ITEMS_DOWNGRADE_UNSAFE")
    _rewrite_assertion(upgrade=False)
    op.execute("DROP FUNCTION finance_open_item_count_as_of_0011(uuid, date)")
