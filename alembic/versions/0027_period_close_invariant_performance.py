"""Avoid repeated full-history validation while sealing an accounting period.

Revision ID: 0027_period_close_perf
Revises: 0026_salary_petty_recovery
Create Date: 2026-08-26
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0027_period_close_perf"
down_revision = "0026_salary_petty_recovery"
branch_labels = None
depends_on = None


_LIFECYCLE_REPLAY_OLD = """            PERFORM finance_assert_fixed_asset(asset.id)
              FROM fixed_assets AS asset
             WHERE asset.org_id = target_period.org_id;
            PERFORM finance_assert_intangible_asset(asset.id)
              FROM intangible_assets AS asset
             WHERE asset.org_id = target_period.org_id;
"""

_LIFECYCLE_REPLAY_NEW = """
            -- Asset mutations are already protected by module-level deferred
            -- their own deferred lifecycle invariants and final rows are immutable.
            -- Period close only rechecks month-specific completion below; replaying
            -- every asset's entire history here made close time grow quadratically.
"""

_SOURCE_INSERT_GUARD_OLD = r"""
CREATE OR REPLACE FUNCTION finance_guard_accounting_period_close_source_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_status varchar;
BEGIN
    SELECT event.status INTO current_status
      FROM vouchers AS voucher
      JOIN business_events AS event
        ON event.org_id = voucher.org_id AND event.id = voucher.event_id
     WHERE voucher.org_id = NEW.org_id AND voucher.id = NEW.voucher_id
       AND event.id = NEW.event_id;
    IF current_status IS NULL OR current_status <> NEW.event_status_at_close THEN
        RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$
"""

_SOURCE_INSERT_GUARD_NEW = r"""
CREATE OR REPLACE FUNCTION finance_guard_accounting_period_close_source_insert()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    current_status varchar;
    close_xmin xid;
BEGIN
    SELECT close.xmin INTO close_xmin
      FROM accounting_period_closes AS close
     WHERE close.org_id = NEW.org_id AND close.id = NEW.close_id;
    IF NOT FOUND OR NOT finance_parent_xmin_is_current_0015(close_xmin) THEN
        RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_ALREADY_SEALED';
    END IF;
    SELECT event.status INTO current_status
      FROM vouchers AS voucher
      JOIN business_events AS event
        ON event.org_id = voucher.org_id AND event.id = voucher.event_id
     WHERE voucher.org_id = NEW.org_id AND voucher.id = NEW.voucher_id
       AND event.id = NEW.event_id;
    IF current_status IS NULL OR current_status <> NEW.event_status_at_close THEN
        RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$
"""

_SOURCE_DEFERRED_VALIDATOR_OLD = r"""
CREATE OR REPLACE FUNCTION finance_validate_accounting_period_close_source()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF TG_OP IN ('UPDATE','DELETE') THEN
        PERFORM finance_assert_accounting_period_close(OLD.close_id);
    END IF;
    IF TG_OP IN ('INSERT','UPDATE') THEN
        PERFORM finance_assert_accounting_period_close(NEW.close_id);
    END IF;
    RETURN NULL;
END;
$$
"""

_SOURCE_DEFERRED_VALIDATOR_NEW = r"""
CREATE OR REPLACE FUNCTION finance_validate_accounting_period_close_source()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    close_xmin xid;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
    END IF;
    SELECT close.xmin INTO close_xmin
      FROM accounting_period_closes AS close
     WHERE close.org_id = NEW.org_id AND close.id = NEW.close_id;
    IF NOT FOUND OR NOT finance_parent_xmin_is_current_0015(close_xmin) THEN
        RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_ALREADY_SEALED';
    END IF;
    -- The deferred root-close trigger validates the complete, final source
    -- set once. Re-running that aggregate assertion for every immutable
    -- source row made one close O(voucher_count * full_snapshot_cost).
    RETURN NULL;
END;
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


def _rewrite_close_assertion(*, upgrade: bool) -> None:
    definition = _function_definition("finance_assert_accounting_period_close(uuid)")
    old, new = (
        (_LIFECYCLE_REPLAY_OLD, _LIFECYCLE_REPLAY_NEW)
        if upgrade
        else (_LIFECYCLE_REPLAY_NEW, _LIFECYCLE_REPLAY_OLD)
    )
    if definition.count(old) != 1:
        raise RuntimeError("PERIOD_CLOSE_PERFORMANCE_FUNCTION_VERSION_MISMATCH")
    _execute_function(definition.replace(old, new, 1))


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _rewrite_close_assertion(upgrade=True)
    _execute_function(_SOURCE_INSERT_GUARD_NEW)
    _execute_function(_SOURCE_DEFERRED_VALIDATOR_NEW)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    _execute_function(_SOURCE_DEFERRED_VALIDATOR_OLD)
    _execute_function(_SOURCE_INSERT_GUARD_OLD)
    _rewrite_close_assertion(upgrade=False)
