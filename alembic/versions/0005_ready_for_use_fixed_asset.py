"""Allow one-event acquisition of a fixed asset that is already ready for use.

Revision ID: 0005_ready_fixed_asset
Revises: 0004_close_approval_width
Create Date: 2026-08-17
"""

# ruff: noqa: E501 -- exact PostgreSQL function fragments intentionally preserve layout.

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0005_ready_fixed_asset"
down_revision = "0004_close_approval_width"
branch_labels = None
depends_on = None


_DECLARATION_OLD = """        DECLARE expected_loss bigint;
        BEGIN"""
_DECLARATION_NEW = """        DECLARE expected_loss bigint;
        DECLARE direct_ready_for_use boolean;
        BEGIN"""

_ACQUISITION_START_OLD = """                SELECT * INTO asset FROM fixed_assets WHERE acquisition_event_id = target_event.id;
                IF NOT FOUND OR asset.org_id <> target_event.org_id"""
_ACQUISITION_START_NEW = """                SELECT * INTO asset FROM fixed_assets WHERE acquisition_event_id = target_event.id;
                direct_ready_for_use := COALESCE(
                    target_event.facts::jsonb -> 'ready_for_use' <> 'null'::jsonb,
                    FALSE
                );
                IF NOT FOUND OR asset.org_id <> target_event.org_id"""

_ACQUISITION_ACTIVATION_OLD = """                   OR asset.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR EXISTS (SELECT 1 FROM fixed_asset_activations WHERE event_id = target_event.id)"""
_ACQUISITION_ACTIVATION_NEW = """                   OR asset.accounting_rule_source_url <> 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   OR (direct_ready_for_use AND NOT EXISTS (
                       SELECT 1 FROM fixed_asset_activations AS ready
                        WHERE ready.event_id = target_event.id
                          AND ready.org_id = target_event.org_id
                          AND ready.asset_id = asset.id
                          AND ready.posting_date = target_event.posting_date
                          AND to_char(ready.in_service_date, 'YYYY-MM-DD')
                              = target_event.facts::jsonb #>> '{ready_for_use,in_service_date}'
                          AND ready.depreciation_method
                              = target_event.facts::jsonb #>> '{ready_for_use,depreciation_method}'
                          AND ready.useful_life_months::text
                              = target_event.facts::jsonb #>> '{ready_for_use,useful_life_months}'
                          AND ready.residual_value_fen::text
                              = target_event.facts::jsonb #>> '{ready_for_use,residual_value_fen}'
                          AND ready.benefit_area
                              = target_event.facts::jsonb #>> '{ready_for_use,benefit_area}'
                          AND ready.accounting_rule_version
                              = 'small_enterprise_fixed_asset_straight_line_2013.1'
                          AND ready.accounting_rule_source_url
                              = 'https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf'
                   ))
                   OR (NOT direct_ready_for_use AND EXISTS (
                       SELECT 1 FROM fixed_asset_activations
                        WHERE event_id = target_event.id
                   ))"""

_ALLOWED_ROLES_OLD = """                           'fixed_asset_pending', 'bank', 'accounts_payable',
                           'employee_payable'"""
_ALLOWED_ROLES_NEW = """                           'fixed_asset_pending', 'fixed_asset_cost', 'bank',
                           'accounts_payable', 'employee_payable'"""

_ACQUISITION_DEBIT_OLD = """                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'debit') <> asset.cost_fen
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'credit') <> 0"""
_ACQUISITION_DEBIT_NEW = """                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'debit')
                      <> (CASE WHEN direct_ready_for_use THEN 0 ELSE asset.cost_fen END)
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_pending', 'credit') <> 0
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'debit')
                      <> (CASE WHEN direct_ready_for_use THEN asset.cost_fen ELSE 0 END)
                   OR finance_asset_role_amount(target_voucher.id, 'fixed_asset_cost', 'credit') <> 0"""

_FACT_COUNT_OLD = """                IF fact_count <> 1 THEN
                    RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
                END IF;"""
_FACT_COUNT_NEW = """                IF (
                    target_event.event_type = 'fixed_asset_acquisition'
                    AND COALESCE(
                        target_event.facts::jsonb -> 'ready_for_use' <> 'null'::jsonb,
                        FALSE
                    )
                    AND fact_count <> 2
                ) OR (
                    NOT (
                        target_event.event_type = 'fixed_asset_acquisition'
                        AND COALESCE(
                            target_event.facts::jsonb -> 'ready_for_use' <> 'null'::jsonb,
                            FALSE
                        )
                    )
                    AND fact_count <> 1
                ) THEN
                    RAISE EXCEPTION 'FIXED_ASSET_EVENT_FACT_SHAPE_INVALID';
                END IF;"""


def _function_definition(signature: str) -> str:
    definition = op.get_bind().scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:signature AS regprocedure))"),
        {"signature": signature},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"READY_FIXED_ASSET_FUNCTION_NOT_FOUND:{signature}")
    return definition


def _replace_once(definition: str, old: str, new: str, *, signature: str) -> str:
    if new in definition:
        return definition
    if definition.count(old) != 1:
        raise RuntimeError(f"READY_FIXED_ASSET_FUNCTION_VERSION_MISMATCH:{signature}")
    return definition.replace(old, new, 1)


def _rewrite_event_shape(signature: str, *, upgrade: bool) -> None:
    definition = _function_definition(signature)
    replacements = (
        (_DECLARATION_OLD, _DECLARATION_NEW),
        (_ACQUISITION_START_OLD, _ACQUISITION_START_NEW),
        (_ACQUISITION_ACTIVATION_OLD, _ACQUISITION_ACTIVATION_NEW),
        (_ALLOWED_ROLES_OLD, _ALLOWED_ROLES_NEW),
        (_ACQUISITION_DEBIT_OLD, _ACQUISITION_DEBIT_NEW),
    )
    if not upgrade:
        replacements = tuple((new, old) for old, new in reversed(replacements))
    original = definition
    for old, new in replacements:
        definition = _replace_once(definition, old, new, signature=signature)
    if definition != original:
        op.get_bind().exec_driver_sql(definition.replace("%", "%%"))


def _rewrite_event_fact_count(*, upgrade: bool) -> None:
    signature = "finance_assert_fixed_asset_from_event(uuid)"
    definition = _function_definition(signature)
    old, new = (
        (_FACT_COUNT_OLD, _FACT_COUNT_NEW)
        if upgrade
        else (_FACT_COUNT_NEW, _FACT_COUNT_OLD)
    )
    rewritten = _replace_once(definition, old, new, signature=signature)
    if rewritten != definition:
        op.get_bind().exec_driver_sql(rewritten.replace("%", "%%"))


def _assert_current_fixed_assets() -> None:
    op.get_bind().execute(
        sa.text(
            "SELECT finance_assert_fixed_asset_from_event(id) FROM business_events "
            "WHERE status IN ('posted','reversed') AND event_type LIKE 'fixed_asset_%'"
        )
    )


def upgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    for signature in (
        "finance_assert_fixed_asset_event_shape(uuid)",
        "finance_assert_fixed_asset_event_shape_0014(uuid)",
    ):
        _rewrite_event_shape(signature, upgrade=True)
    _rewrite_event_fact_count(upgrade=True)
    _assert_current_fixed_assets()


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    unsafe = op.get_bind().scalar(
        sa.text(
            """
            SELECT EXISTS (
                SELECT 1
                  FROM fixed_assets AS asset
                  JOIN fixed_asset_activations AS activation
                    ON activation.org_id = asset.org_id
                   AND activation.asset_id = asset.id
                   AND activation.event_id = asset.acquisition_event_id
            )
            """
        )
    )
    if unsafe:
        raise RuntimeError("READY_FIXED_ASSET_DOWNGRADE_UNSAFE")
    _rewrite_event_fact_count(upgrade=False)
    for signature in (
        "finance_assert_fixed_asset_event_shape_0014(uuid)",
        "finance_assert_fixed_asset_event_shape(uuid)",
    ):
        _rewrite_event_shape(signature, upgrade=False)
