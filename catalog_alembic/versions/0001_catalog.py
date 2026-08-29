"""Create the global identity and company-routing catalog.

Revision ID: 0001_catalog
Revises:
Create Date: 2026-08-28
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import context, op

revision = "0001_catalog"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "catalog_metadata",
        sa.Column("singleton_key", sa.Integer(), nullable=False),
        sa.Column("catalog_instance_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("singleton_key = 1", name="ck_catalog_metadata_singleton"),
        sa.PrimaryKeyConstraint("singleton_key"),
        sa.UniqueConstraint("catalog_instance_id"),
    )
    op.create_table(
        "company_registry",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("database_name", sa.String(length=80), nullable=False),
        sa.Column("database_identity", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("taxpayer_identification_number", sa.String(length=18), nullable=False),
        sa.Column("profile_effective_from", sa.Date(), nullable=False),
        sa.Column("filing_cycle", sa.String(length=20), nullable=False),
        sa.Column("urban_maintenance_rate", sa.Numeric(precision=6, scale=5), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "(status = 'archived' AND archived_at IS NOT NULL) OR "
            "(status <> 'archived' AND archived_at IS NULL)",
            name="ck_company_registry_archive_state",
        ),
        sa.CheckConstraint(
            "database_name = 'finance' OR "
            "database_name ~ '^finance_company_[0-9a-f]{32}$'",
            name="ck_company_registry_database_name",
        ),
        sa.CheckConstraint(
            "filing_cycle IN ('monthly','quarterly')",
            name="ck_company_registry_filing_cycle",
        ),
        sa.CheckConstraint(
            "status IN ('provisioning','active','changing','archived','attention_required')",
            name="ck_company_registry_status",
        ),
        sa.CheckConstraint(
            "urban_maintenance_rate IN (0.07,0.05,0.01)",
            name="ck_company_registry_urban_rate",
        ),
        sa.PrimaryKeyConstraint("org_id"),
        sa.UniqueConstraint("database_identity"),
        sa.UniqueConstraint("database_name"),
        sa.UniqueConstraint(
            "taxpayer_identification_number",
            name="uq_company_registry_taxpayer_identification_number",
        ),
    )
    op.create_table(
        "owner_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("singleton_key", sa.Integer(), nullable=False),
        sa.Column("login_name", sa.String(length=100), nullable=False),
        sa.Column("login_name_normalized", sa.String(length=100), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("password_hash", sa.String(length=512), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("password_failed_attempts", sa.Integer(), nullable=False),
        sa.Column("password_throttled_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("recovery_failed_attempts", sa.Integer(), nullable=False),
        sa.Column("recovery_throttled_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_authenticated_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("singleton_key = 1", name="ck_owner_account_singleton"),
        sa.CheckConstraint("credential_version >= 1", name="ck_owner_account_credential_version"),
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
            "password_failed_attempts >= 0", name="ck_owner_account_password_failures"
        ),
        sa.CheckConstraint(
            "recovery_failed_attempts >= 0", name="ck_owner_account_recovery_failures"
        ),
        sa.CheckConstraint("status IN ('active','disabled')", name="ck_owner_account_status"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["company_registry.org_id"],
            name="fk_owner_account_company", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("login_name_normalized", name="uq_owner_account_login_normalized"),
        sa.UniqueConstraint("org_id", "id", name="uq_owner_account_org_id"),
        sa.UniqueConstraint("singleton_key", name="uq_owner_account_singleton"),
    )
    op.create_index(op.f("ix_owner_accounts_org_id"), "owner_accounts", ["org_id"])
    op.create_table(
        "owner_sessions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("owner_account_id", sa.Uuid(), nullable=False),
        sa.Column("secret_sha256", sa.String(length=64), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("idle_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoke_reason", sa.String(length=50), nullable=True),
        sa.CheckConstraint("credential_version >= 1", name="ck_owner_session_credential_version"),
        sa.CheckConstraint("last_seen_at >= created_at", name="ck_owner_session_last_seen"),
        sa.CheckConstraint("idle_expires_at > created_at", name="ck_owner_session_idle_expiry"),
        sa.CheckConstraint(
            "absolute_expires_at > created_at", name="ck_owner_session_absolute_expiry"
        ),
        sa.CheckConstraint(
            "idle_expires_at <= absolute_expires_at", name="ck_owner_session_expiry_order"
        ),
        sa.CheckConstraint("length(secret_sha256) = 64", name="ck_owner_session_secret_sha256"),
        sa.CheckConstraint(
            "(revoked_at IS NULL AND revoke_reason IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)",
            name="ck_owner_session_revocation_state",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_account_id"],
            ["owner_accounts.org_id", "owner_accounts.id"],
            name="fk_owner_session_org_account",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "owner_account_id", "id", "credential_version",
            name="uq_owner_session_execution_authority"
        ),
        sa.UniqueConstraint("org_id", "id", name="uq_owner_session_org_id"),
        sa.UniqueConstraint("secret_sha256", name="uq_owner_session_secret_sha256"),
    )
    op.create_index(op.f("ix_owner_sessions_org_id"), "owner_sessions", ["org_id"])
    op.create_index(
        op.f("ix_owner_sessions_owner_account_id"), "owner_sessions", ["owner_account_id"]
    )
    op.create_table(
        "owner_recovery_codes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("owner_account_id", sa.Uuid(), nullable=False),
        sa.Column("code_sha256", sa.String(length=64), nullable=False),
        sa.Column("credential_version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("invalidated_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "credential_version >= 1", name="ck_owner_recovery_code_credential_version"
        ),
        sa.CheckConstraint("length(code_sha256) = 64", name="ck_owner_recovery_code_sha256"),
        sa.CheckConstraint(
            "used_at IS NULL OR invalidated_at IS NULL",
            name="ck_owner_recovery_code_terminal_state",
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
    op.create_index(op.f("ix_owner_recovery_codes_org_id"), "owner_recovery_codes", ["org_id"])
    op.create_index(
        op.f("ix_owner_recovery_codes_owner_account_id"),
        "owner_recovery_codes",
        ["owner_account_id"],
    )
    op.create_index(
        "uq_owner_recovery_code_current",
        "owner_recovery_codes",
        ["owner_account_id"],
        unique=True,
        postgresql_where=sa.text("used_at IS NULL AND invalidated_at IS NULL"),
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
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["org_id"], ["company_registry.org_id"],
            name="fk_identity_audit_company", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_account_id"],
            ["owner_accounts.org_id", "owner_accounts.id"],
            name="fk_identity_audit_org_account", ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["owner_sessions.org_id", "owner_sessions.id"],
            name="fk_identity_audit_org_session", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_identity_audit_events_org_id"), "identity_audit_events", ["org_id"])
    op.create_index(
        op.f("ix_identity_audit_events_request_correlation_id"),
        "identity_audit_events",
        ["request_correlation_id"],
    )
    op.create_table(
        "company_lifecycle_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("action_type", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("input_facts", sa.JSON(), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("owner_account_id", sa.Uuid(), nullable=False),
        sa.Column("owner_session_id", sa.Uuid(), nullable=False),
        sa.Column("owner_credential_version", sa.Integer(), nullable=False),
        sa.Column("executor_kind", sa.String(length=30), nullable=False),
        sa.Column("executor_name", sa.String(length=100), nullable=False),
        sa.Column("executor_version", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP")
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "action_type IN ('create','profile_change','status_change','import')",
            name="ck_company_lifecycle_action_type",
        ),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64 AND "
            "(calculation_hash IS NULL OR length(calculation_hash) = 64)",
            name="ck_company_lifecycle_hashes",
        ),
        sa.CheckConstraint(
            "status IN ('started','completed','failed')", name="ck_company_lifecycle_status"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["company_registry.org_id"],
            name="fk_company_lifecycle_company", ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "action_type", "idempotency_key",
            name="uq_company_lifecycle_org_idempotency"
        ),
    )
    op.create_index(
        op.f("ix_company_lifecycle_actions_org_id"),
        "company_lifecycle_actions",
        ["org_id"],
    )
    if op.get_bind().dialect.name == "postgresql":
        op.get_bind().exec_driver_sql(_IDENTITY_GUARDS)
    catalog_id = uuid.UUID(str(context.config.attributes.get("catalog_instance_id", uuid.uuid4())))
    op.execute(
        sa.text(
            "INSERT INTO catalog_metadata (singleton_key, catalog_instance_id, created_at) "
            "VALUES (1, :catalog_id, CURRENT_TIMESTAMP)"
        ).bindparams(catalog_id=catalog_id)
    )


def downgrade() -> None:
    raise RuntimeError("CATALOG_DATABASE_HAS_NO_AUTOMATIC_DOWNGRADE")


_IDENTITY_GUARDS = r"""
CREATE FUNCTION finance_catalog_identity_audit_append_only() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'IDENTITY_AUDIT_APPEND_ONLY';
END;
$$;

CREATE FUNCTION finance_catalog_owner_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'IDENTITY_OWNER_DELETE_FORBIDDEN';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.status <> 'active' OR NEW.credential_version <> 1
           OR NEW.password_failed_attempts <> 0 OR NEW.recovery_failed_attempts <> 0
           OR NEW.password_throttled_until IS NOT NULL
           OR NEW.recovery_throttled_until IS NOT NULL THEN
            RAISE EXCEPTION 'IDENTITY_OWNER_INITIAL_STATE_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.singleton_key IS DISTINCT FROM OLD.singleton_key
       OR NEW.login_name IS DISTINCT FROM OLD.login_name
       OR NEW.login_name_normalized IS DISTINCT FROM OLD.login_name_normalized
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'IDENTITY_OWNER_IMMUTABLE_FIELD';
    END IF;
    IF OLD.status = 'disabled' AND NEW.status IS DISTINCT FROM OLD.status THEN
        RAISE EXCEPTION 'IDENTITY_OWNER_REACTIVATION_FORBIDDEN';
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
$$;

CREATE FUNCTION finance_catalog_session_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'IDENTITY_SESSION_HISTORY_DELETE_FORBIDDEN';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.revoked_at IS NOT NULL OR NEW.revoke_reason IS NOT NULL
           OR NOT EXISTS (
                SELECT 1 FROM owner_accounts owner
                 WHERE owner.id = NEW.owner_account_id AND owner.org_id = NEW.org_id
                   AND owner.status = 'active'
                   AND owner.credential_version = NEW.credential_version) THEN
            RAISE EXCEPTION 'IDENTITY_SESSION_INITIAL_STATE_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.owner_account_id IS DISTINCT FROM OLD.owner_account_id
       OR NEW.secret_sha256 IS DISTINCT FROM OLD.secret_sha256
       OR NEW.credential_version IS DISTINCT FROM OLD.credential_version
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.absolute_expires_at IS DISTINCT FROM OLD.absolute_expires_at THEN
        RAISE EXCEPTION 'IDENTITY_SESSION_IMMUTABLE_FIELD';
    END IF;
    IF OLD.revoked_at IS NOT NULL
       AND (NEW.revoked_at IS DISTINCT FROM OLD.revoked_at
            OR NEW.revoke_reason IS DISTINCT FROM OLD.revoke_reason) THEN
        RAISE EXCEPTION 'IDENTITY_SESSION_TERMINAL_STATE_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

CREATE FUNCTION finance_catalog_recovery_guard() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'IDENTITY_RECOVERY_HISTORY_DELETE_FORBIDDEN';
    END IF;
    IF TG_OP = 'INSERT' THEN
        IF NEW.used_at IS NOT NULL OR NEW.invalidated_at IS NOT NULL
           OR NOT EXISTS (
                SELECT 1 FROM owner_accounts owner
                 WHERE owner.id = NEW.owner_account_id AND owner.org_id = NEW.org_id
                   AND owner.status = 'active'
                   AND owner.credential_version = NEW.credential_version) THEN
            RAISE EXCEPTION 'IDENTITY_RECOVERY_INITIAL_STATE_INVALID';
        END IF;
        RETURN NEW;
    END IF;
    IF NEW.id IS DISTINCT FROM OLD.id OR NEW.org_id IS DISTINCT FROM OLD.org_id
       OR NEW.owner_account_id IS DISTINCT FROM OLD.owner_account_id
       OR NEW.code_sha256 IS DISTINCT FROM OLD.code_sha256
       OR NEW.credential_version IS DISTINCT FROM OLD.credential_version
       OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
        RAISE EXCEPTION 'IDENTITY_RECOVERY_IMMUTABLE_FIELD';
    END IF;
    IF (OLD.used_at IS NOT NULL AND NEW.used_at IS DISTINCT FROM OLD.used_at)
       OR (OLD.invalidated_at IS NOT NULL
           AND NEW.invalidated_at IS DISTINCT FROM OLD.invalidated_at) THEN
        RAISE EXCEPTION 'IDENTITY_RECOVERY_TERMINAL_STATE_IMMUTABLE';
    END IF;
    RETURN NEW;
END;
$$;

CREATE TRIGGER catalog_identity_audit_append_only
BEFORE UPDATE OR DELETE ON identity_audit_events
FOR EACH ROW EXECUTE FUNCTION finance_catalog_identity_audit_append_only();
CREATE TRIGGER catalog_owner_guard
BEFORE INSERT OR UPDATE OR DELETE ON owner_accounts
FOR EACH ROW EXECUTE FUNCTION finance_catalog_owner_guard();
CREATE TRIGGER catalog_session_guard
BEFORE INSERT OR UPDATE OR DELETE ON owner_sessions
FOR EACH ROW EXECUTE FUNCTION finance_catalog_session_guard();
CREATE TRIGGER catalog_recovery_guard
BEFORE INSERT OR UPDATE OR DELETE ON owner_recovery_codes
FOR EACH ROW EXECUTE FUNCTION finance_catalog_recovery_guard();
"""
