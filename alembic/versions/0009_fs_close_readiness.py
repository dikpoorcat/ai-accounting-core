"""Make financial-statement readiness a PostgreSQL close invariant.

Revision ID: 0009_fs_close_readiness
Revises: 0008_fs_opening_unique_org
Create Date: 2026-08-31
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0009_fs_close_readiness"
down_revision = "0008_fs_opening_unique_org"
branch_labels = None
depends_on = None

_V5 = "accounting_period_close_checker_2026.5"
_V6 = "accounting_period_close_checker_2026.6"


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
        rf"'{re.escape(_V5)}'(\s*\))",
        f"'{_V5}',\n                '{_V6}'" + r"\1",
        expected_count=4,
    )
    definition = _replace_once(
        definition,
        f"IF target_close.checker_version = '{_V5}'\n"
        "               AND target_period.calendar_month IN (3,6,9,12)",
        f"IF target_close.checker_version IN ('{_V5}', '{_V6}')\n"
        "               AND target_period.calendar_month IN (3,6,9,12)",
    )
    definition = _replace_once(
        definition,
        "                END IF;\n"
        "                expected_system_checks := expected_system_checks || jsonb_build_array(\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_NO_DRAFT_EVENTS',",
        "                END IF;\n"
        f"                IF target_close.checker_version = '{_V6}'\n"
        "                   AND EXISTS (\n"
        "                       SELECT 1 FROM organizations AS organization\n"
        "                        WHERE organization.id = target_period.org_id\n"
        "                          AND organization.filing_cycle = 'quarterly'\n"
        "                          AND organization.accounting_standard = 'small_enterprise'\n"
        "                   ) THEN\n"
        "                    expected_system_checks := expected_system_checks "
        "|| jsonb_build_array(\n"
        "                        jsonb_build_object(\n"
        "                            'code','ACCOUNTING_PERIOD_FINANCIAL_STATEMENT_READY',\n"
        "                            'passed',true,'count',0)\n"
        "                    );\n"
        "                END IF;\n"
        "                expected_system_checks := expected_system_checks || jsonb_build_array(\n"
        "                    jsonb_build_object(\n"
        "                        'code','ACCOUNTING_PERIOD_NO_DRAFT_EVENTS',",
    )
    definition = _replace_once(
        definition,
        "            IF NOT finance_text_is_canonical_jsonb(target_close.calculation_payload)",
        f"            IF target_close.checker_version = '{_V6}' AND (\n"
        "                jsonb_typeof(\n"
        "                    target_close.calculation::jsonb -> "
        "'financial_statement_requirements'\n"
        "                ) <> 'array'\n"
        "                OR target_close.calculation::jsonb -> 'financial_statement_requirements'\n"
        "                   <> '[]'::jsonb\n"
        "            ) THEN\n"
        "                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_BLOCKED';\n"
        "            END IF;\n\n"
        "            IF NOT finance_text_is_canonical_jsonb(target_close.calculation_payload)",
    )
    _install(definition)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition = _function_definition()
    definition = _replace_regex_count(
        definition,
        rf"'{re.escape(_V5)}'\s*,\s*'{re.escape(_V6)}'(\s*\))",
        f"'{_V5}'" + r"\1",
        expected_count=4,
    )
    definition = _replace_once(
        definition,
        f"IF target_close.checker_version IN ('{_V5}', '{_V6}')\n"
        "               AND target_period.calendar_month IN (3,6,9,12)",
        f"IF target_close.checker_version = '{_V5}'\n"
        "               AND target_period.calendar_month IN (3,6,9,12)",
    )
    definition = _replace_once(
        definition,
        f"                IF target_close.checker_version = '{_V6}'\n"
        "                   AND EXISTS (\n"
        "                       SELECT 1 FROM organizations AS organization\n"
        "                        WHERE organization.id = target_period.org_id\n"
        "                          AND organization.filing_cycle = 'quarterly'\n"
        "                          AND organization.accounting_standard = 'small_enterprise'\n"
        "                   ) THEN\n"
        "                    expected_system_checks := expected_system_checks "
        "|| jsonb_build_array(\n"
        "                        jsonb_build_object(\n"
        "                            'code','ACCOUNTING_PERIOD_FINANCIAL_STATEMENT_READY',\n"
        "                            'passed',true,'count',0)\n"
        "                    );\n"
        "                END IF;\n",
        "",
    )
    definition = _replace_once(
        definition,
        f"            IF target_close.checker_version = '{_V6}' AND (\n"
        "                jsonb_typeof(\n"
        "                    target_close.calculation::jsonb -> "
        "'financial_statement_requirements'\n"
        "                ) <> 'array'\n"
        "                OR target_close.calculation::jsonb -> 'financial_statement_requirements'\n"
        "                   <> '[]'::jsonb\n"
        "            ) THEN\n"
        "                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_BLOCKED';\n"
        "            END IF;\n\n",
        "",
    )
    _install(definition)
