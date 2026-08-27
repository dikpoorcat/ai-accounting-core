"""Allow versioned management commentary fields in period-close actions.

Revision ID: 0030_commentary_action
Revises: 0029_close_commentary
Create Date: 2026-08-27
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0030_commentary_action"
down_revision = "0029_close_commentary"
branch_labels = None
depends_on = None


_OLD_CLOSE_KEYS = re.compile(
    r"<> ARRAY\['calculation_hash','closing_date','confirmation_note',\s*"
    r"'evidence_references','idempotency_key','org_id',\s*'owner_approval_id',\s*"
    r"'period_id','review_facts'\]"
)
_OLD_CLOSE_KEYS_TEXT = (
    "<> ARRAY['calculation_hash','closing_date','confirmation_note',\n"
    "                        'evidence_references','idempotency_key','org_id',\n"
    "                        'owner_approval_id',\n"
    "                        'period_id','review_facts']"
)
_NEW_CLOSE_KEYS = (
    "<> ARRAY['calculation_hash','closing_date','confirmation_note',\n"
    "                        'evidence_references','idempotency_key',\n"
    "                        'management_commentary',\n"
    "                        'management_commentary_context_hash','org_id',\n"
    "                        'owner_approval_id',\n"
    "                        'period_id','review_facts']"
)
_NEW_CLOSE_KEYS_PATTERN = re.compile(
    r"<> ARRAY\['calculation_hash','closing_date','confirmation_note',\s*"
    r"'evidence_references','idempotency_key',\s*'management_commentary',\s*"
    r"'management_commentary_context_hash','org_id',\s*'owner_approval_id',\s*"
    r"'period_id','review_facts'\]"
)
_OLD_FAILURE_FIELDS = re.compile(
    r"'idempotency_key','confirmation_note','evidence_references','calculation_hash',\s*"
    r"'owner_approval_id',"
)
_OLD_FAILURE_FIELDS_TEXT = (
    "'idempotency_key','confirmation_note','evidence_references','calculation_hash',\n"
    "                    'owner_approval_id',"
)
_NEW_FAILURE_FIELDS = (
    "'idempotency_key','confirmation_note','evidence_references','calculation_hash',\n"
    "                    'management_commentary_context_hash',\n"
    "                    'management_commentary','owner_approval_id',"
)
_NEW_FAILURE_FIELDS_PATTERN = re.compile(
    r"'idempotency_key','confirmation_note','evidence_references','calculation_hash',\s*"
    r"'management_commentary_context_hash',\s*'management_commentary',"
    r"'owner_approval_id',"
)


def _rewrite_action_invariant(*, upgrade: bool) -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    definition = bind.scalar(
        sa.text(
            "SELECT pg_get_functiondef("
            "'finance_assert_accounting_period_action(uuid)'::regprocedure)"
        )
    )
    if not isinstance(definition, str):
        raise RuntimeError("ACCOUNTING_PERIOD_ACTION_INVARIANT_NOT_FOUND")
    if upgrade:
        replacements = (
            (_OLD_CLOSE_KEYS, _NEW_CLOSE_KEYS_PATTERN, _NEW_CLOSE_KEYS, 1),
            (_OLD_FAILURE_FIELDS, _NEW_FAILURE_FIELDS_PATTERN, _NEW_FAILURE_FIELDS, 2),
        )
    else:
        replacements = (
            (
                _NEW_FAILURE_FIELDS_PATTERN,
                _OLD_FAILURE_FIELDS,
                _OLD_FAILURE_FIELDS_TEXT,
                2,
            ),
            (_NEW_CLOSE_KEYS_PATTERN, _OLD_CLOSE_KEYS, _OLD_CLOSE_KEYS_TEXT, 1),
        )
    for old_pattern, new_pattern, replacement, expected_count in replacements:
        if len(new_pattern.findall(definition)) == expected_count:
            continue
        if len(old_pattern.findall(definition)) != expected_count:
            raise RuntimeError("ACCOUNTING_PERIOD_ACTION_INVARIANT_VERSION_MISMATCH")
        definition, changed = old_pattern.subn(replacement, definition)
        if changed != expected_count:
            raise RuntimeError("ACCOUNTING_PERIOD_ACTION_INVARIANT_REWRITE_FAILED")
    bind.exec_driver_sql(definition.replace("%", "%%"))


def upgrade() -> None:
    _rewrite_action_invariant(upgrade=True)


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql" and bind.scalar(
        sa.text(
            "SELECT EXISTS ("
            "SELECT 1 FROM accounting_period_actions "
            "WHERE action_type = 'period_close' "
            "AND input_facts::jsonb ?| ARRAY["
            "'management_commentary','management_commentary_context_hash'])"
        )
    ):
        raise RuntimeError("ACCOUNTING_PERIOD_COMMENTARY_ACTION_DOWNGRADE_UNSAFE")
    _rewrite_action_invariant(upgrade=False)
