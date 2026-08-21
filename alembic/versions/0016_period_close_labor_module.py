"""Version period-close snapshots for the labor-remuneration module.

Revision ID: 0016_close_labor_module
Revises: 0015_labor_final_events
Create Date: 2026-08-22
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0016_close_labor_module"
down_revision = "0015_labor_final_events"
branch_labels = None
depends_on = None


_CHECKER_ALLOWLIST_OLD = """               OR target_close.checker_version NOT IN (
                  'accounting_period_close_checker_2026.1',
                  'accounting_period_close_checker_2026.2',
                  'accounting_period_close_checker_2026.3'
               )"""
_CHECKER_ALLOWLIST_NEW = """               OR target_close.checker_version NOT IN (
                  'accounting_period_close_checker_2026.1',
                  'accounting_period_close_checker_2026.2',
                  'accounting_period_close_checker_2026.3',
                  'accounting_period_close_checker_2026.4'
               )"""

_DECLARATION_OLD = """        DECLARE unfinished_payroll bigint;
        DECLARE open_item_count bigint;"""
_DECLARATION_NEW = """        DECLARE unfinished_payroll bigint;
        DECLARE unfinished_labor bigint;
        DECLARE open_item_count bigint;"""

_UNFINISHED_MODULES_OLD = """            SELECT count(*) INTO unfinished_payroll FROM \
payroll_batches
             WHERE org_id = target_period.org_id
               AND payroll_period = to_char(target_period.start_date, 'YYYY-MM')
               AND status NOT IN ('posted','reversed','superseded');
            IF fixed_missing <> 0 OR intangible_missing <> 0
               OR borrowing_missing <> 0 OR unfinished_payroll <> 0 THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_BLOCKED';
            END IF;"""
_UNFINISHED_MODULES_NEW = """            SELECT count(*) INTO unfinished_payroll FROM \
payroll_batches
             WHERE org_id = target_period.org_id
               AND payroll_period = to_char(target_period.start_date, 'YYYY-MM')
               AND status NOT IN ('posted','reversed','superseded');
            unfinished_labor := 0;
            IF target_close.checker_version = 'accounting_period_close_checker_2026.4' THEN
                SELECT (
                    (SELECT count(*) FROM labor_remuneration_batches AS batch
                      WHERE batch.org_id = target_period.org_id
                        AND batch.remuneration_period =
                            to_char(target_period.start_date, 'YYYY-MM')
                        AND batch.status = 'calculated')
                    +
                    (SELECT count(*) FROM unified_payout_runs AS payout
                      WHERE payout.org_id = target_period.org_id
                        AND payout.posting_date BETWEEN
                            target_period.start_date AND target_period.end_date
                        AND payout.status = 'calculated')
                ) INTO unfinished_labor;
            END IF;
            IF fixed_missing <> 0 OR intangible_missing <> 0
               OR borrowing_missing <> 0 OR unfinished_payroll <> 0
               OR unfinished_labor <> 0 THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_BLOCKED';
            END IF;"""

_OPEN_ITEM_VERSION_OLD = """            IF target_close.checker_version = \
'accounting_period_close_checker_2026.3' THEN
                SELECT finance_open_item_count_as_of_0011(
                    target_period.org_id,
                    target_period.end_date
                ) INTO open_item_count;
            END IF;"""
_OPEN_ITEM_VERSION_NEW = """            IF target_close.checker_version IN (
                'accounting_period_close_checker_2026.3',
                'accounting_period_close_checker_2026.4'
            ) THEN
                SELECT finance_open_item_count_as_of_0011(
                    target_period.org_id,
                    target_period.end_date
                ) INTO open_item_count;
            END IF;"""

_MODULE_CHECKS_OLD = """            expected_module_checks := jsonb_build_object(
                'borrowings', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_BORROWING_INTEREST_PENDING',
                    'count',borrowing_missing,'blocking',false),
                'fixed_assets', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_FIXED_ASSET_DEPRECIATION_PENDING',
                    'count',fixed_missing,'blocking',false),
                'intangible_assets', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_INTANGIBLE_AMORTIZATION_PENDING',
                    'count',intangible_missing,'blocking',false),
                'payroll', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_PAYROLL_PENDING',
                    'count',unfinished_payroll,'blocking',false)
            );"""
_MODULE_CHECKS_NEW = """            expected_module_checks := jsonb_build_object(
                'borrowings', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_BORROWING_INTEREST_PENDING',
                    'count',borrowing_missing,'blocking',false),
                'fixed_assets', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_FIXED_ASSET_DEPRECIATION_PENDING',
                    'count',fixed_missing,'blocking',false),
                'intangible_assets', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_INTANGIBLE_AMORTIZATION_PENDING',
                    'count',intangible_missing,'blocking',false),
                'payroll', jsonb_build_object(
                    'code','ACCOUNTING_PERIOD_PAYROLL_PENDING',
                    'count',unfinished_payroll,'blocking',false)
            );
            IF target_close.checker_version = 'accounting_period_close_checker_2026.4' THEN
                expected_module_checks := expected_module_checks || jsonb_build_object(
                    'labor_remuneration', jsonb_build_object(
                        'code','ACCOUNTING_PERIOD_LABOR_REMUNERATION_PENDING',
                        'count',unfinished_labor,'blocking',false)
                );
            END IF;"""


def _function_definition() -> str:
    definition = op.get_bind().scalar(
        sa.text(
            "SELECT pg_get_functiondef("
            "'finance_assert_accounting_period_close(uuid)'::regprocedure)"
        )
    )
    if not isinstance(definition, str):
        raise RuntimeError("PERIOD_CLOSE_FUNCTION_NOT_FOUND")
    return definition


def _rewrite_assertion(*, upgrade: bool) -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _function_definition()
    replacements = (
        (
            (_CHECKER_ALLOWLIST_OLD, _CHECKER_ALLOWLIST_NEW),
            (_DECLARATION_OLD, _DECLARATION_NEW),
            (_UNFINISHED_MODULES_OLD, _UNFINISHED_MODULES_NEW),
            (_OPEN_ITEM_VERSION_OLD, _OPEN_ITEM_VERSION_NEW),
            (_MODULE_CHECKS_OLD, _MODULE_CHECKS_NEW),
        )
        if upgrade
        else (
            (_CHECKER_ALLOWLIST_NEW, _CHECKER_ALLOWLIST_OLD),
            (_DECLARATION_NEW, _DECLARATION_OLD),
            (_UNFINISHED_MODULES_NEW, _UNFINISHED_MODULES_OLD),
            (_OPEN_ITEM_VERSION_NEW, _OPEN_ITEM_VERSION_OLD),
            (_MODULE_CHECKS_NEW, _MODULE_CHECKS_OLD),
        )
    )
    for old, new in replacements:
        if definition.count(old) != 1:
            raise RuntimeError("PERIOD_CLOSE_LABOR_FUNCTION_VERSION_MISMATCH")
        definition = definition.replace(old, new, 1)
    op.get_bind().exec_driver_sql(definition.replace("%", "%%"))


def upgrade() -> None:
    _rewrite_assertion(upgrade=True)


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        unsafe = op.get_bind().scalar(
            sa.text(
                "SELECT EXISTS (SELECT 1 FROM accounting_period_closes "
                "WHERE checker_version = 'accounting_period_close_checker_2026.4')"
            )
        )
        if unsafe:
            raise RuntimeError("PERIOD_CLOSE_LABOR_MODULE_DOWNGRADE_UNSAFE")
    _rewrite_assertion(upgrade=False)
