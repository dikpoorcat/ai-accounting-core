"""Add the single local owner identity and immutable security history.

Revision ID: 0013_local_owner_identity
Revises: 0012_accounting_period_close
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0013_local_owner_identity"
down_revision = "0012_accounting_period_close"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "owner_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("singleton_key", sa.Integer(), server_default="1", nullable=False),
        sa.Column("login_name", sa.String(length=100), nullable=False),
        sa.Column("login_name_normalized", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), server_default="active", nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("credential_version", sa.Integer(), server_default="1", nullable=False),
        sa.Column(
            "password_failed_attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("password_throttled_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "recovery_failed_attempts", sa.Integer(), server_default="0", nullable=False
        ),
        sa.Column("recovery_throttled_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "password_changed_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint("singleton_key = 1", name="ck_owner_account_singleton"),
        sa.CheckConstraint(
            "length(login_name) BETWEEN 3 AND 100 AND login_name = trim(login_name)",
            name="ck_owner_account_login_name",
        ),
        sa.CheckConstraint(
            "login_name_normalized = lower(trim(login_name))",
            name="ck_owner_account_login_normalized",
        ),
        sa.CheckConstraint(
            "length(password_hash) = 97 AND "
            "password_hash LIKE '$argon2id$v=19$m=65536,t=3,p=4$%'",
            name="ck_owner_account_password_hash",
        ),
        sa.CheckConstraint(
            "login_name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$'",
            name="ck_owner_account_login_ascii",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "login_name NOT GLOB '*[^A-Za-z0-9._-]*' "
            "AND substr(login_name, 1, 1) GLOB '[A-Za-z0-9]'",
            name="ck_owner_account_login_ascii",
        ).ddl_if(dialect="sqlite"),
        sa.CheckConstraint(
            "password_hash ~ "
            "'^\\$argon2id\\$v=19\\$m=65536,t=3,p=4\\$[A-Za-z0-9+/]{22}\\$"
            "[A-Za-z0-9+/]{43}$'",
            name="ck_owner_account_password_hash_shape",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "status IN ('active','disabled')", name="ck_owner_account_status"
        ),
        sa.CheckConstraint(
            "credential_version >= 1", name="ck_owner_account_credential_version"
        ),
        sa.CheckConstraint(
            "password_failed_attempts >= 0", name="ck_owner_account_password_failures"
        ),
        sa.CheckConstraint(
            "recovery_failed_attempts >= 0", name="ck_owner_account_recovery_failures"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_owner_account_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("singleton_key", name="uq_owner_account_singleton"),
        sa.UniqueConstraint("org_id", name="uq_owner_account_org"),
        sa.UniqueConstraint(
            "login_name_normalized", name="uq_owner_account_login_normalized"
        ),
        sa.UniqueConstraint("org_id", "id", name="uq_owner_account_org_id"),
    )
    op.create_index("ix_owner_accounts_org_id", "owner_accounts", ["org_id"])

    op.create_table(
        "owner_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("owner_account_id", sa.Uuid(), nullable=False),
        sa.Column("secret_sha256", sa.String(length=64), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column(
            "last_seen_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=50), nullable=True),
        sa.CheckConstraint(
            "length(secret_sha256) = 64", name="ck_owner_session_secret_sha256"
        ),
        sa.CheckConstraint(
            "secret_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_owner_session_secret_lowerhex",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "secret_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_owner_session_secret_lowerhex",
        ).ddl_if(dialect="sqlite"),
        sa.CheckConstraint(
            "credential_version >= 1", name="ck_owner_session_credential_version"
        ),
        sa.CheckConstraint("last_seen_at >= created_at", name="ck_owner_session_last_seen"),
        sa.CheckConstraint(
            "idle_expires_at > created_at", name="ck_owner_session_idle_expiry"
        ),
        sa.CheckConstraint(
            "absolute_expires_at > created_at", name="ck_owner_session_absolute_expiry"
        ),
        sa.CheckConstraint(
            "idle_expires_at <= absolute_expires_at",
            name="ck_owner_session_expiry_order",
        ),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoke_reason IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)",
            name="ck_owner_session_revocation_state",
        ),
        sa.CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_owner_session_revoked_at",
        ),
        sa.CheckConstraint(
            "revoke_reason IS NULL OR revoke_reason IN "
            "('logout','credential_changed','recovery_used','idle_expired',"
            "'absolute_expired','credential_version_mismatch')",
            name="ck_owner_session_revoke_reason",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_account_id"],
            ["owner_accounts.org_id", "owner_accounts.id"],
            name="fk_owner_session_org_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("secret_sha256", name="uq_owner_session_secret_sha256"),
        sa.UniqueConstraint("org_id", "id", name="uq_owner_session_org_id"),
    )
    op.create_index("ix_owner_sessions_org_id", "owner_sessions", ["org_id"])
    op.create_index(
        "ix_owner_sessions_owner_account_id", "owner_sessions", ["owner_account_id"]
    )

    op.create_table(
        "owner_recovery_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("owner_account_id", sa.Uuid(), nullable=False),
        sa.Column("code_sha256", sa.String(length=64), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(code_sha256) = 64", name="ck_owner_recovery_code_sha256"
        ),
        sa.CheckConstraint(
            "code_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_owner_recovery_code_lowerhex",
        ).ddl_if(dialect="postgresql"),
        sa.CheckConstraint(
            "code_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_owner_recovery_code_lowerhex",
        ).ddl_if(dialect="sqlite"),
        sa.CheckConstraint(
            "credential_version >= 1", name="ck_owner_recovery_code_credential_version"
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR invalidated_at IS NULL",
            name="ck_owner_recovery_code_terminal_state",
        ),
        sa.CheckConstraint(
            "used_at IS NULL OR used_at >= created_at",
            name="ck_owner_recovery_code_used_at",
        ),
        sa.CheckConstraint(
            "invalidated_at IS NULL OR invalidated_at >= created_at",
            name="ck_owner_recovery_code_invalidated_at",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_account_id"],
            ["owner_accounts.org_id", "owner_accounts.id"],
            name="fk_owner_recovery_code_org_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("code_sha256", name="uq_owner_recovery_code_sha256"),
        sa.UniqueConstraint("org_id", "id", name="uq_owner_recovery_code_org_id"),
    )
    op.create_index("ix_owner_recovery_codes_org_id", "owner_recovery_codes", ["org_id"])
    op.create_index(
        "ix_owner_recovery_codes_owner_account_id",
        "owner_recovery_codes",
        ["owner_account_id"],
    )
    op.create_index(
        "uq_owner_recovery_code_current",
        "owner_recovery_codes",
        ["owner_account_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND invalidated_at IS NULL"),
        sqlite_where=sa.text("used_at IS NULL AND invalidated_at IS NULL"),
    )

    op.create_table(
        "identity_audit_events",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("owner_account_id", sa.Uuid(), nullable=True),
        sa.Column("session_id", sa.Uuid(), nullable=True),
        sa.Column("event_type", sa.String(length=50), nullable=False),
        sa.Column("outcome", sa.String(length=20), nullable=False),
        sa.Column("reason_code", sa.String(length=100), nullable=True),
        sa.Column("request_correlation_id", sa.Uuid(), nullable=False),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "event_type IN ('owner_provisioned','login_succeeded','login_failed',"
            "'session_revoked','session_expired','password_changed',"
            "'recovery_succeeded','recovery_failed','recovery_code_replaced')",
            name="ck_identity_audit_event_type",
        ),
        sa.CheckConstraint(
            "outcome IN ('succeeded','rejected','blocked')",
            name="ck_identity_audit_outcome",
        ),
        sa.CheckConstraint(
            "reason_code IS NULL OR reason_code IN "
            "('INVALID_CREDENTIALS','ACCOUNT_THROTTLED','ACCOUNT_DISABLED',"
            "'SESSION_REVOKED','SESSION_IDLE_EXPIRED','SESSION_ABSOLUTE_EXPIRED',"
            "'SESSION_CREDENTIAL_VERSION_MISMATCH','RECOVERY_CODE_INVALID',"
            "'RECOVERY_THROTTLED','PASSWORD_POLICY_REJECTED','OWNER_ALREADY_PROVISIONED')",
            name="ck_identity_audit_reason_code",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_identity_audit_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_account_id"],
            ["owner_accounts.org_id", "owner_accounts.id"],
            name="fk_identity_audit_org_account",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["owner_sessions.org_id", "owner_sessions.id"],
            name="fk_identity_audit_org_session",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_identity_audit_events_org_id", "identity_audit_events", ["org_id"])
    op.create_index(
        "ix_identity_audit_events_request_correlation_id",
        "identity_audit_events",
        ["request_correlation_id"],
    )

    _install_postgresql_guards()


def _install_postgresql_guards() -> None:
    if op.get_bind().dialect.name != "postgresql":
        return
    op.execute(
        r"""
        CREATE FUNCTION finance_guard_owner_account_0013()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.credential_version <> 1
                   OR NEW.status <> 'active'
                   OR NEW.password_failed_attempts <> 0
                   OR NEW.password_throttled_until IS NOT NULL
                   OR NEW.recovery_failed_attempts <> 0
                   OR NEW.recovery_throttled_until IS NOT NULL
                   OR NEW.last_authenticated_at IS NOT NULL THEN
                    RAISE EXCEPTION 'IDENTITY_OWNER_INITIAL_STATE_INVALID';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'IDENTITY_SUBJECT_DELETE_FORBIDDEN';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.singleton_key IS DISTINCT FROM OLD.singleton_key
               OR NEW.login_name IS DISTINCT FROM OLD.login_name
               OR NEW.login_name_normalized IS DISTINCT FROM OLD.login_name_normalized
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'IDENTITY_OWNER_IMMUTABLE_FIELD';
            END IF;
            IF OLD.status = 'disabled' AND NEW.status IS DISTINCT FROM OLD.status THEN
                RAISE EXCEPTION 'IDENTITY_OWNER_REACTIVATION_FORBIDDEN';
            END IF;
            IF NEW.updated_at < OLD.updated_at OR NEW.updated_at < NEW.created_at THEN
                RAISE EXCEPTION 'IDENTITY_OWNER_UPDATED_AT_INVALID';
            END IF;
            IF NEW.password_hash IS DISTINCT FROM OLD.password_hash THEN
                IF NEW.credential_version <> OLD.credential_version + 1
                   OR NEW.password_changed_at <= OLD.password_changed_at THEN
                    RAISE EXCEPTION 'IDENTITY_CREDENTIAL_ROTATION_INVALID';
                END IF;
            ELSIF NEW.credential_version IS DISTINCT FROM OLD.credential_version
                  OR NEW.password_changed_at IS DISTINCT FROM OLD.password_changed_at THEN
                RAISE EXCEPTION 'IDENTITY_CREDENTIAL_ROTATION_INVALID';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER owner_account_mutation_guard
        BEFORE INSERT OR UPDATE OR DELETE ON owner_accounts
        FOR EACH ROW EXECUTE FUNCTION finance_guard_owner_account_0013();

        CREATE FUNCTION finance_guard_owner_session_0013()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.revoked_at IS NOT NULL OR NEW.revoke_reason IS NOT NULL
                   OR NOT EXISTS (
                        SELECT 1 FROM owner_accounts AS owner
                         WHERE owner.id = NEW.owner_account_id
                           AND owner.org_id = NEW.org_id
                           AND owner.status = 'active'
                           AND owner.credential_version = NEW.credential_version
                   ) THEN
                    RAISE EXCEPTION 'IDENTITY_SESSION_INITIAL_STATE_INVALID';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'IDENTITY_SESSION_DELETE_FORBIDDEN';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.owner_account_id IS DISTINCT FROM OLD.owner_account_id
               OR NEW.secret_sha256 IS DISTINCT FROM OLD.secret_sha256
               OR NEW.credential_version IS DISTINCT FROM OLD.credential_version
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.absolute_expires_at IS DISTINCT FROM OLD.absolute_expires_at THEN
                RAISE EXCEPTION 'IDENTITY_SESSION_IMMUTABLE_FIELD';
            END IF;
            IF NEW.last_seen_at < OLD.last_seen_at
               OR NEW.idle_expires_at < OLD.idle_expires_at
               OR NEW.idle_expires_at > NEW.absolute_expires_at THEN
                RAISE EXCEPTION 'IDENTITY_SESSION_EXPIRY_INVALID';
            END IF;
            IF OLD.revoked_at IS NOT NULL
               AND (NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
                    OR NEW.revoke_reason IS DISTINCT FROM OLD.revoke_reason) THEN
                RAISE EXCEPTION 'IDENTITY_SESSION_REVOCATION_IMMUTABLE';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER owner_session_mutation_guard
        BEFORE INSERT OR UPDATE OR DELETE ON owner_sessions
        FOR EACH ROW EXECUTE FUNCTION finance_guard_owner_session_0013();

        CREATE FUNCTION finance_guard_owner_recovery_code_0013()
        RETURNS trigger AS $$
        BEGIN
            IF TG_OP = 'INSERT' THEN
                IF NEW.used_at IS NOT NULL OR NEW.invalidated_at IS NOT NULL
                   OR NOT EXISTS (
                        SELECT 1 FROM owner_accounts AS owner
                         WHERE owner.id = NEW.owner_account_id
                           AND owner.org_id = NEW.org_id
                           AND owner.status = 'active'
                           AND owner.credential_version = NEW.credential_version
                   ) THEN
                    RAISE EXCEPTION 'IDENTITY_RECOVERY_INITIAL_STATE_INVALID';
                END IF;
                RETURN NEW;
            END IF;
            IF TG_OP = 'DELETE' THEN
                RAISE EXCEPTION 'IDENTITY_RECOVERY_HISTORY_DELETE_FORBIDDEN';
            END IF;
            IF NEW.id IS DISTINCT FROM OLD.id
               OR NEW.org_id IS DISTINCT FROM OLD.org_id
               OR NEW.owner_account_id IS DISTINCT FROM OLD.owner_account_id
               OR NEW.code_sha256 IS DISTINCT FROM OLD.code_sha256
               OR NEW.credential_version IS DISTINCT FROM OLD.credential_version
               OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
                RAISE EXCEPTION 'IDENTITY_RECOVERY_IMMUTABLE_FIELD';
            END IF;
            IF OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at THEN
                RAISE EXCEPTION 'IDENTITY_RECOVERY_TERMINAL_STATE_IMMUTABLE';
            END IF;
            IF OLD.invalidated_at IS NOT NULL
               AND NEW.invalidated_at IS DISTINCT FROM OLD.invalidated_at THEN
                RAISE EXCEPTION 'IDENTITY_RECOVERY_TERMINAL_STATE_IMMUTABLE';
            END IF;
            IF (OLD.used_at IS NULL AND NEW.used_at IS NOT NULL AND NEW.used_at < OLD.created_at)
               OR (OLD.invalidated_at IS NULL AND NEW.invalidated_at IS NOT NULL
                   AND NEW.invalidated_at < OLD.created_at) THEN
                RAISE EXCEPTION 'IDENTITY_RECOVERY_TERMINAL_TIME_INVALID';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER owner_recovery_code_mutation_guard
        BEFORE INSERT OR UPDATE OR DELETE ON owner_recovery_codes
        FOR EACH ROW EXECUTE FUNCTION finance_guard_owner_recovery_code_0013();

        CREATE FUNCTION finance_block_identity_audit_mutation_0013()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION 'IDENTITY_AUDIT_APPEND_ONLY';
        END;
        $$ LANGUAGE plpgsql;
        CREATE TRIGGER identity_audit_event_append_only
        BEFORE UPDATE OR DELETE ON identity_audit_events
        FOR EACH ROW EXECUTE FUNCTION finance_block_identity_audit_mutation_0013();
        """
    )


def _assert_downgrade_safe() -> None:
    bind = op.get_bind()
    has_history = bind.scalar(
        sa.text(
            """
            SELECT EXISTS (SELECT 1 FROM owner_accounts)
                OR EXISTS (SELECT 1 FROM owner_sessions)
                OR EXISTS (SELECT 1 FROM owner_recovery_codes)
                OR EXISTS (SELECT 1 FROM identity_audit_events)
            """
        )
    )
    if has_history:
        raise RuntimeError("IDENTITY_DOWNGRADE_UNSAFE")


def downgrade() -> None:
    _assert_downgrade_safe()
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            r"""
            DROP TRIGGER IF EXISTS identity_audit_event_append_only ON identity_audit_events;
            DROP FUNCTION IF EXISTS finance_block_identity_audit_mutation_0013();
            DROP TRIGGER IF EXISTS owner_recovery_code_mutation_guard ON owner_recovery_codes;
            DROP FUNCTION IF EXISTS finance_guard_owner_recovery_code_0013();
            DROP TRIGGER IF EXISTS owner_session_mutation_guard ON owner_sessions;
            DROP FUNCTION IF EXISTS finance_guard_owner_session_0013();
            DROP TRIGGER IF EXISTS owner_account_mutation_guard ON owner_accounts;
            DROP FUNCTION IF EXISTS finance_guard_owner_account_0013();
            """
        )
    op.drop_index(
        "ix_identity_audit_events_request_correlation_id",
        table_name="identity_audit_events",
    )
    op.drop_index("ix_identity_audit_events_org_id", table_name="identity_audit_events")
    op.drop_table("identity_audit_events")
    op.drop_index("uq_owner_recovery_code_current", table_name="owner_recovery_codes")
    op.drop_index(
        "ix_owner_recovery_codes_owner_account_id", table_name="owner_recovery_codes"
    )
    op.drop_index("ix_owner_recovery_codes_org_id", table_name="owner_recovery_codes")
    op.drop_table("owner_recovery_codes")
    op.drop_index("ix_owner_sessions_owner_account_id", table_name="owner_sessions")
    op.drop_index("ix_owner_sessions_org_id", table_name="owner_sessions")
    op.drop_table("owner_sessions")
    op.drop_index("ix_owner_accounts_org_id", table_name="owner_accounts")
    op.drop_table("owner_accounts")
