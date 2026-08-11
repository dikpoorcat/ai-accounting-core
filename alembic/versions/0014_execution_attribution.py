"""Add immutable owner/executor attribution for authenticated business writes.

Revision ID: 0014_execution_attribution
Revises: 0013_local_owner_identity
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_execution_attribution"
down_revision = "0013_local_owner_identity"
branch_labels = None
depends_on = None


_ROOT_COLUMNS = (
    ("accounting_period_actions", "fk_period_action_execution_attribution"),
    ("bank_transactions", "fk_bank_transaction_execution_attribution"),
    ("business_events", "fk_business_event_execution_attribution"),
    ("employees", "fk_employee_execution_attribution"),
    ("employee_payroll_profile_versions", "fk_payroll_profile_execution_attribution"),
    ("evidence", "fk_evidence_execution_attribution"),
    ("payroll_batches", "fk_payroll_batch_execution_attribution"),
    ("payroll_opening_states", "fk_payroll_opening_execution_attribution"),
    ("payroll_policy_versions", "fk_payroll_policy_execution_attribution"),
)


def upgrade() -> None:
    with op.batch_alter_table("owner_sessions") as batch:
        batch.create_unique_constraint(
            "uq_owner_session_execution_authority",
            ["org_id", "owner_account_id", "id", "credential_version"],
        )
    op.create_table(
        "execution_attributions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("owner_account_id", sa.Uuid(), nullable=False),
        sa.Column("owner_session_id", sa.Uuid(), nullable=False),
        sa.Column("owner_credential_version", sa.Integer(), nullable=False),
        sa.Column("executor_kind", sa.String(length=30), nullable=False),
        sa.Column("executor_name", sa.String(length=100), nullable=False),
        sa.Column("executor_version", sa.String(length=100), nullable=False),
        sa.Column("tool_name", sa.String(length=100), nullable=False),
        sa.Column("request_correlation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "owner_credential_version >= 1",
            name="ck_execution_attribution_credential_version",
        ),
        sa.CheckConstraint(
            "executor_kind IN ('ai_agent','deterministic_kernel','system_job')",
            name="ck_execution_attribution_executor_kind",
        ),
        sa.CheckConstraint(
            "length(executor_name) BETWEEN 1 AND 100",
            name="ck_execution_attribution_executor_name",
        ),
        sa.CheckConstraint(
            "length(executor_version) BETWEEN 1 AND 100",
            name="ck_execution_attribution_executor_version",
        ),
        sa.CheckConstraint(
            "length(tool_name) BETWEEN 1 AND 100",
            name="ck_execution_attribution_tool_name",
        ),
        sa.CheckConstraint(
            "executor_name ~ '^[A-Za-z0-9._:-]{1,100}$'",
            name="ck_execution_attribution_executor_name_ascii",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "executor_version ~ '^[A-Za-z0-9._:-]{1,100}$'",
            name="ck_execution_attribution_executor_version_ascii",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "tool_name ~ '^finance_[a-z0-9_]{1,92}$'",
            name="ck_execution_attribution_tool_name_ascii",
        ).ddl_if(dialect="postgresql"),
        sa.ForeignKeyConstraint(
            [
                "org_id",
                "owner_account_id",
                "owner_session_id",
                "owner_credential_version",
            ],
            [
                "owner_sessions.org_id",
                "owner_sessions.owner_account_id",
                "owner_sessions.id",
                "owner_sessions.credential_version",
            ],
            name="fk_execution_attribution_session_authority",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_execution_attribution_org_id"),
        sa.UniqueConstraint(
            "request_correlation_id",
            name="uq_execution_attribution_request_correlation",
        ),
    )
    op.create_index(
        "ix_execution_attributions_org_id",
        "execution_attributions",
        ["org_id"],
    )
    for table_name, foreign_key_name in _ROOT_COLUMNS:
        with op.batch_alter_table(table_name) as batch:
            batch.add_column(sa.Column("execution_attribution_id", sa.Uuid(), nullable=True))
            batch.create_foreign_key(
                foreign_key_name,
                "execution_attributions",
                ["org_id", "execution_attribution_id"],
                ["org_id", "id"],
                ondelete="RESTRICT",
            )
    _install_postgresql_guards()


def _install_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        r"""
        CREATE FUNCTION finance_guard_execution_attribution_0014()
        RETURNS trigger AS $$
        DECLARE authority owner_sessions%ROWTYPE;
        DECLARE owner owner_accounts%ROWTYPE;
        BEGIN
            IF TG_OP <> 'INSERT' THEN
                RAISE EXCEPTION 'EXECUTION_ATTRIBUTION_APPEND_ONLY';
            END IF;
            SELECT * INTO owner FROM owner_accounts
             WHERE org_id = NEW.org_id AND id = NEW.owner_account_id
             FOR UPDATE;
            SELECT * INTO authority FROM owner_sessions
             WHERE org_id = NEW.org_id
               AND owner_account_id = NEW.owner_account_id
               AND id = NEW.owner_session_id
               AND credential_version = NEW.owner_credential_version
             FOR UPDATE;
            IF authority.id IS NULL OR owner.id IS NULL
               OR authority.revoked_at IS NOT NULL
               OR owner.status <> 'active'
               OR owner.credential_version <> NEW.owner_credential_version
               OR clock_timestamp() >= authority.idle_expires_at
               OR clock_timestamp() >= authority.absolute_expires_at
               OR NEW.created_at < authority.created_at
               OR NEW.created_at > clock_timestamp()
               OR NEW.executor_name !~ '^[A-Za-z0-9._:-]{1,100}$'
               OR NEW.executor_version !~ '^[A-Za-z0-9._:-]{1,100}$'
               OR NEW.tool_name !~ '^finance_[a-z0-9_]{1,92}$' THEN
                RAISE EXCEPTION 'EXECUTION_ATTRIBUTION_AUTHORITY_INVALID';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER execution_attribution_guard
        BEFORE INSERT OR UPDATE OR DELETE ON execution_attributions
        FOR EACH ROW EXECUTE FUNCTION finance_guard_execution_attribution_0014();

        CREATE FUNCTION finance_guard_attributed_root_0014()
        RETURNS trigger AS $$
        DECLARE owner_mode boolean;
        DECLARE attribution_xmin xid;
        DECLARE configured text;
        BEGIN
            owner_mode := EXISTS (SELECT 1 FROM owner_accounts);
            IF TG_OP = 'UPDATE'
               AND NEW.execution_attribution_id IS DISTINCT FROM OLD.execution_attribution_id
               AND NOT (OLD.execution_attribution_id IS NULL
                        AND NEW.execution_attribution_id IS NOT NULL) THEN
                RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_IMMUTABLE';
            END IF;
            IF owner_mode AND NEW.execution_attribution_id IS NULL THEN
                RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED';
            END IF;
            IF NEW.execution_attribution_id IS NOT NULL THEN
                SELECT xmin INTO attribution_xmin FROM execution_attributions
                 WHERE org_id = NEW.org_id AND id = NEW.execution_attribution_id;
                IF attribution_xmin IS NULL THEN
                    RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_MISMATCH';
                END IF;
                IF owner_mode AND (TG_OP = 'INSERT' OR OLD.execution_attribution_id IS NULL) THEN
                    configured := current_setting('finance.execution_attribution_id', true);
                    IF configured IS NULL
                       OR configured <> NEW.execution_attribution_id::text
                       OR pg_xact_status((attribution_xmin::text)::xid8) <> 'in progress' THEN
                        RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_NOT_CURRENT';
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;

        CREATE FUNCTION finance_guard_payroll_batch_identity_0014()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM owner_accounts)
               AND ((TG_OP = 'INSERT' AND NEW.confirmed_by IS NOT NULL)
                    OR (TG_OP = 'UPDATE' AND OLD.confirmed_by IS NULL
                        AND NEW.confirmed_by IS NOT NULL)) THEN
                RAISE EXCEPTION 'CALLER_CONFIRMER_IDENTITY_FORBIDDEN';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE FUNCTION finance_guard_period_action_identity_0014()
        RETURNS trigger AS $$
        BEGIN
            IF EXISTS (SELECT 1 FROM owner_accounts)
               AND NEW.confirmed_by IS NOT NULL THEN
                RAISE EXCEPTION 'CALLER_CONFIRMER_IDENTITY_FORBIDDEN';
            END IF;
            IF jsonb_path_exists(NEW.input_facts::jsonb, '$.**.confirmed_by') THEN
                RAISE EXCEPTION 'CALLER_CONFIRMER_IDENTITY_FORBIDDEN';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE FUNCTION finance_guard_event_identity_0014()
        RETURNS trigger AS $$
        BEGIN
            IF jsonb_path_exists(NEW.facts::jsonb, '$.**.confirmed_by')
               OR jsonb_path_exists(NEW.rule_trace::jsonb, '$.**.confirmed_by') THEN
                RAISE EXCEPTION 'CALLER_CONFIRMER_IDENTITY_FORBIDDEN';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        """
    )
    for table_name, _foreign_key_name in _ROOT_COLUMNS:
        op.execute(
            sa.text(
                f"""
                CREATE TRIGGER {table_name}_execution_attribution_guard
                BEFORE INSERT OR UPDATE ON {table_name}
                FOR EACH ROW EXECUTE FUNCTION finance_guard_attributed_root_0014()
                """
            )
        )
    op.execute(
        r"""
        CREATE TRIGGER payroll_batch_owner_identity_guard
        BEFORE INSERT OR UPDATE ON payroll_batches
        FOR EACH ROW EXECUTE FUNCTION finance_guard_payroll_batch_identity_0014();
        CREATE TRIGGER period_action_owner_identity_guard
        BEFORE INSERT OR UPDATE ON accounting_period_actions
        FOR EACH ROW EXECUTE FUNCTION finance_guard_period_action_identity_0014();
        CREATE TRIGGER business_event_owner_identity_guard
        BEFORE INSERT OR UPDATE ON business_events
        FOR EACH ROW EXECUTE FUNCTION finance_guard_event_identity_0014();
        """
    )
    _replace_postgresql_period_assertion()


def _replace_postgresql_period_assertion() -> None:
    op.execute(
        "ALTER FUNCTION finance_assert_accounting_period_action(uuid) "
        "RENAME TO finance_assert_accounting_period_action_0012"
    )
    op.execute(_PERIOD_ACTION_ASSERTION_0014)


_PERIOD_ACTION_ASSERTION_0014 = r"""
CREATE FUNCTION finance_assert_accounting_period_action(
    target_action_id uuid
) RETURNS void AS $$
DECLARE target_action accounting_period_actions%ROWTYPE;
DECLARE linked_count bigint;
DECLARE command_name text;
DECLARE expected_request_hash text;
DECLARE invalid_evidence boolean;
BEGIN
    SELECT * INTO target_action FROM accounting_period_actions WHERE id = target_action_id;
    IF NOT FOUND THEN RETURN; END IF;
    IF jsonb_typeof(target_action.input_facts::jsonb) <> 'object'
       OR jsonb_typeof(target_action.missing_information::jsonb) <> 'array'
       OR jsonb_typeof(target_action.errors::jsonb) <> 'array' THEN
        RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
    END IF;
    IF target_action.status = 'posted' THEN
        command_name := CASE target_action.action_type
            WHEN 'period_generation' THEN 'finance_generate_accounting_period'
            ELSE 'finance_confirm_accounting_period_close'
        END;
        expected_request_hash := encode(digest(convert_to(
            finance_canonical_jsonb(jsonb_build_object(
                'command', command_name, 'request', target_action.input_facts::jsonb
            )), 'UTF8'
        ), 'sha256'), 'hex');
        IF target_action.idempotency_key IS NULL
           OR length(trim(target_action.idempotency_key)) = 0
           OR target_action.request_payload_hash !~ '^[0-9a-f]{64}$'
           OR target_action.confirmed_by IS NOT NULL
           OR target_action.confirmation_note IS NULL
           OR length(trim(target_action.confirmation_note)) = 0
           OR length(target_action.confirmation_note) > 2000
           OR target_action.input_facts::jsonb = '{}'::jsonb
           OR target_action.request_payload_hash <> expected_request_hash
           OR target_action.input_facts::jsonb ->> 'org_id' <> target_action.org_id::text
           OR target_action.input_facts::jsonb ->> 'idempotency_key' <>
              target_action.idempotency_key
           OR target_action.input_facts::jsonb ->> 'confirmation_note' <>
              target_action.confirmation_note
           OR target_action.input_facts::jsonb ? 'confirmed_by'
           OR target_action.missing_information::jsonb <> '[]'::jsonb
           OR target_action.errors::jsonb <> '[]'::jsonb
           OR jsonb_array_length(target_action.input_facts::jsonb -> 'evidence_references') <>
              (SELECT count(DISTINCT value) FROM jsonb_array_elements_text(
                  target_action.input_facts::jsonb -> 'evidence_references') AS value)
           OR NOT EXISTS (SELECT 1 FROM accounting_period_action_evidence AS evidence
                WHERE evidence.org_id = target_action.org_id
                  AND evidence.action_id = target_action.id) THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END IF;
        SELECT EXISTS (
            (SELECT value::uuid FROM jsonb_array_elements_text(
                target_action.input_facts::jsonb -> 'evidence_references') AS value
             EXCEPT SELECT evidence.evidence_id FROM accounting_period_action_evidence evidence
              WHERE evidence.org_id = target_action.org_id
                AND evidence.action_id = target_action.id)
            UNION ALL
            (SELECT evidence.evidence_id FROM accounting_period_action_evidence evidence
              WHERE evidence.org_id = target_action.org_id
                AND evidence.action_id = target_action.id
             EXCEPT SELECT value::uuid FROM jsonb_array_elements_text(
                target_action.input_facts::jsonb -> 'evidence_references') AS value)
        ) INTO invalid_evidence;
        IF jsonb_typeof(target_action.input_facts::jsonb -> 'evidence_references') <> 'array'
           OR invalid_evidence THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END IF;
        IF target_action.action_type = 'period_generation' THEN
            IF (SELECT array_agg(key ORDER BY key)
                  FROM jsonb_object_keys(target_action.input_facts::jsonb) AS key)
               <> ARRAY['confirmation_note','evidence_references','idempotency_key',
                        'org_id','period_month'] THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT count(*) INTO linked_count FROM accounting_periods
             WHERE org_id = target_action.org_id AND generation_action_id = target_action.id;
        ELSE
            IF (SELECT array_agg(key ORDER BY key)
                  FROM jsonb_object_keys(target_action.input_facts::jsonb) AS key)
               <> ARRAY['calculation_hash','closing_date','confirmation_note',
                        'evidence_references','idempotency_key','org_id','period_id','review_facts']
               OR (SELECT array_agg(key ORDER BY key)
                     FROM jsonb_object_keys(target_action.input_facts::jsonb -> 'review_facts') key)
                  <> ARRAY['asset_and_borrowing_schedules_reviewed',
                           'bank_reconciliation_reviewed','open_items_reviewed',
                           'payroll_and_statutory_items_reviewed','tax_items_reviewed',
                           'voucher_completeness_reviewed'] THEN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
            END IF;
            SELECT count(*) INTO linked_count FROM accounting_period_closes
             WHERE org_id = target_action.org_id AND action_id = target_action.id;
        END IF;
        IF linked_count <> 1 THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END IF;
        IF target_action.action_type = 'period_close' AND (
            target_action.input_facts::jsonb
                #>> '{review_facts,voucher_completeness_reviewed}' <> 'true'
            OR target_action.input_facts::jsonb
                #>> '{review_facts,bank_reconciliation_reviewed}' <> 'true'
            OR target_action.input_facts::jsonb
                #>> '{review_facts,open_items_reviewed}' <> 'true'
            OR target_action.input_facts::jsonb
                #>> '{review_facts,payroll_and_statutory_items_reviewed}' <> 'true'
            OR target_action.input_facts::jsonb
                #>> '{review_facts,tax_items_reviewed}' <> 'true'
            OR target_action.input_facts::jsonb
                #>> '{review_facts,asset_and_borrowing_schedules_reviewed}' <> 'true'
        ) THEN RAISE EXCEPTION 'ACCOUNTING_PERIOD_REVIEW_INCOMPLETE'; END IF;
    ELSE
        IF target_action.request_payload_hash IS NULL
           OR target_action.request_payload_hash !~ '^[0-9a-f]{64}$'
           OR target_action.input_facts::jsonb <> '{}'::jsonb
           OR target_action.confirmed_by IS NOT NULL
           OR target_action.confirmation_note IS NOT NULL
           OR EXISTS (SELECT 1 FROM accounting_period_action_evidence evidence
                WHERE evidence.org_id = target_action.org_id
                  AND evidence.action_id = target_action.id)
           OR EXISTS (SELECT 1 FROM jsonb_array_elements(
                target_action.missing_information::jsonb) item
                WHERE jsonb_typeof(item) <> 'string' OR item #>> '{}' NOT IN (
                    'idempotency_key','confirmation_note','evidence_references','calculation_hash',
                    'review_facts.voucher_completeness_reviewed',
                    'review_facts.bank_reconciliation_reviewed','review_facts.open_items_reviewed',
                    'review_facts.payroll_and_statutory_items_reviewed',
                    'review_facts.tax_items_reviewed',
                    'review_facts.asset_and_borrowing_schedules_reviewed'))
           OR EXISTS (SELECT 1 FROM jsonb_array_elements(target_action.errors::jsonb) item
                WHERE jsonb_typeof(item) <> 'object'
                   OR (SELECT array_agg(key ORDER BY key) FROM jsonb_object_keys(item) key)
                      <> ARRAY['code','field_paths']
                   OR jsonb_typeof(item -> 'code') <> 'string'
                   OR item ->> 'code' !~ '^ACCOUNTING_PERIOD_[A-Z0-9_]+$'
                   OR jsonb_typeof(item -> 'field_paths') <> 'array'
                   OR EXISTS (SELECT 1 FROM jsonb_array_elements(item -> 'field_paths') path
                        WHERE jsonb_typeof(path) <> 'string' OR path #>> '{}' NOT IN (
                            'idempotency_key','confirmation_note','evidence_references',
                            'calculation_hash','review_facts.voucher_completeness_reviewed',
                            'review_facts.bank_reconciliation_reviewed',
                            'review_facts.open_items_reviewed',
                            'review_facts.payroll_and_statutory_items_reviewed',
                            'review_facts.tax_items_reviewed',
                            'review_facts.asset_and_borrowing_schedules_reviewed')))
           OR (target_action.missing_information::jsonb = '[]'::jsonb
               AND target_action.errors::jsonb = '[]'::jsonb)
           OR EXISTS (SELECT 1 FROM accounting_periods
                WHERE generation_action_id = target_action.id)
           OR EXISTS (SELECT 1 FROM accounting_period_closes
                WHERE action_id = target_action.id) THEN
            RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
        END IF;
    END IF;
EXCEPTION WHEN invalid_text_representation THEN
    RAISE EXCEPTION 'ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE';
END;
$$ LANGUAGE plpgsql;
"""


def _assert_downgrade_safe() -> None:
    if op.get_bind().scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM execution_attributions)")
    ):
        raise RuntimeError("EXECUTION_ATTRIBUTION_DOWNGRADE_UNSAFE")


def downgrade() -> None:
    _assert_downgrade_safe()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            r"""
            DROP FUNCTION finance_assert_accounting_period_action(uuid);
            ALTER FUNCTION finance_assert_accounting_period_action_0012(uuid)
              RENAME TO finance_assert_accounting_period_action;
            DROP TRIGGER IF EXISTS business_event_owner_identity_guard ON business_events;
            DROP TRIGGER IF EXISTS period_action_owner_identity_guard ON accounting_period_actions;
            DROP TRIGGER IF EXISTS payroll_batch_owner_identity_guard ON payroll_batches;
            """
        )
        for table_name, _foreign_key_name in reversed(_ROOT_COLUMNS):
            op.execute(
                sa.text(
                    "DROP TRIGGER IF EXISTS "
                    f"{table_name}_execution_attribution_guard ON {table_name}"
                )
            )
        op.execute(
            r"""
            DROP FUNCTION IF EXISTS finance_guard_event_identity_0014();
            DROP FUNCTION IF EXISTS finance_guard_period_action_identity_0014();
            DROP FUNCTION IF EXISTS finance_guard_payroll_batch_identity_0014();
            DROP FUNCTION IF EXISTS finance_guard_attributed_root_0014();
            DROP TRIGGER IF EXISTS execution_attribution_guard ON execution_attributions;
            DROP FUNCTION IF EXISTS finance_guard_execution_attribution_0014();
            """
        )
    for table_name, foreign_key_name in reversed(_ROOT_COLUMNS):
        with op.batch_alter_table(table_name) as batch:
            batch.drop_constraint(foreign_key_name, type_="foreignkey")
            batch.drop_column("execution_attribution_id")
    op.drop_index("ix_execution_attributions_org_id", table_name="execution_attributions")
    op.drop_table("execution_attributions")
    with op.batch_alter_table("owner_sessions") as batch:
        batch.drop_constraint("uq_owner_session_execution_authority", type_="unique")
