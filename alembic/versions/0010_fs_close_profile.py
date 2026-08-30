"""Use the effective organization profile in the report close gate.

Revision ID: 0010_fs_close_profile
Revises: 0009_fs_close_readiness
Create Date: 2026-08-31
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0010_fs_close_profile"
down_revision = "0009_fs_close_readiness"
branch_labels = None
depends_on = None

_ROOT_PROFILE_PATTERN = (
    r"EXISTS\s*\(\s*"
    r"SELECT 1 FROM organizations AS organization\s*"
    r"WHERE organization\.id = target_period\.org_id\s*"
    r"AND organization\.filing_cycle = 'quarterly'\s*"
    r"AND organization\.accounting_standard = 'small_enterprise'\s*"
    r"\)"
)
_ROOT_PROFILE = (
    "EXISTS (\n"
    "                   SELECT 1 FROM organizations AS organization\n"
    "                    WHERE organization.id = target_period.org_id\n"
    "                      AND organization.filing_cycle = 'quarterly'\n"
    "                      AND organization.accounting_standard = 'small_enterprise'\n"
    "               )"
)
_EFFECTIVE_PROFILE = (
    "COALESCE((\n"
    "                   SELECT profile.filing_cycle\n"
    "                     FROM organization_profile_versions AS profile\n"
    "                    WHERE profile.org_id = target_period.org_id\n"
    "                      AND profile.effective_from <= target_period.end_date\n"
    "                    ORDER BY profile.effective_from DESC\n"
    "                    LIMIT 1\n"
    "               ), (\n"
    "                   SELECT organization.filing_cycle\n"
    "                     FROM organizations AS organization\n"
    "                    WHERE organization.id = target_period.org_id\n"
    "               )) = 'quarterly'\n"
    "               AND COALESCE((\n"
    "                   SELECT profile.accounting_standard\n"
    "                     FROM organization_profile_versions AS profile\n"
    "                    WHERE profile.org_id = target_period.org_id\n"
    "                      AND profile.effective_from <= target_period.end_date\n"
    "                    ORDER BY profile.effective_from DESC\n"
    "                    LIMIT 1\n"
    "               ), (\n"
    "                   SELECT organization.accounting_standard\n"
    "                     FROM organizations AS organization\n"
    "                    WHERE organization.id = target_period.org_id\n"
    "               )) = 'small_enterprise'"
)


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
    definition, count = re.subn(
        _ROOT_PROFILE_PATTERN,
        _EFFECTIVE_PROFILE,
        _function_definition(),
    )
    if count != 2:
        raise RuntimeError("ACCOUNTING_PERIOD_CLOSE_PROFILE_VALIDATOR_MISMATCH")
    _install(definition)


def downgrade() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    definition, count = re.subn(
        re.escape(_EFFECTIVE_PROFILE),
        _ROOT_PROFILE,
        _function_definition(),
    )
    if count != 2:
        raise RuntimeError("ACCOUNTING_PERIOD_CLOSE_PROFILE_VALIDATOR_MISMATCH")
    _install(definition)
