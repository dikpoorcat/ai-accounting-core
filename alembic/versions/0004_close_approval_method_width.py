"""Repair owner close approval storage and PostgreSQL invariants.

Revision ID: 0004_close_approval_width
Revises: 0003_owner_close_approval
Create Date: 2026-08-17
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0004_close_approval_width"
down_revision = "0003_owner_close_approval"
branch_labels = None
depends_on = None


_OLD_CLOSE_KEYS = re.compile(
    r"<> ARRAY\['calculation_hash','closing_date','confirmation_note',\s*"
    r"'evidence_references','idempotency_key','org_id','period_id','review_facts'\]"
)
_OLD_CLOSE_KEYS_TEXT = (
    "<> ARRAY['calculation_hash','closing_date','confirmation_note',\n"
    "                        'evidence_references','idempotency_key','org_id',\n"
    "                        'period_id','review_facts']"
)
_NEW_CLOSE_KEYS = (
    "<> ARRAY['calculation_hash','closing_date','confirmation_note',\n"
    "                        'evidence_references','idempotency_key','org_id',\n"
    "                        'owner_approval_id',\n"
    "                        'period_id','review_facts']"
)
_NEW_CLOSE_KEYS_PATTERN = re.compile(
    r"<> ARRAY\['calculation_hash','closing_date','confirmation_note',\s*"
    r"'evidence_references','idempotency_key','org_id',\s*'owner_approval_id',\s*"
    r"'period_id','review_facts'\]"
)
_OLD_FAILURE_FIELDS = re.compile(
    r"'idempotency_key','confirmation_note',\s*'evidence_references',\s*"
    r"'calculation_hash',"
)
_OLD_FAILURE_FIELDS_TEXT = (
    "'idempotency_key','confirmation_note','evidence_references','calculation_hash',"
)
_NEW_FAILURE_FIELDS = (
    "'idempotency_key','confirmation_note','evidence_references','calculation_hash',\n"
    "                    'owner_approval_id',"
)
_NEW_FAILURE_FIELDS_PATTERN = re.compile(
    r"'idempotency_key','confirmation_note',\s*'evidence_references',\s*"
    r"'calculation_hash',\s*'owner_approval_id',"
)


def _replace_action_invariant(*, upgrade: bool) -> None:
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


def _install_owner_approval_invariants() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            CREATE OR REPLACE FUNCTION finance_assert_period_close_owner_approval(
                target_close_id uuid
            ) RETURNS void
            LANGUAGE plpgsql
            AS $$
            DECLARE target_close accounting_period_closes%ROWTYPE;
            DECLARE target_action accounting_period_actions%ROWTYPE;
            DECLARE owner_exists boolean;
            BEGIN
                SELECT * INTO target_close
                  FROM accounting_period_closes WHERE id = target_close_id;
                IF NOT FOUND THEN RETURN; END IF;
                SELECT * INTO target_action
                  FROM accounting_period_actions
                 WHERE org_id = target_close.org_id AND id = target_close.action_id;
                SELECT EXISTS (
                    SELECT 1 FROM owner_accounts WHERE org_id = target_close.org_id
                ) INTO owner_exists;
                IF target_action.id IS NULL
                   OR target_action.input_facts::jsonb ->> 'owner_approval_id'
                        IS DISTINCT FROM target_close.owner_approval_id::text
                   OR (owner_exists AND (
                        target_close.owner_approval_id IS NULL
                        OR NOT EXISTS (
                            SELECT 1
                              FROM accounting_period_close_approvals AS approval
                             WHERE approval.org_id = target_close.org_id
                               AND approval.id = target_close.owner_approval_id
                               AND approval.period_id = target_close.period_id
                               AND approval.calculation_hash = target_close.calculation_hash
                               AND approval.confirmation_method =
                                   'local_password_reauthentication'
                               AND approval.consumed_at IS NOT NULL
                               AND approval.confirmed_at <= approval.consumed_at
                               AND approval.consumed_at <= target_close.confirmed_at
                               AND approval.expires_at >= target_close.confirmed_at
                        )
                   ))
                   OR (NOT owner_exists AND target_close.owner_approval_id IS NOT NULL)
                THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_OWNER_APPROVAL_INVALID';
                END IF;
            END;
            $$;

            CREATE OR REPLACE FUNCTION finance_validate_period_close_owner_approval()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                IF TG_OP IN ('UPDATE','DELETE') THEN
                    PERFORM finance_assert_period_close_owner_approval(OLD.id);
                END IF;
                IF TG_OP IN ('INSERT','UPDATE') THEN
                    PERFORM finance_assert_period_close_owner_approval(NEW.id);
                END IF;
                RETURN NULL;
            END;
            $$;

            CREATE OR REPLACE FUNCTION finance_validate_period_close_approval_usage()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            DECLARE linked_close_id uuid;
            BEGIN
                FOR linked_close_id IN
                    SELECT close.id
                      FROM accounting_period_closes AS close
                     WHERE close.owner_approval_id IN (OLD.id, NEW.id)
                LOOP
                    PERFORM finance_assert_period_close_owner_approval(linked_close_id);
                END LOOP;
                RETURN NULL;
            END;
            $$;

            CREATE CONSTRAINT TRIGGER accounting_period_close_owner_approval_deferred
            AFTER INSERT OR UPDATE ON accounting_period_closes
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION finance_validate_period_close_owner_approval();

            CREATE CONSTRAINT TRIGGER accounting_period_close_approval_usage_deferred
            AFTER UPDATE ON accounting_period_close_approvals
            DEFERRABLE INITIALLY DEFERRED
            FOR EACH ROW EXECUTE FUNCTION finance_validate_period_close_approval_usage();
            """
        )
    )


def _drop_owner_approval_invariants() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            """
            DROP TRIGGER IF EXISTS accounting_period_close_approval_usage_deferred
                ON accounting_period_close_approvals;
            DROP TRIGGER IF EXISTS accounting_period_close_owner_approval_deferred
                ON accounting_period_closes;
            DROP FUNCTION IF EXISTS finance_validate_period_close_approval_usage();
            DROP FUNCTION IF EXISTS finance_validate_period_close_owner_approval();
            DROP FUNCTION IF EXISTS finance_assert_period_close_owner_approval(uuid);
            """
        )
    )


def upgrade() -> None:
    with op.batch_alter_table("accounting_period_close_approvals", recreate="auto") as batch:
        batch.alter_column(
            "confirmation_method",
            existing_type=sa.String(length=30),
            type_=sa.String(length=40),
            existing_nullable=False,
        )
    _replace_action_invariant(upgrade=True)
    _install_owner_approval_invariants()


def downgrade() -> None:
    _drop_owner_approval_invariants()
    _replace_action_invariant(upgrade=False)
    with op.batch_alter_table("accounting_period_close_approvals", recreate="auto") as batch:
        batch.alter_column(
            "confirmation_method",
            existing_type=sa.String(length=40),
            type_=sa.String(length=30),
            existing_nullable=False,
        )
