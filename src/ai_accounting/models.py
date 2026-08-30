from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Column,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Table,
    Text,
    UniqueConstraint,
    Uuid,
    and_,
    event,
    inspect,
    select,
    text,
)
from sqlalchemy.orm import Mapped, Session, attributes, mapped_column, relationship, validates

from .database import Base
from .taxpayer_identity import normalize_taxpayer_identification_number


def utcnow() -> datetime:
    return datetime.now(UTC)


CATALOG_SINGLETON_ID = 1
LEGACY_CATALOG_INSTANCE_ID = uuid.UUID(int=0)
COMPANY_STATUSES = (
    "provisioning",
    "active",
    "changing",
    "archived",
    "attention_required",
)


class CatalogMetadata(Base):
    """Identity of the local catalog database, never copied in a company handoff."""

    __tablename__ = "catalog_metadata"

    singleton_key: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    catalog_instance_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, unique=True, default=uuid.uuid4
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("singleton_key = 1", name="ck_catalog_metadata_singleton"),
    )


class CompanyRegistry(Base):
    """Safe catalog routing data; raw URLs and credentials are never persisted."""

    __tablename__ = "company_registry"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    database_name: Mapped[str] = mapped_column(String(80), nullable=False, unique=True)
    database_identity: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    taxpayer_identification_number: Mapped[str] = mapped_column(String(18), nullable=False)
    profile_effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    filing_cycle: Mapped[str] = mapped_column(String(20), nullable=False)
    urban_maintenance_rate: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    is_primary: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "taxpayer_identification_number",
            name="uq_company_registry_taxpayer_identification_number",
        ),
        CheckConstraint(
            "status IN ('provisioning','active','changing','archived','attention_required')",
            name="ck_company_registry_status",
        ),
        CheckConstraint(
            "database_name = 'finance' OR "
            "database_name ~ '^finance_company_[0-9a-f]{32}$'",
            name="ck_company_registry_database_name",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "filing_cycle IN ('monthly','quarterly')",
            name="ck_company_registry_filing_cycle",
        ),
        CheckConstraint(
            "urban_maintenance_rate IN (0.07,0.05,0.01)",
            name="ck_company_registry_urban_rate",
        ),
        CheckConstraint(
            "(status = 'archived' AND archived_at IS NOT NULL) OR "
            "(status <> 'archived' AND archived_at IS NULL)",
            name="ck_company_registry_archive_state",
        ),
        Index(
            "uq_company_registry_single_primary",
            "is_primary",
            unique=True,
            postgresql_where=text("is_primary"),
            sqlite_where=text("is_primary = 1"),
        ),
    )

    @validates("taxpayer_identification_number")
    def validate_registry_taxpayer_id(self, _key: str, value: str) -> str:
        return normalize_taxpayer_identification_number(value)


class CompanyLifecycleAction(Base):
    """Idempotent catalog-owned audit trail for cross-database lifecycle work."""

    __tablename__ = "company_lifecycle_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    action_type: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    input_facts: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False, default=dict)
    calculation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    owner_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    executor_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    executor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    executor_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint(
            "org_id",
            "action_type",
            "idempotency_key",
            name="uq_company_lifecycle_org_idempotency",
        ),
        CheckConstraint(
            "action_type IN ('create','profile_change','status_change','import')",
            name="ck_company_lifecycle_action_type",
        ),
        CheckConstraint(
            "status IN ('started','completed','failed')",
            name="ck_company_lifecycle_status",
        ),
        CheckConstraint(
            "length(request_payload_hash) = 64 AND "
            "(calculation_hash IS NULL OR length(calculation_hash) = 64)",
            name="ck_company_lifecycle_hashes",
        ),
    )


class CloseBackupLocationVersion(Base):
    """Append-only owner choice for automatic post-close backup storage."""

    __tablename__ = "close_backup_location_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    version: Mapped[int] = mapped_column(Integer, nullable=False, unique=True)
    backup_directory: Mapped[str] = mapped_column(Text, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False, unique=True)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_note: Mapped[str] = mapped_column(Text, nullable=False)
    owner_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    executor_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    executor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    executor_version: Mapped[str] = mapped_column(String(100), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_close_backup_location_version"),
        CheckConstraint(
            "length(request_payload_hash) = 64",
            name="ck_close_backup_location_request_hash",
        ),
        CheckConstraint(
            "owner_credential_version >= 1",
            name="ck_close_backup_location_credential_version",
        ),
    )


class AccountingPeriodCloseBackup(Base):
    """Catalog audit for the non-transactional filesystem side of a close."""

    __tablename__ = "accounting_period_close_backups"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("company_registry.org_id", ondelete="RESTRICT"), index=True
    )
    close_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    period_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    period_month: Mapped[str] = mapped_column(String(7), nullable=False)
    database_identity: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    location_version_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("close_backup_location_versions.id", ondelete="RESTRICT"),
        nullable=False,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    archive_file: Mapped[str | None] = mapped_column(Text, nullable=True)
    archive_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    manifest_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    requested_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("org_id", "close_id", name="uq_close_backup_org_close"),
        CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="ck_close_backup_status",
        ),
        CheckConstraint("attempt_count >= 0", name="ck_close_backup_attempt_count"),
        CheckConstraint(
            "length(period_month) = 7 AND substr(period_month, 5, 1) = '-'",
            name="ck_close_backup_period_month",
        ),
        CheckConstraint(
            "(status = 'completed' AND archive_file IS NOT NULL "
            "AND archive_sha256 IS NOT NULL AND manifest_sha256 IS NOT NULL "
            "AND error_code IS NULL AND completed_at IS NOT NULL) OR "
            "(status <> 'completed' AND archive_file IS NULL "
            "AND archive_sha256 IS NULL AND manifest_sha256 IS NULL)",
            name="ck_close_backup_completion",
        ),
    )


event_evidence = Table(
    "event_evidence",
    Base.metadata,
    Column(
        "event_id", Uuid, ForeignKey("business_events.id", ondelete="CASCADE"), primary_key=True
    ),
    Column("evidence_id", Uuid, ForeignKey("evidence.id", ondelete="RESTRICT"), primary_key=True),
    # ``event_id`` and ``evidence_id`` remain individually constrained for
    # compatibility with the original association-table shape.  The
    # organization is intentionally stored on the edge as well: the composite
    # foreign keys make a cross-enterprise evidence attachment impossible at
    # the database boundary, rather than relying on relationship loading.
    Column("org_id", Uuid, nullable=False, index=True),
    Column("relation_kind", String(30), nullable=False, default="supporting"),
    ForeignKeyConstraint(
        ["org_id", "event_id"],
        ["business_events.org_id", "business_events.id"],
        name="fk_event_evidence_org_event",
        ondelete="CASCADE",
    ),
    ForeignKeyConstraint(
        ["org_id", "evidence_id"],
        ["evidence.org_id", "evidence.id"],
        name="fk_event_evidence_org_evidence",
        ondelete="RESTRICT",
    ),
    CheckConstraint(
        "relation_kind IN ('supporting','inherited','reversal_reason')",
        name="ck_event_evidence_relation_kind",
    ),
)


class Organization(Base):
    __tablename__ = "organizations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(200))
    taxpayer_identification_number: Mapped[str] = mapped_column(String(18), nullable=False)
    taxpayer_type: Mapped[str] = mapped_column(String(30), default="small_scale")
    filing_cycle: Mapped[str] = mapped_column(String(20), default="quarterly")
    jurisdiction: Mapped[str] = mapped_column(String(100), default="CN")
    urban_maintenance_rate: Mapped[Decimal] = mapped_column(Numeric(6, 5), default=Decimal("0.07"))
    accounting_standard: Mapped[str] = mapped_column(String(50), default="small_enterprise")
    accounting_period_control_enabled: Mapped[bool] = mapped_column(
        default=True, server_default="1"
    )
    accounting_period_control_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    bank_reconciliation_scope_current_action_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, nullable=True
    )
    bank_reconciliation_scope_confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "length(taxpayer_identification_number) = 18",
            name="ck_org_taxpayer_identification_number_length",
        ),
        CheckConstraint(
            "taxpayer_identification_number = upper(taxpayer_identification_number)",
            name="ck_org_taxpayer_identification_number_uppercase",
        ),
        CheckConstraint("taxpayer_type = 'small_scale'", name="ck_org_small_scale"),
        CheckConstraint("filing_cycle IN ('monthly', 'quarterly')", name="ck_org_filing_cycle"),
        CheckConstraint(
            "urban_maintenance_rate IN (0.07, 0.05, 0.01)",
            name="ck_org_urban_rate",
        ),
        CheckConstraint(
            "accounting_period_control_enabled IS TRUE OR "
            "accounting_period_control_start_date IS NULL",
            name="ck_org_accounting_period_control",
        ),
        ForeignKeyConstraint(
            ["id", "bank_reconciliation_scope_current_action_id"],
            ["bank_reconciliation_scope_actions.org_id", "bank_reconciliation_scope_actions.id"],
            name="fk_org_bank_reconciliation_scope_current_action",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        CheckConstraint(
            "(bank_reconciliation_scope_current_action_id IS NULL "
            "AND bank_reconciliation_scope_confirmed_at IS NULL) OR "
            "(bank_reconciliation_scope_current_action_id IS NOT NULL "
            "AND bank_reconciliation_scope_confirmed_at IS NOT NULL)",
            name="ck_org_bank_reconciliation_scope_confirmation",
        ),
    )

    @validates("taxpayer_identification_number")
    def validate_taxpayer_identification_number(self, _key: str, value: str) -> str:
        return normalize_taxpayer_identification_number(value)


class OrganizationDatabaseMetadata(Base):
    """Singleton binding that prevents a catalog route from reaching the wrong database."""

    __tablename__ = "organization_database_metadata"

    singleton_key: Mapped[int] = mapped_column(Integer, primary_key=True, default=1)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    database_identity: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    current_catalog_instance_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_approval_required: Mapped[bool] = mapped_column(nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_org_database_metadata_org"
        ),
        CheckConstraint("singleton_key = 1", name="ck_org_database_metadata_singleton"),
    )


class OrganizationProfileVersion(Base):
    """Append-only, effective-dated organization and tax configuration."""

    __tablename__ = "organization_profile_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    taxpayer_identification_number: Mapped[str] = mapped_column(String(18), nullable=False)
    taxpayer_type: Mapped[str] = mapped_column(String(30), nullable=False)
    filing_cycle: Mapped[str] = mapped_column(String(20), nullable=False)
    jurisdiction: Mapped[str] = mapped_column(String(100), nullable=False)
    urban_maintenance_rate: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    accounting_standard: Mapped[str] = mapped_column(String(50), nullable=False)
    confirmation_note: Mapped[str] = mapped_column(Text, nullable=False)
    lifecycle_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_org_profile_version_org"
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_org_profile_version_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_org_profile_version_org_id"),
        UniqueConstraint(
            "org_id", "effective_from", name="uq_org_profile_version_effective_from"
        ),
        CheckConstraint("taxpayer_type = 'small_scale'", name="ck_org_profile_small_scale"),
        CheckConstraint("jurisdiction = 'CN'", name="ck_org_profile_jurisdiction"),
        CheckConstraint(
            "accounting_standard = 'small_enterprise'",
            name="ck_org_profile_accounting_standard",
        ),
        CheckConstraint(
            "filing_cycle IN ('monthly','quarterly')", name="ck_org_profile_filing_cycle"
        ),
        CheckConstraint(
            "urban_maintenance_rate IN (0.07,0.05,0.01)",
            name="ck_org_profile_urban_rate",
        ),
        CheckConstraint(
            "length(taxpayer_identification_number) = 18 AND "
            "taxpayer_identification_number = upper(taxpayer_identification_number)",
            name="ck_org_profile_taxpayer_id",
        ),
        CheckConstraint(
            "length(trim(confirmation_note)) > 0", name="ck_org_profile_confirmation_note"
        ),
    )

    @validates("taxpayer_identification_number")
    def validate_profile_taxpayer_id(self, _key: str, value: str) -> str:
        return normalize_taxpayer_identification_number(value)


class OwnerAccount(Base):
    """The sole local business owner identity for this deployment."""

    __tablename__ = "owner_accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    singleton_key: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    login_name: Mapped[str] = mapped_column(String(100), nullable=False)
    login_name_normalized: Mapped[str] = mapped_column(String(100), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    password_hash: Mapped[str] = mapped_column(String(512), nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    password_failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    password_throttled_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    recovery_failed_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    recovery_throttled_until: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    last_authenticated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    password_changed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_owner_account_org",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("singleton_key", name="uq_owner_account_singleton"),
        UniqueConstraint("org_id", name="uq_owner_account_org"),
        UniqueConstraint("login_name_normalized", name="uq_owner_account_login_normalized"),
        UniqueConstraint("org_id", "id", name="uq_owner_account_org_id"),
        CheckConstraint("singleton_key = 1", name="ck_owner_account_singleton"),
        CheckConstraint(
            "length(login_name) BETWEEN 3 AND 100 AND login_name = trim(login_name)",
            name="ck_owner_account_login_name",
        ),
        CheckConstraint(
            "login_name_normalized = lower(trim(login_name))",
            name="ck_owner_account_login_normalized",
        ),
        CheckConstraint(
            "length(password_hash) = 97 AND password_hash LIKE '$argon2id$v=19$m=65536,t=3,p=4$%'",
            name="ck_owner_account_password_hash",
        ),
        CheckConstraint(
            "login_name ~ '^[A-Za-z0-9][A-Za-z0-9._-]{2,99}$'",
            name="ck_owner_account_login_ascii",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "login_name NOT GLOB '*[^A-Za-z0-9._-]*' "
            "AND substr(login_name, 1, 1) GLOB '[A-Za-z0-9]'",
            name="ck_owner_account_login_ascii",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "password_hash ~ "
            "'^\\$argon2id\\$v=19\\$m=65536,t=3,p=4\\$[A-Za-z0-9+/]{22}\\$"
            "[A-Za-z0-9+/]{43}$'",
            name="ck_owner_account_password_hash_shape",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint("status IN ('active','disabled')", name="ck_owner_account_status"),
        CheckConstraint("credential_version >= 1", name="ck_owner_account_credential_version"),
        CheckConstraint("password_failed_attempts >= 0", name="ck_owner_account_password_failures"),
        CheckConstraint("recovery_failed_attempts >= 0", name="ck_owner_account_recovery_failures"),
    )


class OwnerSession(Base):
    """A server-side session containing only the SHA-256 digest of its secret."""

    __tablename__ = "owner_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    owner_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    secret_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    idle_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    absolute_expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoke_reason: Mapped[str | None] = mapped_column(String(50), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "owner_account_id"],
            ["owner_accounts.org_id", "owner_accounts.id"],
            name="fk_owner_session_org_account",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("secret_sha256", name="uq_owner_session_secret_sha256"),
        UniqueConstraint("org_id", "id", name="uq_owner_session_org_id"),
        UniqueConstraint(
            "org_id",
            "owner_account_id",
            "id",
            "credential_version",
            name="uq_owner_session_execution_authority",
        ),
        CheckConstraint("length(secret_sha256) = 64", name="ck_owner_session_secret_sha256"),
        CheckConstraint(
            "secret_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_owner_session_secret_lowerhex",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "secret_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_owner_session_secret_lowerhex",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint("credential_version >= 1", name="ck_owner_session_credential_version"),
        CheckConstraint("last_seen_at >= created_at", name="ck_owner_session_last_seen"),
        CheckConstraint("idle_expires_at > created_at", name="ck_owner_session_idle_expiry"),
        CheckConstraint(
            "absolute_expires_at > created_at", name="ck_owner_session_absolute_expiry"
        ),
        CheckConstraint(
            "idle_expires_at <= absolute_expires_at", name="ck_owner_session_expiry_order"
        ),
        CheckConstraint(
            "(revoked_at IS NULL AND revoke_reason IS NULL) OR "
            "(revoked_at IS NOT NULL AND revoke_reason IS NOT NULL)",
            name="ck_owner_session_revocation_state",
        ),
        CheckConstraint(
            "revoked_at IS NULL OR revoked_at >= created_at",
            name="ck_owner_session_revoked_at",
        ),
        CheckConstraint(
            "revoke_reason IS NULL OR revoke_reason IN "
            "('logout','credential_changed','recovery_used','idle_expired',"
            "'absolute_expired','credential_version_mismatch')",
            name="ck_owner_session_revoke_reason",
        ),
    )


class OwnerRecoveryCode(Base):
    """One-time recovery-code history; only a SHA-256 digest is persisted."""

    __tablename__ = "owner_recovery_codes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    owner_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    code_sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "owner_account_id"],
            ["owner_accounts.org_id", "owner_accounts.id"],
            name="fk_owner_recovery_code_org_account",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("code_sha256", name="uq_owner_recovery_code_sha256"),
        UniqueConstraint("org_id", "id", name="uq_owner_recovery_code_org_id"),
        CheckConstraint("length(code_sha256) = 64", name="ck_owner_recovery_code_sha256"),
        CheckConstraint(
            "code_sha256 ~ '^[0-9a-f]{64}$'",
            name="ck_owner_recovery_code_lowerhex",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "code_sha256 NOT GLOB '*[^0-9a-f]*'",
            name="ck_owner_recovery_code_lowerhex",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "credential_version >= 1", name="ck_owner_recovery_code_credential_version"
        ),
        CheckConstraint(
            "used_at IS NULL OR invalidated_at IS NULL",
            name="ck_owner_recovery_code_terminal_state",
        ),
        CheckConstraint(
            "used_at IS NULL OR used_at >= created_at",
            name="ck_owner_recovery_code_used_at",
        ),
        CheckConstraint(
            "invalidated_at IS NULL OR invalidated_at >= created_at",
            name="ck_owner_recovery_code_invalidated_at",
        ),
    )


class IdentityAuditEvent(Base):
    """Fixed-shape, append-only local identity security audit event."""

    __tablename__ = "identity_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    owner_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    session_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    outcome: Mapped[str] = mapped_column(String(20), nullable=False)
    reason_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    request_correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_identity_audit_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "owner_account_id"],
            ["owner_accounts.org_id", "owner_accounts.id"],
            name="fk_identity_audit_org_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "session_id"],
            ["owner_sessions.org_id", "owner_sessions.id"],
            name="fk_identity_audit_org_session",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_type IN ('owner_provisioned','login_succeeded','login_failed',"
            "'session_revoked','session_expired','password_changed',"
            "'recovery_succeeded','recovery_failed','recovery_code_replaced')",
            name="ck_identity_audit_event_type",
        ),
        CheckConstraint(
            "outcome IN ('succeeded','rejected','blocked')",
            name="ck_identity_audit_outcome",
        ),
        CheckConstraint(
            "reason_code IS NULL OR reason_code IN "
            "('INVALID_CREDENTIALS','ACCOUNT_THROTTLED','ACCOUNT_DISABLED',"
            "'SESSION_REVOKED','SESSION_IDLE_EXPIRED','SESSION_ABSOLUTE_EXPIRED',"
            "'SESSION_CREDENTIAL_VERSION_MISMATCH','RECOVERY_CODE_INVALID',"
            "'RECOVERY_THROTTLED','PASSWORD_POLICY_REJECTED','OWNER_ALREADY_PROVISIONED')",
            name="ck_identity_audit_reason_code",
        ),
    )


class ExecutionAttribution(Base):
    """Immutable owner authority and server executor frozen for one write call."""

    __tablename__ = "execution_attributions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    catalog_instance_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, default=lambda: LEGACY_CATALOG_INSTANCE_ID
    )
    owner_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    executor_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    executor_name: Mapped[str] = mapped_column(String(100), nullable=False)
    executor_version: Mapped[str] = mapped_column(String(100), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(100), nullable=False)
    request_correlation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
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
        UniqueConstraint("org_id", "id", name="uq_execution_attribution_org_id"),
        UniqueConstraint(
            "request_correlation_id",
            name="uq_execution_attribution_request_correlation",
        ),
        CheckConstraint(
            "owner_credential_version >= 1",
            name="ck_execution_attribution_credential_version",
        ),
        CheckConstraint(
            "executor_kind IN ('ai_agent','deterministic_kernel','system_job')",
            name="ck_execution_attribution_executor_kind",
        ),
        CheckConstraint(
            "length(executor_name) BETWEEN 1 AND 100",
            name="ck_execution_attribution_executor_name",
        ),
        CheckConstraint(
            "length(executor_version) BETWEEN 1 AND 100",
            name="ck_execution_attribution_executor_version",
        ),
        CheckConstraint(
            "length(tool_name) BETWEEN 1 AND 100",
            name="ck_execution_attribution_tool_name",
        ),
        CheckConstraint(
            "executor_name ~ '^[A-Za-z0-9._:-]{1,100}$'",
            name="ck_execution_attribution_executor_name_ascii",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "executor_version ~ '^[A-Za-z0-9._:-]{1,100}$'",
            name="ck_execution_attribution_executor_version_ascii",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "tool_name ~ '^finance_[a-z0-9_]{1,92}$'",
            name="ck_execution_attribution_tool_name_ascii",
        ).ddl_if(dialect="postgresql"),
    )


Index(
    "uq_owner_recovery_code_current",
    OwnerRecoveryCode.owner_account_id,
    unique=True,
    postgresql_where=(
        OwnerRecoveryCode.used_at.is_(None) & OwnerRecoveryCode.invalidated_at.is_(None)
    ),
    sqlite_where=(OwnerRecoveryCode.used_at.is_(None) & OwnerRecoveryCode.invalidated_at.is_(None)),
)


class Account(Base):
    __tablename__ = "accounts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    code: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(100))
    category: Mapped[str] = mapped_column(String(30))
    normal_side: Mapped[str] = mapped_column(String(10))
    system_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    active: Mapped[bool] = mapped_column(default=True)
    requires_bank_reconciliation: Mapped[bool] = mapped_column(
        nullable=False, default=False, server_default="0"
    )
    bank_reconciliation_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    bank_reconciliation_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    bank_reconciliation_configured_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    __table_args__ = (
        UniqueConstraint("org_id", "code", name="uq_account_org_code"),
        UniqueConstraint("org_id", "system_role", name="uq_account_org_role"),
        UniqueConstraint("org_id", "id", name="uq_account_org_id"),
        CheckConstraint("normal_side IN ('debit', 'credit')", name="ck_account_normal_side"),
        CheckConstraint(
            "(requires_bank_reconciliation IS FALSE "
            "AND bank_reconciliation_start_date IS NULL "
            "AND bank_reconciliation_end_date IS NULL) OR "
            "(requires_bank_reconciliation IS TRUE "
            "AND bank_reconciliation_start_date IS NOT NULL "
            "AND bank_reconciliation_configured_at IS NOT NULL)",
            name="ck_account_bank_reconciliation_scope",
        ),
        CheckConstraint(
            "bank_reconciliation_start_date IS NULL OR "
            "substr(CAST(bank_reconciliation_start_date AS VARCHAR), 9, 2) = '01'",
            name="ck_account_bank_reconciliation_start_month",
        ),
        CheckConstraint(
            "bank_reconciliation_end_date IS NULL OR "
            "CAST(substr(CAST(bank_reconciliation_end_date AS VARCHAR), 9, 2) "
            "AS INTEGER) BETWEEN 28 AND 31",
            name="ck_account_bank_reconciliation_end_month",
        ),
        CheckConstraint(
            "bank_reconciliation_end_date IS NULL OR "
            "bank_reconciliation_start_date <= bank_reconciliation_end_date",
            name="ck_account_bank_reconciliation_dates",
        ),
        CheckConstraint(
            "requires_bank_reconciliation IS FALSE OR "
            "(active IS TRUE AND category = 'asset' AND normal_side = 'debit')",
            name="ck_account_bank_reconciliation_account_shape",
        ),
    )


class BankReconciliationScopeAction(Base):
    """Immutable owner-confirmed bank-account scope snapshot (DEC-038 A)."""

    __tablename__ = "bank_reconciliation_scope_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    action_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    previous_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    target_account_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    calculation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    scope_snapshot: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_field_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    execution_attribution_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_bank_scope_action_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "previous_action_id"],
            ["bank_reconciliation_scope_actions.org_id", "bank_reconciliation_scope_actions.id"],
            name="fk_bank_scope_action_previous",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "target_account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_bank_scope_action_target_account",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_bank_scope_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_bank_scope_action_org_id"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_bank_scope_action_idempotency"),
        CheckConstraint("status IN ('posted','rejected')", name="ck_bank_scope_action_status"),
        CheckConstraint(
            "action_type IS NULL OR action_type IN ('initial_confirmation','scope_change')",
            name="ck_bank_scope_action_type",
        ),
        CheckConstraint(
            "(status = 'posted' AND action_type IS NOT NULL "
            "AND calculation_payload IS NOT NULL AND calculation_hash IS NOT NULL "
            "AND scope_snapshot IS NOT NULL AND explanation IS NOT NULL "
            "AND length(trim(explanation)) BETWEEN 1 AND 2000 "
            "AND error_code IS NULL AND error_field_path IS NULL AND error_count = 0) OR "
            "(status = 'rejected' AND action_type IS NULL "
            "AND previous_action_id IS NULL AND target_account_id IS NULL "
            "AND calculation_payload IS NULL AND calculation_hash IS NULL "
            "AND scope_snapshot IS NULL AND explanation IS NULL "
            "AND error_code IS NOT NULL AND error_count > 0)",
            name="ck_bank_scope_action_payload_shape",
        ),
        CheckConstraint(
            "status <> 'posted' OR "
            "(action_type = 'initial_confirmation' AND previous_action_id IS NULL "
            "AND target_account_id IS NULL) OR "
            "(action_type = 'scope_change' AND previous_action_id IS NOT NULL "
            "AND target_account_id IS NOT NULL)",
            name="ck_bank_scope_action_lineage",
        ),
        CheckConstraint(
            "length(request_payload_hash) = 64 AND "
            "(calculation_payload IS NULL OR length(calculation_payload) > 0) AND "
            "(calculation_hash IS NULL OR length(calculation_hash) = 64)",
            name="ck_bank_scope_action_hashes",
        ),
    )


class BankReconciliationScopeActionEvidence(Base):
    __tablename__ = "bank_reconciliation_scope_action_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_sha256_at_action: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["bank_reconciliation_scope_actions.org_id", "bank_reconciliation_scope_actions.id"],
            name="fk_bank_scope_action_evidence_org_action",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_bank_scope_action_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(evidence_sha256_at_action) = 64",
            name="ck_bank_scope_action_evidence_hash",
        ),
    )


class AccountBankReconciliationScopeHistory(Base):
    """Append-only audit of changes to current Account reconciliation fields."""

    __tablename__ = "account_bank_reconciliation_scope_history"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    scope_action_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    old_required: Mapped[bool] = mapped_column(nullable=False)
    old_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    old_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    new_required: Mapped[bool] = mapped_column(nullable=False)
    new_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    new_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_account_bank_scope_history_org_account",
            ondelete="RESTRICT",
            deferrable=True,
            initially="DEFERRED",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_account_bank_scope_history_execution_attribution",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "scope_action_id"],
            ["bank_reconciliation_scope_actions.org_id", "bank_reconciliation_scope_actions.id"],
            name="fk_account_bank_scope_history_org_action",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_account_bank_scope_history_org_id"),
    )


class Counterparty(Base):
    __tablename__ = "counterparties"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(30))
    name: Mapped[str] = mapped_column(String(200))
    external_ref: Mapped[str | None] = mapped_column(String(100), nullable=True)

    __table_args__ = (
        UniqueConstraint("org_id", "kind", "name", name="uq_counterparty_identity"),
        UniqueConstraint("org_id", "id", name="uq_counterparty_org_id"),
        CheckConstraint(
            "kind IN ('customer','supplier','employee','owner','other','labor_person')",
            name="ck_counterparty_kind",
        ),
    )


class Employee(Base):
    __tablename__ = "employees"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    prior_labor_person_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    employee_code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    employment_start_date: Mapped[date] = mapped_column(Date)
    tax_withholding_start_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    employment_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), default="active")
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    counterparty: Mapped[Counterparty] = relationship(lazy="joined")

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "counterparty_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_employee_org_counterparty",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_employee_execution_attribution",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "prior_labor_person_id"],
            ["labor_service_persons.org_id", "labor_service_persons.id"],
            name="fk_employee_org_prior_labor_person",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_employee_org_id"),
        UniqueConstraint("org_id", "employee_code", name="uq_employee_org_code"),
        UniqueConstraint("counterparty_id", name="uq_employee_counterparty"),
        UniqueConstraint("prior_labor_person_id", name="uq_employee_prior_labor_person"),
        CheckConstraint(
            "employment_end_date IS NULL OR employment_start_date <= employment_end_date",
            name="ck_employee_employment_dates",
        ),
        CheckConstraint(
            "tax_withholding_start_date IS NULL OR "
            "employment_start_date <= tax_withholding_start_date",
            name="ck_employee_tax_withholding_start",
        ),
        CheckConstraint("status IN ('active','inactive','terminated')", name="ck_employee_status"),
    )


class EmployeePayrollProfileVersion(Base):
    __tablename__ = "employee_payroll_profile_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    expense_role: Mapped[str] = mapped_column(String(50))
    social_insurance_base_fen: Mapped[int] = mapped_column(BigInteger)
    housing_fund_base_fen: Mapped[int] = mapped_column(BigInteger)
    social_insurance_participating: Mapped[bool] = mapped_column(default=True)
    housing_fund_participating: Mapped[bool] = mapped_column(default=True)
    resident_employee: Mapped[bool | None] = mapped_column(nullable=True)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_payroll_profile_org_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_payroll_profile_execution_attribution",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "employee_id", "supersedes_id"],
            [
                "employee_payroll_profile_versions.org_id",
                "employee_payroll_profile_versions.employee_id",
                "employee_payroll_profile_versions.id",
            ],
            name="fk_payroll_profile_supersedes",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_payroll_profile_org_id"),
        UniqueConstraint(
            "org_id",
            "employee_id",
            "id",
            name="uq_payroll_profile_org_employee_id",
        ),
        UniqueConstraint("supersedes_id", name="uq_payroll_profile_successor"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to",
            name="ck_employee_payroll_profile_dates",
        ),
        CheckConstraint(
            "expense_role IN ('payroll_management_expense','payroll_sales_expense',"
            "'payroll_service_cost')",
            name="ck_employee_payroll_profile_expense_role",
        ),
        CheckConstraint(
            "social_insurance_base_fen >= 0 AND housing_fund_base_fen >= 0",
            name="ck_employee_payroll_profile_bases",
        ),
    )


class PayrollFirstWageTaxTreatment(Base):
    """Evidenced employee-year treatment for the annual first-wage deduction rule."""

    __tablename__ = "payroll_first_wage_tax_treatments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    tax_year: Mapped[int] = mapped_column(Integer, nullable=False, index=True)
    first_wage_month: Mapped[int] = mapped_column(Integer, nullable=False)
    treatment_state: Mapped[str] = mapped_column(String(20), nullable=False)
    declaration_date: Mapped[date] = mapped_column(Date, nullable=False)
    confirmation_description: Mapped[str] = mapped_column(Text, nullable=False)
    legal_basis_url: Mapped[str] = mapped_column(String(1000), nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, unique=True)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_first_wage_treatment_org_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_first_wage_treatment_execution_attribution",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "supersedes_id"],
            ["payroll_first_wage_tax_treatments.org_id", "payroll_first_wage_tax_treatments.id"],
            name="fk_first_wage_treatment_org_supersedes",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_first_wage_treatment_org_id"),
        UniqueConstraint(
            "org_id", "idempotency_key", name="uq_first_wage_treatment_idempotency"
        ),
        CheckConstraint("tax_year BETWEEN 1900 AND 9999", name="ck_first_wage_treatment_year"),
        CheckConstraint(
            "first_wage_month BETWEEN 1 AND 12", name="ck_first_wage_treatment_month"
        ),
        CheckConstraint(
            "treatment_state IN ('eligible','not_eligible')",
            name="ck_first_wage_treatment_state",
        ),
    )


Index(
    "uq_first_wage_treatment_root",
    PayrollFirstWageTaxTreatment.org_id,
    PayrollFirstWageTaxTreatment.employee_id,
    PayrollFirstWageTaxTreatment.tax_year,
    unique=True,
    postgresql_where=PayrollFirstWageTaxTreatment.supersedes_id.is_(None),
    sqlite_where=PayrollFirstWageTaxTreatment.supersedes_id.is_(None),
)


class PayrollFirstWageTaxTreatmentEvidence(Base):
    __tablename__ = "payroll_first_wage_tax_treatment_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    treatment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "treatment_id"],
            ["payroll_first_wage_tax_treatments.org_id", "payroll_first_wage_tax_treatments.id"],
            name="fk_first_wage_treatment_evidence_org_treatment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_first_wage_treatment_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
    )


class PayrollFirstWageTaxTreatmentUse(Base):
    """Normalized dependency from a payroll draft to the employee-year treatment fact."""

    __tablename__ = "payroll_first_wage_tax_treatment_uses"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    treatment_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    payroll_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "treatment_id"],
            ["payroll_first_wage_tax_treatments.org_id", "payroll_first_wage_tax_treatments.id"],
            name="fk_first_wage_treatment_use_org_treatment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_first_wage_treatment_use_org_batch",
            ondelete="RESTRICT",
        ),
    )


class PayrollContributionActualSet(Base):
    """Immutable evidenced actual assessment facts for an employee contribution month."""

    __tablename__ = "payroll_contribution_actual_sets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    contribution_period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    declaration_date: Mapped[date] = mapped_column(Date, nullable=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_description: Mapped[str] = mapped_column(Text, nullable=False)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_contribution_actual_set_org_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_contribution_actual_set_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_contribution_actual_set_org_id"),
        UniqueConstraint(
            "org_id", "idempotency_key", name="uq_contribution_actual_set_idempotency"
        ),
        CheckConstraint(
            "length(contribution_period) = 7 AND substr(contribution_period, 5, 1) = '-' "
            "AND substr(contribution_period, 6, 2) BETWEEN '01' AND '12'",
            name="ck_contribution_actual_set_period",
        ),
        CheckConstraint(
            "reason_code IN ('late_enrollment','missing_declaration','partial_declaration',"
            "'agency_assessment','documented_correction','other_documented')",
            name="ck_contribution_actual_set_reason",
        ),
    )


class PayrollContributionActualItem(Base):
    """One employee/month/contribution-kind actual amount, never a policy mutation."""

    __tablename__ = "payroll_contribution_actual_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    actual_set_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    contribution_period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    contribution_group: Mapped[str] = mapped_column(String(30), nullable=False)
    insurance_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    actual_state: Mapped[str] = mapped_column(String(20), nullable=False)
    employee_amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    employer_amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "actual_set_id"],
            ["payroll_contribution_actual_sets.org_id", "payroll_contribution_actual_sets.id"],
            name="fk_contribution_actual_item_org_set",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_contribution_actual_item_org_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "supersedes_id"],
            ["payroll_contribution_actual_items.org_id", "payroll_contribution_actual_items.id"],
            name="fk_contribution_actual_item_org_supersedes",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_contribution_actual_item_org_id"),
        UniqueConstraint(
            "actual_set_id",
            "contribution_group",
            "insurance_kind",
            name="uq_contribution_actual_item_set_kind",
        ),
        CheckConstraint(
            "contribution_group IN ('social_insurance','housing_fund')",
            name="ck_contribution_actual_item_group",
        ),
        CheckConstraint(
            "actual_state IN ('declared','not_declared')",
            name="ck_contribution_actual_item_state",
        ),
        CheckConstraint(
            "employee_amount_fen >= 0 AND employer_amount_fen >= 0",
            name="ck_contribution_actual_item_amounts",
        ),
        CheckConstraint(
            "actual_state <> 'not_declared' OR "
            "(employee_amount_fen = 0 AND employer_amount_fen = 0)",
            name="ck_contribution_actual_item_non_declaration_zero",
        ),
    )


Index(
    "uq_contribution_actual_root_kind",
    PayrollContributionActualItem.org_id,
    PayrollContributionActualItem.employee_id,
    PayrollContributionActualItem.contribution_period,
    PayrollContributionActualItem.contribution_group,
    PayrollContributionActualItem.insurance_kind,
    unique=True,
    postgresql_where=PayrollContributionActualItem.supersedes_id.is_(None),
    sqlite_where=PayrollContributionActualItem.supersedes_id.is_(None),
)


class PayrollContributionActualEvidence(Base):
    __tablename__ = "payroll_contribution_actual_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    actual_set_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "actual_set_id"],
            ["payroll_contribution_actual_sets.org_id", "payroll_contribution_actual_sets.id"],
            name="fk_contribution_actual_evidence_org_set",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_contribution_actual_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
    )


class PayrollContributionActualUse(Base):
    """Normalized dependency that freezes the actual facts used by a payroll draft."""

    __tablename__ = "payroll_contribution_actual_uses"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    actual_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    payroll_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "actual_item_id"],
            ["payroll_contribution_actual_items.org_id", "payroll_contribution_actual_items.id"],
            name="fk_contribution_actual_use_org_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_contribution_actual_use_org_batch",
            ondelete="RESTRICT",
        ),
    )


class PayrollContributionSupplement(Base):
    """Typed historical contribution accrual; its event date is never the old payroll month."""

    __tablename__ = "payroll_contribution_supplements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    source_payroll_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    contribution_period: Mapped[str] = mapped_column(String(7), nullable=False, index=True)
    assessment_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    reason_code: Mapped[str] = mapped_column(String(40), nullable=False)
    reason_description: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_contribution_supplement_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_contribution_supplement_org_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_contribution_supplement_org_source_batch",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_contribution_supplement_org_id"),
        UniqueConstraint(
            "org_id",
            "employee_id",
            "assessment_reference",
            name="uq_contribution_supplement_assessment",
        ),
        CheckConstraint(
            "length(contribution_period) = 7 AND substr(contribution_period, 5, 1) = '-' "
            "AND substr(contribution_period, 6, 2) BETWEEN '01' AND '12'",
            name="ck_contribution_supplement_period",
        ),
        CheckConstraint(
            "reason_code IN ('late_enrollment','missing_declaration','agency_assessment',"
            "'documented_correction','other_documented')",
            name="ck_contribution_supplement_reason",
        ),
    )


class PayrollContributionSupplementItem(Base):
    __tablename__ = "payroll_contribution_supplement_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    supplement_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    contribution_group: Mapped[str] = mapped_column(String(30), nullable=False)
    insurance_kind: Mapped[str] = mapped_column(String(50), nullable=False)
    employee_amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    employer_amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    employee_amount_treatment: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "supplement_id"],
            ["payroll_contribution_supplements.org_id", "payroll_contribution_supplements.id"],
            name="fk_contribution_supplement_item_org_supplement",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_contribution_supplement_item_org_id"),
        UniqueConstraint(
            "supplement_id",
            "contribution_group",
            "insurance_kind",
            name="uq_contribution_supplement_item_kind",
        ),
        CheckConstraint(
            "contribution_group IN ('social_insurance','housing_fund')",
            name="ck_contribution_supplement_item_group",
        ),
        CheckConstraint(
            "employee_amount_fen >= 0 AND employer_amount_fen >= 0 "
            "AND employee_amount_fen + employer_amount_fen > 0",
            name="ck_contribution_supplement_item_amounts",
        ),
        CheckConstraint(
            "employee_amount_treatment IN ('employer_borne','employee_receivable')",
            name="ck_contribution_supplement_item_treatment",
        ),
    )


class PayrollPolicyVersion(Base):
    __tablename__ = "payroll_policy_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    region: Mapped[str] = mapped_column(String(100))
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    version: Mapped[str] = mapped_column(String(50))
    source_url: Mapped[str] = mapped_column(Text)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_payroll_policy_org_id"),
        UniqueConstraint("org_id", "region", "id", name="uq_payroll_policy_org_region_id"),
        UniqueConstraint("org_id", "region", "version", name="uq_payroll_policy_version"),
        UniqueConstraint("supersedes_id", name="uq_payroll_policy_successor"),
        ForeignKeyConstraint(
            ["org_id", "region", "supersedes_id"],
            [
                "payroll_policy_versions.org_id",
                "payroll_policy_versions.region",
                "payroll_policy_versions.id",
            ],
            name="fk_payroll_policy_supersedes",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_payroll_policy_execution_attribution",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to",
            name="ck_payroll_policy_dates",
        ),
    )


class PayrollVersionGuard(Base):
    """Persistent transaction-lock domain for a payroll version dimension.

    PostgreSQL triggers derive ``dimension_key`` from the version row; callers
    never choose it.  Locking this durable row closes the write-skew gap that a
    deferred lineage assertion alone cannot see across two transactions.
    """

    __tablename__ = "payroll_version_guards"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    guard_kind: Mapped[str] = mapped_column(String(20), primary_key=True)
    dimension_key: Mapped[str] = mapped_column(String(300), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "guard_kind IN ('profile','policy','opening')",
            name="ck_payroll_version_guard_kind",
        ),
        CheckConstraint("length(dimension_key) > 0", name="ck_payroll_version_guard_dimension"),
    )


class PayrollBatch(Base):
    __tablename__ = "payroll_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    batch_kind: Mapped[str] = mapped_column(String(30))
    payroll_period: Mapped[str] = mapped_column(String(7))
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20), default="calculated")
    calculation_hash: Mapped[str] = mapped_column(String(64))
    request_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    calculation_input: Mapped[dict[str, Any]] = mapped_column(JSON)
    calculation_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    posting_date: Mapped[date] = mapped_column(Date)
    payment_date: Mapped[date] = mapped_column(Date)
    tax_method: Mapped[str | None] = mapped_column(String(20), nullable=True)
    confirmed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confirmation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    business_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, unique=True)
    reversal_of_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, unique=True)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "policy_version_id"],
            ["payroll_policy_versions.org_id", "payroll_policy_versions.id"],
            name="fk_payroll_batch_org_policy",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_payroll_batch_execution_attribution",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "business_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_payroll_batch_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "reversal_of_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_batch_org_reversal",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_payroll_batch_org_id"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_payroll_batch_idempotency"),
        UniqueConstraint("org_id", "calculation_hash", name="uq_payroll_batch_calculation_hash"),
        UniqueConstraint(
            "org_id", "batch_kind", "payroll_period", "version", name="uq_payroll_batch_version"
        ),
        CheckConstraint("batch_kind IN ('regular','annual_bonus')", name="ck_payroll_batch_kind"),
        CheckConstraint(
            "length(payroll_period) = 7 AND substr(payroll_period, 5, 1) = '-' AND "
            "substr(payroll_period, 6, 2) BETWEEN '01' AND '12'",
            name="ck_payroll_batch_period",
        ),
        CheckConstraint("version > 0", name="ck_payroll_batch_version_positive"),
        CheckConstraint(
            "status IN ('draft','calculated','posted','reversed','superseded')",
            name="ck_payroll_batch_status",
        ),
        CheckConstraint(
            "tax_method IS NULL OR tax_method IN ('separate','combined')",
            name="ck_payroll_batch_tax_method",
        ),
        CheckConstraint(
            "status <> 'posted' OR batch_kind = 'regular' OR tax_method IS NOT NULL",
            name="ck_payroll_batch_posted_bonus_tax_method",
        ),
    )


class PayrollBatchVersionSequence(Base):
    """Database-owned allocator for immutable payroll draft versions.

    Services must lock this row (or atomically upsert it) before consuming a
    version.  ``max(version) + 1`` is not safe when previews run concurrently.
    """

    __tablename__ = "payroll_batch_version_sequences"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    batch_kind: Mapped[str] = mapped_column(String(30), primary_key=True)
    payroll_period: Mapped[str] = mapped_column(String(7), primary_key=True)
    next_version: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (
        CheckConstraint(
            "batch_kind IN ('regular','annual_bonus')", name="ck_payroll_sequence_kind"
        ),
        CheckConstraint(
            "length(payroll_period) = 7 AND substr(payroll_period, 5, 1) = '-' AND "
            "substr(payroll_period, 6, 2) BETWEEN '01' AND '12'",
            name="ck_payroll_sequence_period",
        ),
        CheckConstraint("next_version > 0", name="ck_payroll_sequence_next_version"),
    )


class PayrollLine(Base):
    __tablename__ = "payroll_lines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    payroll_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    regular_payroll_batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    employee_payroll_profile_version_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    wage_tax_declaration_state: Mapped[str] = mapped_column(String(20), default="declared")
    tax_reported_salary_fen: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    tax_reporting_difference_reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    special_additional_deduction_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    other_legal_deduction_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    annual_bonus_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    employee_social_insurance_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    employer_social_insurance_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    employee_housing_fund_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    employer_housing_fund_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    employee_social_insurance_items: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    employer_social_insurance_items: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    employee_housing_fund_items: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    employer_housing_fund_items: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    individual_income_tax_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    gross_salary_fen: Mapped[int] = mapped_column(BigInteger)
    net_salary_fen: Mapped[int] = mapped_column(BigInteger)
    calculation_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_line_org_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "regular_payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_line_org_regular_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_payroll_line_org_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "employee_id", "employee_payroll_profile_version_id"],
            [
                "employee_payroll_profile_versions.org_id",
                "employee_payroll_profile_versions.employee_id",
                "employee_payroll_profile_versions.id",
            ],
            name="fk_payroll_line_org_employee_profile",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_payroll_line_org_id"),
        UniqueConstraint(
            "org_id",
            "payroll_batch_id",
            "employee_id",
            "id",
            name="uq_payroll_line_org_batch_employee_id",
        ),
        UniqueConstraint("payroll_batch_id", "employee_id", name="uq_payroll_line_employee"),
        CheckConstraint(
            "(tax_reported_salary_fen IS NULL OR tax_reported_salary_fen >= 0) AND "
            "special_additional_deduction_fen >= 0 AND "
            "other_legal_deduction_fen >= 0 AND annual_bonus_fen >= 0 AND "
            "employee_social_insurance_fen >= 0 AND employer_social_insurance_fen >= 0 AND "
            "employee_housing_fund_fen >= 0 AND employer_housing_fund_fen >= 0 AND "
            "individual_income_tax_fen >= 0 AND gross_salary_fen >= 0",
            name="ck_payroll_line_nonnegative_amounts",
        ),
        CheckConstraint(
            "((wage_tax_declaration_state = 'declared' AND "
            "tax_reported_salary_fen IS NOT NULL AND annual_bonus_fen = 0 AND "
            "((gross_salary_fen = tax_reported_salary_fen AND "
            "tax_reporting_difference_reason IS NULL) OR "
            "(gross_salary_fen <> tax_reported_salary_fen AND "
            "tax_reporting_difference_reason IS NOT NULL AND "
            "length(trim(tax_reporting_difference_reason)) BETWEEN 1 AND 2000))) OR "
            "(wage_tax_declaration_state = 'not_declared' AND "
            "tax_reported_salary_fen IS NULL AND annual_bonus_fen = 0 AND "
            "gross_salary_fen = 0 AND tax_reporting_difference_reason IS NULL) OR "
            "(wage_tax_declaration_state = 'not_applicable' AND "
            "tax_reported_salary_fen IS NULL AND annual_bonus_fen > 0 AND "
            "gross_salary_fen = annual_bonus_fen AND "
            "tax_reporting_difference_reason IS NULL))",
            name="ck_payroll_line_gross_salary",
        ),
        CheckConstraint(
            "wage_tax_declaration_state IN ('declared','not_declared','not_applicable')",
            name="ck_payroll_line_wage_tax_declaration_state",
        ),
        CheckConstraint(
            "net_salary_fen = gross_salary_fen - employee_social_insurance_fen - "
            "employee_housing_fund_fen - individual_income_tax_fen AND net_salary_fen >= 0",
            name="ck_payroll_line_net_salary",
        ),
    )


class PayrollWithholdingAllocation(Base):
    """One payment event's statutory withholdings for one immutable payroll line."""

    __tablename__ = "payroll_withholding_allocations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    payroll_line_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payment_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    employee_social_insurance_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    employee_housing_fund_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    individual_income_tax_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    reversed: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "payroll_line_id"],
            ["payroll_lines.org_id", "payroll_lines.id"],
            name="fk_withholding_allocation_org_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_withholding_allocation_org_payment_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "org_id",
            "payroll_line_id",
            "payment_event_id",
            name="uq_withholding_allocation_line_event",
        ),
        CheckConstraint(
            "employee_social_insurance_fen >= 0 AND employee_housing_fund_fen >= 0 AND "
            "individual_income_tax_fen >= 0",
            name="ck_withholding_allocation_nonnegative",
        ),
    )


class PayrollWithholdingEntitlement(Base):
    """Immutable per-kind withholding entitlement formalized from a payroll line."""

    __tablename__ = "payroll_withholding_entitlements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    payroll_line_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    contribution_group: Mapped[str] = mapped_column(String(50))
    insurance_kind: Mapped[str] = mapped_column(String(50))
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "payroll_line_id"],
            ["payroll_lines.org_id", "payroll_lines.id"],
            name="fk_withholding_entitlement_org_line",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_withholding_entitlement_org_id"),
        UniqueConstraint(
            "org_id",
            "payroll_line_id",
            "contribution_group",
            "insurance_kind",
            name="uq_withholding_entitlement_kind",
        ),
        CheckConstraint(
            "contribution_group IN ('employee_social_insurance','employee_housing_fund',"
            "'individual_income_tax')",
            name="ck_withholding_entitlement_group",
        ),
        CheckConstraint("amount_fen >= 0", name="ck_withholding_entitlement_amount"),
    )


class PayrollWithholdingPaymentAllocation(Base):
    """A payment event's allocation to one formal withholding entitlement."""

    __tablename__ = "payroll_withholding_payment_allocations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    entitlement_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    payment_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    reversed: Mapped[bool] = mapped_column(default=False)
    reversed_by_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "entitlement_id"],
            ["payroll_withholding_entitlements.org_id", "payroll_withholding_entitlements.id"],
            name="fk_withholding_payment_org_entitlement",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_withholding_payment_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "reversed_by_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_withholding_payment_org_reversal_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "org_id",
            "entitlement_id",
            "payment_event_id",
            name="uq_withholding_payment_entitlement_event",
        ),
        CheckConstraint("amount_fen > 0", name="ck_withholding_payment_amount"),
    )


class PayrollSalaryActualDeductionAllocation(Base):
    """Actual employer-retained salary deduction used in one salary settlement."""

    __tablename__ = "payroll_salary_actual_deduction_allocations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payroll_line_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payment_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expense_role: Mapped[str] = mapped_column(String(50), nullable=False)
    reversed: Mapped[bool] = mapped_column(default=False)
    reversed_by_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "payroll_line_id"],
            ["payroll_lines.org_id", "payroll_lines.id"],
            name="fk_salary_actual_deduction_org_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_salary_actual_deduction_org_payment_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "reversed_by_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_salary_actual_deduction_org_reversal_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "org_id",
            "payroll_line_id",
            "payment_event_id",
            name="uq_salary_actual_deduction_line_event",
        ),
        Index(
            "ix_salary_actual_deduction_org_line",
            "org_id",
            "payroll_line_id",
        ),
        Index(
            "ix_salary_actual_deduction_org_event",
            "org_id",
            "payment_event_id",
        ),
        CheckConstraint("amount_fen > 0", name="ck_salary_actual_deduction_positive"),
        CheckConstraint(
            "expense_role IN ('payroll_management_expense','payroll_sales_expense',"
            "'payroll_service_cost')",
            name="ck_salary_actual_deduction_expense_role",
        ),
        CheckConstraint(
            "(reversed IS FALSE AND reversed_by_event_id IS NULL) OR "
            "(reversed IS TRUE AND reversed_by_event_id IS NOT NULL)",
            name="ck_salary_actual_deduction_reversal",
        ),
    )


class PayrollOpeningState(Base):
    __tablename__ = "payroll_opening_states"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    tax_year: Mapped[int] = mapped_column(Integer)
    through_month: Mapped[int] = mapped_column(Integer)
    cumulative_income_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    cumulative_tax_exempt_income_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    cumulative_basic_deduction_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    cumulative_employee_social_insurance_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    cumulative_employee_housing_fund_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    cumulative_special_additional_deduction_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    cumulative_other_legal_deduction_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    cumulative_tax_relief_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    cumulative_tax_withheld_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_payroll_opening_state_org_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_payroll_opening_execution_attribution",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "employee_id", "tax_year", "through_month", "supersedes_id"],
            [
                "payroll_opening_states.org_id",
                "payroll_opening_states.employee_id",
                "payroll_opening_states.tax_year",
                "payroll_opening_states.through_month",
                "payroll_opening_states.id",
            ],
            name="fk_payroll_opening_state_supersedes",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_payroll_opening_state_org_id"),
        UniqueConstraint(
            "org_id",
            "employee_id",
            "tax_year",
            "through_month",
            "id",
            name="uq_payroll_opening_state_period_id",
        ),
        UniqueConstraint("supersedes_id", name="uq_payroll_opening_state_successor"),
        CheckConstraint("tax_year BETWEEN 1900 AND 9999", name="ck_payroll_opening_state_year"),
        CheckConstraint("through_month BETWEEN 1 AND 12", name="ck_payroll_opening_state_month"),
        CheckConstraint(
            "cumulative_income_fen >= 0 AND cumulative_tax_exempt_income_fen >= 0 AND "
            "cumulative_basic_deduction_fen >= 0 AND "
            "cumulative_employee_social_insurance_fen >= 0 AND "
            "cumulative_employee_housing_fund_fen >= 0 AND "
            "cumulative_special_additional_deduction_fen >= 0 AND "
            "cumulative_other_legal_deduction_fen >= 0 AND "
            "cumulative_tax_relief_fen >= 0 AND cumulative_tax_withheld_fen >= 0",
            name="ck_payroll_opening_state_nonnegative",
        ),
    )


class AnnualBonusUsage(Base):
    __tablename__ = "annual_bonus_usages"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    tax_year: Mapped[int] = mapped_column(Integer)
    payroll_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    payroll_line_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_annual_bonus_usage_org_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_annual_bonus_usage_org_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payroll_batch_id", "employee_id", "payroll_line_id"],
            [
                "payroll_lines.org_id",
                "payroll_lines.payroll_batch_id",
                "payroll_lines.employee_id",
                "payroll_lines.id",
            ],
            name="fk_annual_bonus_usage_org_line",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "employee_id", "tax_year", name="uq_annual_bonus_employee_year"),
        CheckConstraint("tax_year BETWEEN 1900 AND 9999", name="ck_annual_bonus_usage_year"),
    )


class PayrollTaxYearGuard(Base):
    """A persistent, lockable ordering domain for one employee's tax year."""

    __tablename__ = "payroll_tax_year_guards"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tax_year: Mapped[int] = mapped_column(Integer, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_payroll_tax_guard_org_employee",
            ondelete="RESTRICT",
        ),
        CheckConstraint("tax_year BETWEEN 1900 AND 9999", name="ck_payroll_tax_guard_year"),
    )


class PayrollTaxStateSlot(Base):
    """The single final-tax state slot for one employee and controlled tax month."""

    __tablename__ = "payroll_tax_state_slots"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    tax_year: Mapped[int] = mapped_column(Integer)
    tax_month: Mapped[int] = mapped_column(Integer)
    regular_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    final_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_payroll_tax_slot_org_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "regular_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_tax_slot_org_regular_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "final_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_tax_slot_org_final_batch",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "org_id",
            "employee_id",
            "tax_year",
            "tax_month",
            name="uq_payroll_tax_state_slot",
        ),
        CheckConstraint("tax_year BETWEEN 1900 AND 9999", name="ck_payroll_tax_slot_year"),
        CheckConstraint("tax_month BETWEEN 1 AND 12", name="ck_payroll_tax_slot_month"),
    )


class AccountingPeriodCalendar(Base):
    __tablename__ = "accounting_period_calendars"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    calendar_year: Mapped[int] = mapped_column(Integer)
    rule_version: Mapped[str] = mapped_column(String(80))
    rule_effective_from: Mapped[date] = mapped_column(Date)
    source_urls: Mapped[list[str]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_accounting_period_calendar_org_id"),
        UniqueConstraint("org_id", "calendar_year", name="uq_accounting_period_calendar_org_year"),
        CheckConstraint(
            "calendar_year BETWEEN 1 AND 9999", name="ck_accounting_period_calendar_year"
        ),
        CheckConstraint(
            "length(trim(rule_version)) > 0", name="ck_accounting_period_calendar_rule"
        ),
    )


class AccountingPeriodAction(Base):
    __tablename__ = "accounting_period_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="RESTRICT"), index=True
    )
    action_type: Mapped[str] = mapped_column(String(30))
    idempotency_key: Mapped[str | None] = mapped_column(String(200), nullable=True)
    request_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30))
    input_facts: Mapped[dict[str, Any]] = mapped_column(JSON)
    missing_information: Mapped[list[str]] = mapped_column(JSON, default=list)
    errors: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    confirmed_by: Mapped[str | None] = mapped_column(String(100), nullable=True)
    confirmation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_period_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_accounting_period_action_org_id"),
        UniqueConstraint(
            "org_id", "idempotency_key", name="uq_accounting_period_action_idempotency"
        ),
        CheckConstraint(
            "action_type IN ('period_generation','period_close')",
            name="ck_accounting_period_action_type",
        ),
        CheckConstraint(
            "status IN ('posted','needs_information','rejected')",
            name="ck_accounting_period_action_status",
        ),
        CheckConstraint(
            "request_payload_hash IS NULL OR length(request_payload_hash) = 64",
            name="ck_accounting_period_action_hash_length",
        ),
    )


accounting_period_action_evidence = Table(
    "accounting_period_action_evidence",
    Base.metadata,
    Column("org_id", Uuid, primary_key=True),
    Column("action_id", Uuid, primary_key=True),
    Column("evidence_id", Uuid, primary_key=True),
    Column("created_at", DateTime(timezone=True), nullable=False, default=utcnow),
    ForeignKeyConstraint(
        ["org_id", "action_id"],
        ["accounting_period_actions.org_id", "accounting_period_actions.id"],
        name="fk_period_action_evidence_org_action",
        ondelete="RESTRICT",
    ),
    ForeignKeyConstraint(
        ["org_id", "evidence_id"],
        ["evidence.org_id", "evidence.id"],
        name="fk_period_action_evidence_org_evidence",
        ondelete="RESTRICT",
    ),
)


class AccountingPeriod(Base):
    __tablename__ = "accounting_periods"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    calendar_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    generation_action_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    calendar_year: Mapped[int] = mapped_column(Integer)
    calendar_month: Mapped[int] = mapped_column(Integer)
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20), default="open")
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    close_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, unique=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "calendar_id"],
            ["accounting_period_calendars.org_id", "accounting_period_calendars.id"],
            name="fk_accounting_period_org_calendar",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "generation_action_id"],
            ["accounting_period_actions.org_id", "accounting_period_actions.id"],
            name="fk_accounting_period_org_generation_action",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "close_id"],
            ["accounting_period_closes.org_id", "accounting_period_closes.id"],
            name="fk_accounting_period_org_close",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        UniqueConstraint("org_id", "id", name="uq_accounting_period_org_id"),
        UniqueConstraint(
            "org_id", "calendar_year", "calendar_month", name="uq_accounting_period_org_month"
        ),
        UniqueConstraint("org_id", "start_date", "end_date", name="uq_period_range"),
        CheckConstraint("calendar_year BETWEEN 1 AND 9999", name="ck_period_year"),
        CheckConstraint("calendar_month BETWEEN 1 AND 12", name="ck_period_month"),
        CheckConstraint("start_date <= end_date", name="ck_period_dates"),
        CheckConstraint(
            "calendar_year = CAST(substr(CAST(start_date AS VARCHAR), 1, 4) AS INTEGER) "
            "AND calendar_month = CAST(substr(CAST(start_date AS VARCHAR), 6, 2) AS INTEGER) "
            "AND substr(CAST(start_date AS VARCHAR), 9, 2) = '01' "
            "AND calendar_year = CAST(substr(CAST(end_date AS VARCHAR), 1, 4) AS INTEGER) "
            "AND calendar_month = CAST(substr(CAST(end_date AS VARCHAR), 6, 2) AS INTEGER) "
            "AND CAST(substr(CAST(end_date AS VARCHAR), 9, 2) AS INTEGER) BETWEEN 28 AND 31",
            name="ck_period_natural_month",
        ),
        CheckConstraint("status IN ('open','closed')", name="ck_period_status"),
        CheckConstraint(
            "(status = 'open' AND closed_at IS NULL AND close_id IS NULL) OR "
            "(status = 'closed' AND closed_at IS NOT NULL AND close_id IS NOT NULL)",
            name="ck_period_close_state",
        ),
    )


class AccountingPeriodClose(Base):
    __tablename__ = "accounting_period_closes"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    period_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    action_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    owner_approval_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, unique=True)
    calculation: Mapped[dict[str, Any]] = mapped_column(JSON)
    calculation_payload: Mapped[str] = mapped_column(Text)
    calculation_hash: Mapped[str] = mapped_column(String(64))
    rule_version: Mapped[str] = mapped_column(String(80))
    rule_effective_from: Mapped[date] = mapped_column(Date)
    source_urls: Mapped[list[str]] = mapped_column(JSON)
    previous_close_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    checker_version: Mapped[str] = mapped_column(String(80))
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    voucher_count: Mapped[int] = mapped_column(Integer)
    line_count: Mapped[int] = mapped_column(Integer)
    total_debit_fen: Mapped[int] = mapped_column(BigInteger)
    total_credit_fen: Mapped[int] = mapped_column(BigInteger)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_accounting_period_close_org_period",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["accounting_period_actions.org_id", "accounting_period_actions.id"],
            name="fk_accounting_period_close_org_action",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "owner_approval_id"],
            ["accounting_period_close_approvals.org_id", "accounting_period_close_approvals.id"],
            name="fk_accounting_period_close_org_owner_approval",
            ondelete="RESTRICT",
            use_alter=True,
        ),
        UniqueConstraint("org_id", "id", name="uq_accounting_period_close_org_id"),
        CheckConstraint("length(calculation_payload) > 0", name="ck_period_close_payload"),
        CheckConstraint("length(calculation_hash) = 64", name="ck_period_close_hash_length"),
        CheckConstraint(
            "previous_close_hash IS NULL OR length(previous_close_hash) = 64",
            name="ck_period_close_previous_hash_length",
        ),
        CheckConstraint("voucher_count >= 0 AND line_count >= 0", name="ck_period_close_counts"),
        CheckConstraint(
            "total_debit_fen >= 0 AND total_debit_fen = total_credit_fen",
            name="ck_period_close_totals",
        ),
    )


class AccountingPeriodCloseCommentary(Base):
    """Immutable AI interpretation bound to one deterministic close snapshot."""

    __tablename__ = "accounting_period_close_commentaries"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    close_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    commentary: Mapped[str] = mapped_column(Text, nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(80), nullable=False)
    context_payload: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    context_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    generation_method: Mapped[str] = mapped_column(String(40), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "close_id"],
            ["accounting_period_closes.org_id", "accounting_period_closes.id"],
            name="fk_period_close_commentary_org_close",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_period_close_commentary_org_id"),
        CheckConstraint(
            "length(trim(commentary)) BETWEEN 1 AND 2000",
            name="ck_period_close_commentary_text",
        ),
        CheckConstraint(
            "length(prompt_version) BETWEEN 1 AND 80",
            name="ck_period_close_commentary_prompt_version",
        ),
        CheckConstraint(
            "length(context_hash) = 64",
            name="ck_period_close_commentary_context_hash_length",
        ),
        CheckConstraint(
            "context_hash ~ '^[0-9a-f]{64}$'",
            name="ck_period_close_commentary_context_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "generation_method IN ('close_ai_agent','historical_ai_backfill')",
            name="ck_period_close_commentary_generation_method",
        ),
    )


class AccountingPeriodCloseApproval(Base):
    """Password-reauthenticated owner approval bound to one close preview hash."""

    __tablename__ = "accounting_period_close_approvals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    catalog_instance_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, nullable=False, default=lambda: LEGACY_CATALOG_INSTANCE_ID
    )
    period_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    owner_account_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_session_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    owner_credential_version: Mapped[int] = mapped_column(Integer, nullable=False)
    calculation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_method: Mapped[str] = mapped_column(String(40), nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_period_close_approval_org_period",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "owner_account_id"],
            ["owner_accounts.org_id", "owner_accounts.id"],
            name="fk_period_close_approval_org_owner",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "owner_account_id", "owner_session_id", "owner_credential_version"],
            [
                "owner_sessions.org_id",
                "owner_sessions.owner_account_id",
                "owner_sessions.id",
                "owner_sessions.credential_version",
            ],
            name="fk_period_close_approval_owner_session",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_period_close_approval_org_id"),
        CheckConstraint(
            "owner_credential_version >= 1",
            name="ck_period_close_approval_credential_version",
        ),
        CheckConstraint(
            "length(calculation_hash) = 64",
            name="ck_period_close_approval_hash_length",
        ),
        CheckConstraint(
            "confirmation_method = 'local_password_reauthentication'",
            name="ck_period_close_approval_method",
        ),
        CheckConstraint(
            "expires_at > confirmed_at",
            name="ck_period_close_approval_expiry",
        ),
        CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= confirmed_at",
            name="ck_period_close_approval_consumed_at",
        ),
    )


class AccountingPeriodCloseSource(Base):
    __tablename__ = "accounting_period_close_sources"

    close_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    voucher_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    voucher_number: Mapped[str] = mapped_column(String(50))
    posting_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(Text)
    event_type: Mapped[str] = mapped_column(String(60))
    event_status_at_close: Mapped[str] = mapped_column(String(30))
    request_payload_hash_at_close: Mapped[str | None] = mapped_column(String(64), nullable=True)
    debit_fen: Mapped[int] = mapped_column(BigInteger)
    credit_fen: Mapped[int] = mapped_column(BigInteger)
    line_snapshot: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "close_id"],
            ["accounting_period_closes.org_id", "accounting_period_closes.id"],
            name="fk_period_close_source_org_close",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "voucher_id"],
            ["vouchers.org_id", "vouchers.id"],
            name="fk_period_close_source_org_voucher",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_period_close_source_org_event",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "event_status_at_close IN ('posted','reversed')",
            name="ck_period_close_source_event_status",
        ),
        CheckConstraint(
            "debit_fen > 0 AND debit_fen = credit_fen",
            name="ck_period_close_source_balanced",
        ),
    )


class FixedAsset(Base):
    __tablename__ = "fixed_assets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    asset_code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(30))
    expected_use_over_one_year: Mapped[bool] = mapped_column()
    acquisition_date: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    purchase_price_fen: Mapped[int] = mapped_column(BigInteger)
    noncreditable_tax_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    transport_and_handling_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    installation_and_direct_cost_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_fen: Mapped[int] = mapped_column(BigInteger)
    supplier_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    reimbursing_employee_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    settlement_method: Mapped[str] = mapped_column(String(40))
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    acquisition_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "supplier_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_fixed_asset_org_supplier",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "reimbursing_employee_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_fixed_asset_org_reimbursing_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "acquisition_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_org_acquisition_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_fixed_asset_org_id"),
        UniqueConstraint("org_id", "asset_code", name="uq_fixed_asset_org_code"),
        CheckConstraint(
            "category IN ('production_equipment','tools_furniture','transport',"
            "'electronic','other_movable_tangible')",
            name="ck_fixed_asset_category",
        ),
        CheckConstraint("expected_use_over_one_year IS TRUE", name="ck_fixed_asset_expected_use"),
        CheckConstraint(
            "purchase_price_fen >= 0 AND noncreditable_tax_fen >= 0 "
            "AND transport_and_handling_fen >= 0 "
            "AND installation_and_direct_cost_fen >= 0",
            name="ck_fixed_asset_cost_components_nonnegative",
        ),
        CheckConstraint("cost_fen > 0", name="ck_fixed_asset_cost_positive"),
        CheckConstraint(
            "cost_fen = purchase_price_fen + noncreditable_tax_fen "
            "+ transport_and_handling_fen + installation_and_direct_cost_fen",
            name="ck_fixed_asset_cost_components_total",
        ),
        CheckConstraint(
            "settlement_method IN "
            "('bank','payable','employee_payable','allocated_employee_payables')",
            name="ck_fixed_asset_settlement_method",
        ),
        CheckConstraint(
            "(settlement_method = 'bank' AND payment_date IS NOT NULL AND due_date IS NULL "
            "AND reimbursing_employee_id IS NULL) OR "
            "(settlement_method = 'payable' AND payment_date IS NULL AND due_date IS NOT NULL "
            "AND reimbursing_employee_id IS NULL) OR "
            "(settlement_method = 'employee_payable' AND payment_date IS NULL "
            "AND due_date IS NOT NULL AND reimbursing_employee_id IS NOT NULL) OR "
            "(settlement_method = 'allocated_employee_payables' "
            "AND payment_date IS NULL AND due_date IS NULL "
            "AND reimbursing_employee_id IS NULL)",
            name="ck_fixed_asset_settlement_dates",
        ),
    )


class FixedAssetCostSource(Base):
    __tablename__ = "fixed_asset_cost_sources"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    open_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    source_key: Mapped[str] = mapped_column(String(200))
    employee_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    due_date: Mapped[date] = mapped_column(Date)
    description: Mapped[str] = mapped_column(String(500))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["fixed_assets.org_id", "fixed_assets.id"],
            name="fk_fixed_asset_cost_source_org_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_cost_source_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_fixed_asset_cost_source_org_employee",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_fixed_asset_cost_source_org_open_item",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "org_id", "event_id", "source_key", name="uq_fixed_asset_cost_source_event_key"
        ),
        UniqueConstraint("org_id", "id", name="uq_fixed_asset_cost_source_org_id"),
        CheckConstraint("amount_fen > 0", name="ck_fixed_asset_cost_source_amount"),
        CheckConstraint("length(trim(source_key)) > 0", name="ck_fixed_asset_cost_source_key"),
        CheckConstraint(
            "length(trim(description)) > 0", name="ck_fixed_asset_cost_source_description"
        ),
    )


class FixedAssetActivation(Base):
    __tablename__ = "fixed_asset_activations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    in_service_date: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    depreciation_method: Mapped[str] = mapped_column(String(30), default="straight_line")
    useful_life_months: Mapped[int] = mapped_column(Integer)
    residual_value_fen: Mapped[int] = mapped_column(BigInteger)
    benefit_area: Mapped[str] = mapped_column(String(30))
    depreciation_group_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    depreciation_rounding_policy: Mapped[str] = mapped_column(
        String(50), default="round_half_up_card_v1"
    )
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["fixed_assets.org_id", "fixed_assets.id"],
            name="fk_fixed_asset_activation_org_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_activation_org_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_fixed_asset_activation_org_id"),
        CheckConstraint("depreciation_method = 'straight_line'", name="ck_asset_activation_method"),
        CheckConstraint("useful_life_months >= 13", name="ck_asset_activation_life"),
        CheckConstraint("residual_value_fen >= 0", name="ck_asset_activation_residual"),
        CheckConstraint(
            "benefit_area IN ('management','sales','service_delivery')",
            name="ck_asset_activation_benefit_area",
        ),
        CheckConstraint(
            "depreciation_rounding_policy IN "
            "('floor_final_remainder_v1','round_half_up_card_v1',"
            "'round_half_up_group_v1')",
            name="ck_asset_activation_rounding_policy",
        ),
        CheckConstraint(
            "depreciation_group_code IS NULL OR length(trim(depreciation_group_code)) > 0",
            name="ck_asset_activation_group_code",
        ),
        Index(
            "ix_fixed_asset_activation_org_group",
            "org_id",
            "depreciation_group_code",
        ),
    )


class FixedAssetDepreciationBatch(Base):
    __tablename__ = "fixed_asset_depreciation_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    period_start: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    asset_count: Mapped[int] = mapped_column(Integer)
    total_amount_fen: Mapped[int] = mapped_column(BigInteger)
    calculation_hash: Mapped[str] = mapped_column(String(64))
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_depreciation_batch_org_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_fixed_asset_depreciation_batch_org_id"),
        CheckConstraint("asset_count > 0", name="ck_fixed_asset_depreciation_batch_count"),
        CheckConstraint("total_amount_fen > 0", name="ck_fixed_asset_depreciation_batch_amount"),
        CheckConstraint(
            "length(calculation_hash) = 64",
            name="ck_fixed_asset_depreciation_batch_hash_length",
        ),
        CheckConstraint(
            "period_start = date_trunc('month', period_start)::date",
            name="ck_fixed_asset_depreciation_batch_period_month_start",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "date_trunc('month', posting_date)::date = period_start",
            name="ck_fixed_asset_depreciation_batch_posting_month",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "strftime('%Y-%m', posting_date) = strftime('%Y-%m', period_start)",
            name="ck_fixed_asset_depreciation_batch_posting_month",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "calculation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_fixed_asset_depreciation_batch_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
    )


class FixedAssetDepreciation(Base):
    __tablename__ = "fixed_asset_depreciations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    activation_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    batch_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    period_start: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    sequence_no: Mapped[int] = mapped_column(Integer)
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    accumulated_after_fen: Mapped[int] = mapped_column(BigInteger)
    calculation_hash: Mapped[str] = mapped_column(String(64))
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["fixed_assets.org_id", "fixed_assets.id"],
            name="fk_fixed_asset_depreciation_org_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "activation_id"],
            ["fixed_asset_activations.org_id", "fixed_asset_activations.id"],
            name="fk_fixed_asset_depreciation_org_activation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_depreciation_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "batch_id"],
            ["fixed_asset_depreciation_batches.org_id", "fixed_asset_depreciation_batches.id"],
            name="fk_fixed_asset_depreciation_org_batch",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_fixed_asset_depreciation_org_id"),
        UniqueConstraint(
            "org_id", "event_id", "asset_id", name="uq_fixed_asset_depreciation_event_asset"
        ),
        CheckConstraint("sequence_no > 0", name="ck_fixed_asset_depreciation_sequence"),
        CheckConstraint("amount_fen > 0", name="ck_fixed_asset_depreciation_amount"),
        CheckConstraint(
            "accumulated_after_fen >= amount_fen", name="ck_fixed_asset_depreciation_accumulated"
        ),
        CheckConstraint(
            "length(calculation_hash) = 64", name="ck_fixed_asset_depreciation_hash_length"
        ),
        CheckConstraint(
            "calculation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_fixed_asset_depreciation_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "period_start = date_trunc('month', period_start)::date",
            name="ck_fixed_asset_depreciation_period_month_start",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "date_trunc('month', posting_date)::date = period_start",
            name="ck_fixed_asset_depreciation_posting_month",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "strftime('%Y-%m', posting_date) = strftime('%Y-%m', period_start)",
            name="ck_fixed_asset_depreciation_posting_month",
        ).ddl_if(dialect="sqlite"),
    )


class FixedAssetDisposal(Base):
    __tablename__ = "fixed_asset_disposals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    activation_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, unique=True)
    disposal_date: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    disposal_kind: Mapped[str] = mapped_column(String(20))
    settlement_method: Mapped[str] = mapped_column(String(20))
    customer_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    gross_proceeds_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    invoice_type: Mapped[str] = mapped_column(String(20), default="none")
    waive_threshold_exemption: Mapped[bool] = mapped_column(default=False)
    vat_tax_sales_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    vat_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    clearance_cost_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    accumulated_depreciation_fen: Mapped[int] = mapped_column(BigInteger)
    book_value_fen: Mapped[int] = mapped_column(BigInteger)
    gain_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    loss_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    tax_rule_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("tax_rules.id", ondelete="RESTRICT"), nullable=True
    )
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["fixed_assets.org_id", "fixed_assets.id"],
            name="fk_fixed_asset_disposal_org_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "activation_id"],
            ["fixed_asset_activations.org_id", "fixed_asset_activations.id"],
            name="fk_fixed_asset_disposal_org_activation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_fixed_asset_disposal_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "customer_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_fixed_asset_disposal_org_customer",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_fixed_asset_disposal_org_id"),
        CheckConstraint("disposal_kind IN ('sale','retirement')", name="ck_asset_disposal_kind"),
        CheckConstraint(
            "settlement_method IN ('bank','receivable','none')",
            name="ck_asset_disposal_settlement_method",
        ),
        CheckConstraint(
            "invoice_type IN ('ordinary','special','none')", name="ck_asset_disposal_invoice_type"
        ),
        CheckConstraint(
            "gross_proceeds_fen >= 0 AND vat_tax_sales_fen >= 0 AND vat_fen >= 0 "
            "AND clearance_cost_fen >= 0 AND accumulated_depreciation_fen >= 0 "
            "AND book_value_fen >= 0 AND gain_fen >= 0 AND loss_fen >= 0",
            name="ck_asset_disposal_amounts_nonnegative",
        ),
        CheckConstraint(
            "NOT (gain_fen > 0 AND loss_fen > 0)", name="ck_asset_disposal_gain_loss_exclusive"
        ),
        CheckConstraint(
            "(disposal_kind = 'sale' AND settlement_method IN ('bank','receivable') "
            "AND customer_id IS NOT NULL AND gross_proceeds_fen > 0 AND tax_rule_id IS NOT NULL) "
            "OR (disposal_kind = 'retirement' AND settlement_method = 'none' "
            "AND customer_id IS NULL AND gross_proceeds_fen = 0 AND vat_tax_sales_fen = 0 "
            "AND vat_fen = 0 AND tax_rule_id IS NULL AND invoice_type = 'none' "
            "AND waive_threshold_exemption IS FALSE)",
            name="ck_asset_disposal_business_shape",
        ),
    )


class IntangibleAsset(Base):
    __tablename__ = "intangible_assets"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    asset_code: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(200))
    category: Mapped[str] = mapped_column(String(50))
    rights_description: Mapped[str] = mapped_column(Text)
    other_right_type_description: Mapped[str | None] = mapped_column(Text, nullable=True)
    identifiability_basis: Mapped[str | None] = mapped_column(Text, nullable=True)
    supplier_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    acquisition_date: Mapped[date] = mapped_column(Date)
    available_for_use_date: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    purchase_price_fen: Mapped[int] = mapped_column(BigInteger)
    noncreditable_tax_fen: Mapped[int] = mapped_column(BigInteger)
    directly_attributable_cost_fen: Mapped[int] = mapped_column(BigInteger)
    cost_fen: Mapped[int] = mapped_column(BigInteger)
    settlement_method: Mapped[str] = mapped_column(String(20))
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    benefit_area: Mapped[str] = mapped_column(String(30))
    life_basis: Mapped[str] = mapped_column(String(30))
    useful_life_months: Mapped[int] = mapped_column(Integer)
    life_basis_explanation: Mapped[str] = mapped_column(Text)
    is_available_for_use: Mapped[bool] = mapped_column()
    claims_creditable_input_vat: Mapped[bool] = mapped_column()
    acquisition_event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "supplier_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_intangible_asset_org_supplier",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "acquisition_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_intangible_asset_org_acquisition_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_intangible_asset_org_id"),
        UniqueConstraint("org_id", "asset_code", name="uq_intangible_asset_org_code"),
        UniqueConstraint("acquisition_event_id", name="uq_intangible_asset_acquisition_event"),
        CheckConstraint(
            "category IN ('software','patent','trademark','copyright',"
            "'non_patented_technology','other_identifiable_non_land')",
            name="ck_intangible_asset_category",
        ),
        CheckConstraint(
            "length(trim(asset_code)) > 0 AND length(trim(name)) > 0",
            name="ck_intangible_asset_identity_text",
        ),
        CheckConstraint("length(trim(rights_description)) > 0", name="ck_intangible_asset_rights"),
        CheckConstraint(
            "(category = 'other_identifiable_non_land' "
            "AND length(trim(other_right_type_description)) > 0 "
            "AND length(trim(identifiability_basis)) > 0) OR "
            "(category <> 'other_identifiable_non_land' "
            "AND other_right_type_description IS NULL AND identifiability_basis IS NULL)",
            name="ck_intangible_asset_other_identifiable",
        ),
        CheckConstraint(
            "available_for_use_date >= acquisition_date",
            name="ck_intangible_asset_available_date",
        ),
        CheckConstraint(
            "date_trunc('month', acquisition_date)::date = "
            "date_trunc('month', available_for_use_date)::date AND "
            "date_trunc('month', acquisition_date)::date = "
            "date_trunc('month', posting_date)::date",
            name="ck_intangible_asset_acquisition_month",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "strftime('%Y-%m', acquisition_date) = strftime('%Y-%m', available_for_use_date) "
            "AND strftime('%Y-%m', acquisition_date) = strftime('%Y-%m', posting_date)",
            name="ck_intangible_asset_acquisition_month",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "purchase_price_fen >= 0 AND noncreditable_tax_fen >= 0 "
            "AND directly_attributable_cost_fen >= 0 "
            "AND purchase_price_fen <= 9223372036854775807 "
            "AND noncreditable_tax_fen <= 9223372036854775807 "
            "AND directly_attributable_cost_fen <= 9223372036854775807",
            name="ck_intangible_asset_cost_components_nonnegative",
        ),
        CheckConstraint(
            "cost_fen = purchase_price_fen + noncreditable_tax_fen "
            "+ directly_attributable_cost_fen AND cost_fen > 0 "
            "AND cost_fen <= 9223372036854775807",
            name="ck_intangible_asset_cost_total",
        ),
        CheckConstraint(
            "settlement_method IN ('bank','payable')",
            name="ck_intangible_asset_settlement_method",
        ),
        CheckConstraint(
            "(settlement_method = 'bank' AND payment_date IS NOT NULL AND due_date IS NULL) OR "
            "(settlement_method = 'payable' AND payment_date IS NULL AND due_date IS NOT NULL)",
            name="ck_intangible_asset_settlement_dates",
        ),
        CheckConstraint(
            "benefit_area IN ('management','sales','service_delivery')",
            name="ck_intangible_asset_benefit_area",
        ),
        CheckConstraint(
            "life_basis IN ('legal_or_contractual','reliably_estimated','not_reliably_estimated')",
            name="ck_intangible_asset_life_basis",
        ),
        CheckConstraint(
            "useful_life_months > 0 AND useful_life_months <= 119988 "
            "AND cost_fen >= useful_life_months",
            name="ck_intangible_asset_life_and_nonzero_amortization",
        ),
        CheckConstraint(
            "life_basis <> 'not_reliably_estimated' OR useful_life_months >= 120",
            name="ck_intangible_asset_unreliable_life_minimum",
        ),
        CheckConstraint(
            "length(trim(life_basis_explanation)) > 0",
            name="ck_intangible_asset_life_explanation",
        ),
        CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_intangible_asset_rule_text",
        ),
        CheckConstraint(
            "is_available_for_use IS TRUE",
            name="ck_intangible_asset_available_for_use",
        ),
        CheckConstraint(
            "claims_creditable_input_vat IS FALSE",
            name="ck_intangible_asset_no_creditable_vat",
        ),
    )


class IntangibleAssetAmortization(Base):
    __tablename__ = "intangible_asset_amortizations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    period_start: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    sequence_no: Mapped[int] = mapped_column(Integer)
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    accumulated_after_fen: Mapped[int] = mapped_column(BigInteger)
    calculation_hash: Mapped[str] = mapped_column(String(64))
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["intangible_assets.org_id", "intangible_assets.id"],
            name="fk_intangible_amortization_org_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_intangible_amortization_org_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_intangible_amortization_org_id"),
        UniqueConstraint("event_id", name="uq_intangible_amortization_event"),
        CheckConstraint("sequence_no > 0", name="ck_intangible_amortization_sequence"),
        CheckConstraint(
            "amount_fen > 0 AND amount_fen <= 9223372036854775807",
            name="ck_intangible_amortization_amount",
        ),
        CheckConstraint(
            "accumulated_after_fen >= amount_fen AND accumulated_after_fen <= 9223372036854775807",
            name="ck_intangible_amortization_accumulated",
        ),
        CheckConstraint(
            "length(calculation_hash) = 64",
            name="ck_intangible_amortization_hash_length",
        ),
        CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_intangible_amortization_rule_text",
        ),
        CheckConstraint(
            "calculation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_intangible_amortization_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "period_start = date_trunc('month', period_start)::date",
            name="ck_intangible_amortization_period_month_start",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "date_trunc('month', posting_date)::date = period_start",
            name="ck_intangible_amortization_posting_month",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "strftime('%d', period_start) = '01'",
            name="ck_intangible_amortization_period_month_start",
        ).ddl_if(dialect="sqlite"),
        CheckConstraint(
            "strftime('%Y-%m', posting_date) = strftime('%Y-%m', period_start)",
            name="ck_intangible_amortization_posting_month",
        ).ddl_if(dialect="sqlite"),
    )


class IntangibleAssetRetirement(Base):
    __tablename__ = "intangible_asset_retirements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    asset_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    retirement_date: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    gross_proceeds_fen: Mapped[int] = mapped_column(BigInteger)
    compensation_fen: Mapped[int] = mapped_column(BigInteger)
    taxes_and_fees_fen: Mapped[int] = mapped_column(BigInteger)
    residual_proceeds_fen: Mapped[int] = mapped_column(BigInteger)
    accumulated_amortization_fen: Mapped[int] = mapped_column(BigInteger)
    book_value_fen: Mapped[int] = mapped_column(BigInteger)
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "asset_id"],
            ["intangible_assets.org_id", "intangible_assets.id"],
            name="fk_intangible_retirement_org_asset",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_intangible_retirement_org_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_intangible_retirement_org_id"),
        UniqueConstraint("event_id", name="uq_intangible_retirement_event"),
        CheckConstraint(
            "gross_proceeds_fen = 0 AND compensation_fen = 0 "
            "AND taxes_and_fees_fen = 0 AND residual_proceeds_fen = 0",
            name="ck_intangible_retirement_zero_proceeds",
        ),
        CheckConstraint(
            "accumulated_amortization_fen >= 0 AND book_value_fen >= 0 "
            "AND accumulated_amortization_fen <= 9223372036854775807 "
            "AND book_value_fen <= 9223372036854775807",
            name="ck_intangible_retirement_amounts",
        ),
        CheckConstraint(
            "posting_date = retirement_date",
            name="ck_intangible_retirement_posting_date",
        ),
        CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_intangible_retirement_rule_text",
        ),
        CheckConstraint(
            "retirement_date = (date_trunc('month', retirement_date) "
            "+ interval '1 month - 1 day')::date",
            name="ck_intangible_retirement_month_end",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "retirement_date = date(retirement_date, 'start of month', '+1 month', '-1 day')",
            name="ck_intangible_retirement_month_end",
        ).ddl_if(dialect="sqlite"),
    )


class Borrowing(Base):
    __tablename__ = "borrowings"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    borrowing_code: Mapped[str] = mapped_column(String(100))
    contract_name: Mapped[str] = mapped_column(String(200))
    lender_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    lender_is_licensed_financial_institution: Mapped[bool] = mapped_column()
    currency: Mapped[str] = mapped_column(String(3))
    principal_fen: Mapped[int] = mapped_column(BigInteger)
    drawdown_date: Mapped[date] = mapped_column(Date)
    due_date: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    annual_rate_percent: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    day_count_basis: Mapped[str] = mapped_column(String(20))
    interest_due_dates: Mapped[list[str]] = mapped_column(JSON)
    capitalization_applicable: Mapped[bool] = mapped_column()
    purpose_description: Mapped[str] = mapped_column(Text)
    single_drawdown: Mapped[bool] = mapped_column()
    fixed_rate: Mapped[bool] = mapped_column()
    simple_interest: Mapped[bool] = mapped_column()
    bullet_principal_at_maturity: Mapped[bool] = mapped_column()
    allows_prepayment: Mapped[bool] = mapped_column()
    allows_extension: Mapped[bool] = mapped_column()
    has_penalty_interest: Mapped[bool] = mapped_column()
    has_financing_fees: Mapped[bool] = mapped_column()
    drawdown_event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "lender_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_borrowing_org_lender",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "drawdown_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_borrowing_org_drawdown_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_borrowing_org_id"),
        UniqueConstraint("org_id", "borrowing_code", name="uq_borrowing_org_code"),
        UniqueConstraint("drawdown_event_id", name="uq_borrowing_drawdown_event"),
        CheckConstraint(
            "length(trim(borrowing_code)) > 0 AND length(trim(contract_name)) > 0",
            name="ck_borrowing_identity_text",
        ),
        CheckConstraint(
            "lender_is_licensed_financial_institution IS TRUE",
            name="ck_borrowing_licensed_lender",
        ),
        CheckConstraint("currency = 'CNY'", name="ck_borrowing_currency"),
        CheckConstraint(
            "principal_fen > 0 AND principal_fen <= 9223372036854775807",
            name="ck_borrowing_principal",
        ),
        CheckConstraint(
            "drawdown_date < due_date AND posting_date = drawdown_date",
            name="ck_borrowing_dates",
        ),
        CheckConstraint(
            "annual_rate_percent > 0 AND annual_rate_percent <= 100 "
            "AND annual_rate_percent = round(annual_rate_percent, 6)",
            name="ck_borrowing_annual_rate",
        ),
        CheckConstraint(
            "day_count_basis IN ('actual_360','actual_365')",
            name="ck_borrowing_day_count_basis",
        ),
        CheckConstraint(
            "capitalization_applicable IS FALSE",
            name="ck_borrowing_no_capitalization",
        ),
        CheckConstraint("length(trim(purpose_description)) > 0", name="ck_borrowing_purpose"),
        CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_borrowing_rule_text",
        ),
        CheckConstraint(
            "single_drawdown IS TRUE AND fixed_rate IS TRUE AND simple_interest IS TRUE "
            "AND bullet_principal_at_maturity IS TRUE AND allows_prepayment IS FALSE "
            "AND allows_extension IS FALSE AND has_penalty_interest IS FALSE "
            "AND has_financing_fees IS FALSE",
            name="ck_borrowing_phase_one_terms",
        ),
    )


class BorrowingInterestAccrual(Base):
    __tablename__ = "borrowing_interest_accruals"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    borrowing_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    period_start: Mapped[date] = mapped_column(Date)
    period_end: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    sequence_no: Mapped[int] = mapped_column(Integer)
    principal_fen: Mapped[int] = mapped_column(BigInteger)
    annual_rate_percent: Mapped[Decimal] = mapped_column(Numeric(9, 6))
    day_count_basis: Mapped[str] = mapped_column(String(20))
    actual_days: Mapped[int] = mapped_column(Integer)
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    calculation_hash: Mapped[str] = mapped_column(String(64))
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "borrowing_id"],
            ["borrowings.org_id", "borrowings.id"],
            name="fk_borrowing_accrual_org_borrowing",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_borrowing_accrual_org_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_borrowing_accrual_org_id"),
        UniqueConstraint(
            "org_id",
            "borrowing_id",
            "id",
            name="uq_borrowing_accrual_org_borrowing_id",
        ),
        UniqueConstraint("event_id", name="uq_borrowing_accrual_event"),
        CheckConstraint("period_start < period_end", name="ck_borrowing_accrual_period"),
        CheckConstraint("posting_date = period_end", name="ck_borrowing_accrual_posting_date"),
        CheckConstraint("sequence_no > 0", name="ck_borrowing_accrual_sequence"),
        CheckConstraint(
            "principal_fen > 0 AND principal_fen <= 9223372036854775807",
            name="ck_borrowing_accrual_principal",
        ),
        CheckConstraint(
            "annual_rate_percent > 0 AND annual_rate_percent <= 100 "
            "AND annual_rate_percent = round(annual_rate_percent, 6)",
            name="ck_borrowing_accrual_annual_rate",
        ),
        CheckConstraint(
            "day_count_basis IN ('actual_360','actual_365')",
            name="ck_borrowing_accrual_day_count_basis",
        ),
        CheckConstraint("actual_days > 0", name="ck_borrowing_accrual_actual_days"),
        CheckConstraint(
            "amount_fen > 0 AND amount_fen <= 9223372036854775807",
            name="ck_borrowing_accrual_amount",
        ),
        CheckConstraint(
            "length(calculation_hash) = 64",
            name="ck_borrowing_accrual_hash_length",
        ),
        CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_borrowing_accrual_rule_text",
        ),
        CheckConstraint(
            "calculation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_borrowing_accrual_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
    )


class BorrowingPayment(Base):
    __tablename__ = "borrowing_payments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    borrowing_id: Mapped[uuid.UUID] = mapped_column(Uuid, index=True)
    accrual_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid)
    payment_kind: Mapped[str] = mapped_column(String(20))
    payment_date: Mapped[date] = mapped_column(Date)
    posting_date: Mapped[date] = mapped_column(Date)
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    accounting_rule_version: Mapped[str] = mapped_column(String(50))
    accounting_rule_source_url: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "borrowing_id"],
            ["borrowings.org_id", "borrowings.id"],
            name="fk_borrowing_payment_org_borrowing",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "borrowing_id", "accrual_id"],
            [
                "borrowing_interest_accruals.org_id",
                "borrowing_interest_accruals.borrowing_id",
                "borrowing_interest_accruals.id",
            ],
            name="fk_borrowing_payment_org_accrual",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_borrowing_payment_org_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_borrowing_payment_org_id"),
        UniqueConstraint("event_id", name="uq_borrowing_payment_event"),
        CheckConstraint(
            "payment_kind IN ('interest','principal')",
            name="ck_borrowing_payment_kind",
        ),
        CheckConstraint(
            "(payment_kind = 'interest' AND accrual_id IS NOT NULL) OR "
            "(payment_kind = 'principal' AND accrual_id IS NULL)",
            name="ck_borrowing_payment_accrual_shape",
        ),
        CheckConstraint("posting_date = payment_date", name="ck_borrowing_payment_posting_date"),
        CheckConstraint(
            "amount_fen > 0 AND amount_fen <= 9223372036854775807",
            name="ck_borrowing_payment_amount",
        ),
        CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_borrowing_payment_rule_text",
        ),
    )


class IntangibleBorrowingAccountMigrationAction(Base):
    """Ownership ledger for default-account creation."""

    __tablename__ = "intangible_borrowing_account_migration_actions"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action: Mapped[str] = mapped_column(String(20))
    original_system_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_intangible_borrowing_account_action_org_account",
            ondelete="CASCADE",
        ),
        CheckConstraint(
            "action IN ('created','bound')",
            name="ck_intangible_borrowing_account_action",
        ),
    )


class FixedAssetAccountMigrationAction(Base):
    """Migration-owned account adoption ledger used for a reversible downgrade."""

    __tablename__ = "fixed_asset_account_migration_actions"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action: Mapped[str] = mapped_column(String(20))
    original_system_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_fixed_asset_account_action_org_account",
            ondelete="CASCADE",
        ),
        CheckConstraint("action IN ('created','bound')", name="ck_fixed_asset_account_action"),
    )


class FixedAssetTaxRuleMigrationAction(Base):
    """Tracks ownership of the effective-dated fixed-asset tax rule."""

    __tablename__ = "fixed_asset_tax_rule_migration_actions"

    tax_rule_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tax_rules.id", ondelete="RESTRICT"), primary_key=True
    )
    action: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (CheckConstraint("action = 'created'", name="ck_fixed_asset_tax_rule_action"),)


class TaxDeterminismExtensionAction(Base):
    """Tracks whether the schema baseline owns each PostgreSQL extension it uses."""

    __tablename__ = "tax_determinism_extension_actions"

    extension_name: Mapped[str] = mapped_column(String(63), primary_key=True)
    action: Mapped[str] = mapped_column(String(20))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        CheckConstraint(
            "extension_name IN ('btree_gist','pgcrypto')",
            name="ck_tax_determinism_extension_name",
        ),
        CheckConstraint(
            "action IN ('created','reused')", name="ck_tax_determinism_extension_action"
        ),
    )


class TaxRule(Base):
    __tablename__ = "tax_rules"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(100))
    jurisdiction: Mapped[str] = mapped_column(String(100), default="CN")
    effective_from: Mapped[date] = mapped_column(Date)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    version: Mapped[str] = mapped_column(String(50))
    source_url: Mapped[str] = mapped_column(Text)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON)

    __table_args__ = (
        UniqueConstraint("code", "jurisdiction", "version", name="uq_tax_rule_version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to", name="ck_tax_rule_dates"
        ),
    )


class Evidence(Base):
    __tablename__ = "evidence"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    sha256: Mapped[str] = mapped_column(String(64))
    original_name: Mapped[str] = mapped_column(String(255))
    media_type: Mapped[str] = mapped_column(String(100), default="application/octet-stream")
    source: Mapped[str] = mapped_column(String(50))
    size_bytes: Mapped[int] = mapped_column(BigInteger)
    storage_path: Mapped[str] = mapped_column(Text)
    metadata_json: Mapped[dict[str, Any]] = mapped_column("metadata", JSON, default=dict)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_evidence_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "sha256", name="uq_evidence_org_sha"),
        UniqueConstraint("org_id", "id", name="uq_evidence_org_id"),
        CheckConstraint("length(sha256) = 64", name="ck_evidence_sha256_length"),
        CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name="ck_evidence_sha256_lower_hex").ddl_if(
            dialect="postgresql"
        ),
        CheckConstraint("size_bytes >= 0", name="ck_evidence_size"),
    )


class OrganizationProfileVersionEvidence(Base):
    __tablename__ = "organization_profile_version_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    profile_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "profile_version_id"],
            ["organization_profile_versions.org_id", "organization_profile_versions.id"],
            name="fk_org_profile_evidence_version",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_org_profile_evidence_evidence",
            ondelete="RESTRICT",
        ),
    )


class BusinessEvent(Base):
    __tablename__ = "business_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_payload_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    event_type: Mapped[str] = mapped_column(String(60), index=True)
    status: Mapped[str] = mapped_column(String(30))
    description: Mapped[str] = mapped_column(Text, default="")
    facts: Mapped[dict[str, Any]] = mapped_column(JSON)
    business_date: Mapped[date] = mapped_column(Date)
    fulfillment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payment_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    tax_obligation_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    posting_date: Mapped[date] = mapped_column(Date, index=True)
    rule_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    rule_version: Mapped[str | None] = mapped_column(String(50), nullable=True)
    reversed_by_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=True
    )
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    evidence: Mapped[list[Evidence]] = relationship(
        secondary=event_evidence,
        # The legacy single-column foreign keys remain for upgrade
        # compatibility, while the R4 composite keys enforce organization
        # isolation.  State both joins explicitly so ORM relationship loading
        # follows the organization-bound edge rather than treating those two
        # compatible foreign-key paths as ambiguous.
        primaryjoin=lambda: and_(
            BusinessEvent.id == event_evidence.c.event_id,
            BusinessEvent.org_id == event_evidence.c.org_id,
        ),
        secondaryjoin=lambda: and_(
            Evidence.id == event_evidence.c.evidence_id,
            Evidence.org_id == event_evidence.c.org_id,
        ),
        lazy="selectin",
    )
    vouchers: Mapped[list[Voucher]] = relationship(
        "Voucher",
        back_populates="event",
        foreign_keys="Voucher.event_id",
        lazy="selectin",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_business_event_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_business_event_org_id"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_event_org_idempotency"),
        CheckConstraint(
            "status IN ('draft','posted','needs_information','rejected','reversed')",
            name="ck_event_status",
        ),
    )


class BusinessEventDependency(Base):
    """Immutable normalized dependency between supported customer events."""

    __tablename__ = "business_event_dependencies"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    parent_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    child_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    dependency_kind: Mapped[str] = mapped_column(String(30))
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "parent_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_business_event_dependency_org_parent",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "child_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_business_event_dependency_org_child",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_business_event_dependency_org_id"),
        CheckConstraint(
            "dependency_kind IN ('advance_fulfillment','advance_refund','sale_return')",
            name="ck_business_event_dependency_kind",
        ),
        CheckConstraint("parent_event_id <> child_event_id", name="ck_event_dependency_distinct"),
        CheckConstraint("amount_fen > 0", name="ck_event_dependency_amount"),
    )


class DeferredOutputVatTransfer(Base):
    """Immutable link from deferred VAT recognition to its automatic transfer."""

    __tablename__ = "deferred_output_vat_transfers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    source_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    source_open_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    transfer_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    tax_obligation_date: Mapped[date] = mapped_column(Date, nullable=False)
    accounting_rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    accounting_rule_source_url: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "source_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_deferred_vat_transfer_org_source_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_deferred_vat_transfer_org_open_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "transfer_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_deferred_vat_transfer_org_transfer_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "org_id",
            "source_event_id",
            "transfer_event_id",
            name="uq_deferred_vat_transfer_source_event",
        ),
        UniqueConstraint("org_id", "id", name="uq_deferred_vat_transfer_org_id"),
        CheckConstraint("amount_fen > 0", name="ck_deferred_vat_transfer_amount"),
        CheckConstraint(
            "length(trim(accounting_rule_version)) > 0 "
            "AND length(trim(accounting_rule_source_url)) > 0",
            name="ck_deferred_vat_transfer_rule_text",
        ),
    )


class AccountingPeriodDependencyMigrationAction(Base):
    """Marks normalized dependency rows proven by the schema baseline."""

    __tablename__ = "accounting_period_dependency_migration_actions"

    dependency_id: Mapped[uuid.UUID] = mapped_column(
        Uuid,
        ForeignKey("business_event_dependencies.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class PayrollEventLink(Base):
    """Normalized origin link from a payroll-related event to its source batch."""

    __tablename__ = "payroll_event_links"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payroll_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    source_payment_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    source_open_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    link_kind: Mapped[str] = mapped_column(String(40))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_payroll_event_link_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_event_link_org_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_payroll_event_link_org_source_payment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_payroll_event_link_org_source_open_item",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "link_kind IN ('payroll_accrual','salary_payment','contribution_supplement',"
            "'statutory_payment','reversal')",
            name="ck_payroll_event_link_kind",
        ),
    )


class PayrollBatchEvidence(Base):
    """Organization-bound evidence references retained for payroll drafts and finals."""

    __tablename__ = "payroll_batch_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    payroll_batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_payroll_batch_evidence_org_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_payroll_batch_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
    )


class PayrollAccountMigrationAction(Base):
    """Audit record that makes payroll default-account migration safely reversible."""

    __tablename__ = "payroll_account_migration_actions"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    account_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action: Mapped[str] = mapped_column(String(20))
    original_system_role: Mapped[str | None] = mapped_column(String(50), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_payroll_account_action_org_account",
            ondelete="RESTRICT",
        ),
        CheckConstraint("action IN ('created','bound')", name="ck_payroll_account_action"),
    )


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=True
    )
    direction: Mapped[str] = mapped_column(String(10))
    invoice_type: Mapped[str] = mapped_column(String(20))
    number: Mapped[str] = mapped_column(String(100))
    issue_date: Mapped[date] = mapped_column(Date)
    gross_amount_fen: Mapped[int] = mapped_column(BigInteger)
    tax_amount_fen: Mapped[int] = mapped_column(BigInteger)

    __table_args__ = (
        UniqueConstraint("org_id", "direction", "number", name="uq_invoice_number"),
        CheckConstraint("direction IN ('output','input')", name="ck_invoice_direction"),
        CheckConstraint("invoice_type IN ('ordinary','special','none')", name="ck_invoice_type"),
        CheckConstraint("gross_amount_fen > 0", name="ck_invoice_gross"),
        CheckConstraint("tax_amount_fen >= 0", name="ck_invoice_tax"),
    )


class TaxPeriod(Base):
    __tablename__ = "tax_periods"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    adjustment_posting_date: Mapped[date] = mapped_column(Date)
    rule_version: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(String(20), default="posted")
    calculation: Mapped[dict[str, Any]] = mapped_column(JSON)
    calculation_hash: Mapped[str] = mapped_column(String(64))
    calculation_hash_payload: Mapped[str] = mapped_column(Text)
    filing_cycle_snapshot: Mapped[str] = mapped_column(String(20))
    jurisdiction_snapshot: Mapped[str] = mapped_column(String(100))
    urban_maintenance_rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(6, 5))
    vat_rule_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tax_rules.id", ondelete="RESTRICT")
    )
    surtax_rule_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("tax_rules.id", ondelete="RESTRICT")
    )
    adjustment_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), unique=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    sources: Mapped[list[TaxPeriodSource]] = relationship(
        "TaxPeriodSource",
        back_populates="tax_period",
        cascade="save-update, merge",
        lazy="selectin",
    )

    __table_args__ = (
        UniqueConstraint("org_id", "id", name="uq_tax_period_org_id"),
        CheckConstraint("start_date <= end_date", name="ck_tax_period_dates"),
        CheckConstraint("status IN ('posted','reversed')", name="ck_tax_period_status"),
        CheckConstraint("length(calculation_hash) = 64", name="ck_tax_period_hash_length"),
        CheckConstraint(
            "filing_cycle_snapshot IN ('monthly','quarterly')",
            name="ck_tax_period_filing_cycle_snapshot",
        ),
        CheckConstraint(
            "urban_maintenance_rate_snapshot IN (0.07, 0.05, 0.01)",
            name="ck_tax_period_urban_rate_snapshot",
        ),
        CheckConstraint(
            "length(calculation_hash_payload) > 0",
            name="ck_tax_period_hash_payload_nonempty",
        ),
        CheckConstraint(
            "calculation_hash ~ '^[0-9a-f]{64}$'", name="ck_tax_period_hash_lower_hex"
        ).ddl_if(dialect="postgresql"),
    )


class LaborRemunerationTaxPolicyVersion(Base):
    """Effective-dated official withholding policy for personal labor income."""

    __tablename__ = "labor_remuneration_tax_policy_versions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    code: Mapped[str] = mapped_column(String(80), nullable=False)
    version: Mapped[str] = mapped_column(String(50), nullable=False)
    effective_from: Mapped[date] = mapped_column(Date, nullable=False)
    effective_to: Mapped[date | None] = mapped_column(Date, nullable=True)
    primary_source_url: Mapped[str] = mapped_column(Text, nullable=False)
    invoice_withholding_source_url: Mapped[str] = mapped_column(Text, nullable=False)
    legal_filing_source_url: Mapped[str] = mapped_column(Text, nullable=False)
    parameters: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        UniqueConstraint("code", "version", name="uq_labor_tax_policy_code_version"),
        CheckConstraint(
            "effective_to IS NULL OR effective_from <= effective_to",
            name="ck_labor_tax_policy_dates",
        ),
    )


class LaborServicePerson(Base):
    """A non-employee natural person engaged for temporary personal services."""

    __tablename__ = "labor_service_persons"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    person_code: Mapped[str] = mapped_column(String(100), nullable=False)
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    relationship_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    relationship_end_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="active")
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "counterparty_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_labor_person_org_counterparty",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_labor_person_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_labor_person_org_id"),
        UniqueConstraint("org_id", "person_code", name="uq_labor_person_org_code"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_labor_person_idempotency"),
        UniqueConstraint("counterparty_id", name="uq_labor_person_counterparty"),
        CheckConstraint(
            "relationship_end_date IS NULL OR relationship_start_date <= relationship_end_date",
            name="ck_labor_person_dates",
        ),
        CheckConstraint(
            "status IN ('active','ended')",
            name="ck_labor_person_status",
        ),
        CheckConstraint(
            "(status = 'active' AND relationship_end_date IS NULL) OR "
            "(status = 'ended' AND relationship_end_date IS NOT NULL)",
            name="ck_labor_person_status_dates",
        ),
        CheckConstraint(
            "length(request_payload_hash) = 64",
            name="ck_labor_person_request_hash",
        ),
    )


class LaborServicePersonEvidence(Base):
    __tablename__ = "labor_service_person_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    labor_person_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "labor_person_id"],
            ["labor_service_persons.org_id", "labor_service_persons.id"],
            name="fk_labor_person_evidence_org_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_labor_person_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
    )


class LaborServicePersonEndAction(Base):
    """Append-only evidence-backed end of a non-employee service relationship."""

    __tablename__ = "labor_service_person_end_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    labor_person_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    relationship_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "labor_person_id"],
            ["labor_service_persons.org_id", "labor_service_persons.id"],
            name="fk_labor_person_end_action_org_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_labor_person_end_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_labor_person_end_action_org_id"),
        UniqueConstraint("labor_person_id", name="uq_labor_person_end_action_person"),
        UniqueConstraint(
            "org_id", "idempotency_key", name="uq_labor_person_end_action_idempotency"
        ),
        CheckConstraint(
            "length(request_payload_hash) = 64",
            name="ck_labor_person_end_action_request_hash",
        ),
    )


class LaborServicePersonEndActionEvidence(Base):
    __tablename__ = "labor_service_person_end_action_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["labor_service_person_end_actions.org_id", "labor_service_person_end_actions.id"],
            name="fk_labor_person_end_evidence_org_action",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_labor_person_end_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
    )


class LaborRemunerationBatch(Base):
    __tablename__ = "labor_remuneration_batches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    remuneration_period: Mapped[str] = mapped_column(String(7), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="calculated")
    calculation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    calculation_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    policy_version_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    planned_payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    business_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, unique=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["policy_version_id"],
            ["labor_remuneration_tax_policy_versions.id"],
            name="fk_labor_batch_tax_policy",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "business_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_batch_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_labor_batch_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_labor_batch_org_id"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_labor_batch_idempotency"),
        CheckConstraint(
            "status IN ('calculated','posted','reversed','superseded')",
            name="ck_labor_batch_status",
        ),
        CheckConstraint(
            "length(remuneration_period) = 7 AND substr(remuneration_period, 5, 1) = '-' "
            "AND substr(remuneration_period, 6, 2) BETWEEN '01' AND '12'",
            name="ck_labor_batch_period",
        ),
        CheckConstraint("length(calculation_hash) = 64", name="ck_labor_batch_hash"),
        CheckConstraint("length(request_payload_hash) = 64", name="ck_labor_batch_request_hash"),
    )


class LaborRemunerationLine(Base):
    __tablename__ = "labor_remuneration_lines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    labor_person_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    service_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    service_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    fixed_fee_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    commission_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    gross_remuneration_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    expense_role: Mapped[str] = mapped_column(String(50), nullable=False)
    tax_identity: Mapped[str] = mapped_column(String(20), nullable=False)
    income_grouping: Mapped[str] = mapped_column(String(30), nullable=False)
    is_full_time_student: Mapped[bool] = mapped_column(nullable=False)
    expense_deduction_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    taxable_income_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    withholding_rate: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    quick_deduction_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    withholding_tax_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    net_payment_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    external_declaration_status: Mapped[str] = mapped_column(String(30), nullable=False)
    external_declaration_reference: Mapped[str | None] = mapped_column(String(200), nullable=True)
    calculation_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "batch_id"],
            ["labor_remuneration_batches.org_id", "labor_remuneration_batches.id"],
            name="fk_labor_line_org_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "labor_person_id"],
            ["labor_service_persons.org_id", "labor_service_persons.id"],
            name="fk_labor_line_org_person",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "counterparty_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_labor_line_org_counterparty",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_labor_line_org_id"),
        UniqueConstraint("batch_id", "labor_person_id", name="uq_labor_line_batch_person"),
        CheckConstraint("service_start_date <= service_end_date", name="ck_labor_line_dates"),
        CheckConstraint(
            "fixed_fee_fen >= 0 AND commission_fen >= 0 AND gross_remuneration_fen > 0 "
            "AND gross_remuneration_fen = fixed_fee_fen + commission_fen",
            name="ck_labor_line_gross",
        ),
        CheckConstraint(
            "expense_role IN ('labor_management_expense','labor_sales_expense',"
            "'labor_service_cost')",
            name="ck_labor_line_expense_role",
        ),
        CheckConstraint("tax_identity = 'resident'", name="ck_labor_line_resident"),
        CheckConstraint(
            "income_grouping IN ('single_occurrence','continuous_monthly')",
            name="ck_labor_line_grouping",
        ),
        CheckConstraint("is_full_time_student IS FALSE", name="ck_labor_line_not_student"),
        CheckConstraint(
            "expense_deduction_fen >= 0 AND taxable_income_fen >= 0 "
            "AND withholding_tax_fen >= 0 AND net_payment_fen >= 0 "
            "AND expense_deduction_fen + taxable_income_fen = gross_remuneration_fen "
            "AND net_payment_fen = gross_remuneration_fen - withholding_tax_fen",
            name="ck_labor_line_calculation",
        ),
        CheckConstraint(
            "external_declaration_status IN ('not_due','pending','confirmed')",
            name="ck_labor_line_declaration_status",
        ),
        CheckConstraint(
            "(external_declaration_status = 'confirmed' "
            "AND external_declaration_reference IS NOT NULL) OR "
            "(external_declaration_status <> 'confirmed' "
            "AND external_declaration_reference IS NULL)",
            name="ck_labor_line_declaration_reference",
        ),
    )


class LaborRemunerationBatchEvidence(Base):
    __tablename__ = "labor_remuneration_batch_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "batch_id"],
            ["labor_remuneration_batches.org_id", "labor_remuneration_batches.id"],
            name="fk_labor_batch_evidence_org_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_labor_batch_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
    )


class LaborExternalDeclarationConfirmation(Base):
    """Append-only evidence that an external labor-IIT declaration was confirmed."""

    __tablename__ = "labor_external_declaration_confirmations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    labor_line_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    declaration_date: Mapped[date] = mapped_column(Date, nullable=False)
    external_declaration_reference: Mapped[str] = mapped_column(String(200), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "labor_line_id"],
            ["labor_remuneration_lines.org_id", "labor_remuneration_lines.id"],
            name="fk_labor_declaration_org_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_labor_declaration_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_labor_declaration_org_id"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_labor_declaration_idempotency"),
        CheckConstraint(
            "length(request_payload_hash) = 64", name="ck_labor_declaration_request_hash"
        ),
    )


class LaborExternalDeclarationEvidence(Base):
    __tablename__ = "labor_external_declaration_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    confirmation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "confirmation_id"],
            [
                "labor_external_declaration_confirmations.org_id",
                "labor_external_declaration_confirmations.id",
            ],
            name="fk_labor_declaration_evidence_org_confirmation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_labor_declaration_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
    )


class LaborWithholdingEntitlement(Base):
    __tablename__ = "labor_withholding_entitlements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    labor_line_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "labor_line_id"],
            ["labor_remuneration_lines.org_id", "labor_remuneration_lines.id"],
            name="fk_labor_withholding_org_line",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_labor_withholding_org_id"),
        CheckConstraint("amount_fen >= 0", name="ck_labor_withholding_amount"),
    )


class UnifiedPayoutRun(Base):
    """One bank-matched parent for employee salary and personal labor children."""

    __tablename__ = "unified_payout_runs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="calculated")
    calculation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_input: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    calculation_trace: Mapped[list[dict[str, Any]]] = mapped_column(JSON, default=list)
    bank_account_code: Mapped[str] = mapped_column(String(30), nullable=False)
    bank_transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    business_date: Mapped[date] = mapped_column(Date, nullable=False)
    payment_date: Mapped[date] = mapped_column(Date, nullable=False)
    posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    gross_total_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    withholding_total_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    salary_petty_cash_recovery_total_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    net_total_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    business_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True, unique=True)
    confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    confirmation_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "bank_transaction_id"],
            ["bank_transactions.org_id", "bank_transactions.id"],
            name="fk_payout_run_org_bank_transaction",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "business_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_payout_run_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_payout_run_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_payout_run_org_id"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_payout_run_idempotency"),
        Index(
            "uq_active_payout_run_bank_transaction",
            "org_id",
            "bank_transaction_id",
            unique=True,
            postgresql_where=text("status IN ('calculated','posted')"),
            sqlite_where=text("status IN ('calculated','posted')"),
        ),
        CheckConstraint(
            "status IN ('calculated','posted','reversed','superseded')",
            name="ck_payout_run_status",
        ),
        CheckConstraint(
            "gross_total_fen > 0 AND withholding_total_fen >= 0 AND net_total_fen > 0 "
            "AND salary_petty_cash_recovery_total_fen >= 0 "
            "AND salary_petty_cash_recovery_total_fen <= withholding_total_fen "
            "AND net_total_fen = gross_total_fen - withholding_total_fen "
            "+ salary_petty_cash_recovery_total_fen",
            name="ck_payout_run_totals",
        ),
        CheckConstraint("length(calculation_hash) = 64", name="ck_payout_run_hash"),
        CheckConstraint("length(request_payload_hash) = 64", name="ck_payout_run_request_hash"),
    )


class UnifiedPayoutRunBankTransaction(Base):
    """Normalized bank rows that jointly fund one unified payout run."""

    __tablename__ = "unified_payout_run_bank_transactions"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    payout_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    bank_transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "payout_run_id"],
            ["unified_payout_runs.org_id", "unified_payout_runs.id"],
            name="fk_payout_bank_org_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "bank_transaction_id"],
            ["bank_transactions.org_id", "bank_transactions.id"],
            name="fk_payout_bank_org_transaction",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "payout_run_id",
            "bank_transaction_id",
            name="uq_payout_bank_run_transaction",
        ),
    )


class UnifiedPayoutRunItem(Base):
    __tablename__ = "unified_payout_run_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    payout_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    item_kind: Mapped[str] = mapped_column(String(20), nullable=False)
    source_open_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payroll_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    labor_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    counterparty_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    settlement_mode: Mapped[str] = mapped_column(String(50), nullable=False)
    gross_amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    employee_social_insurance_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    employee_housing_fund_fen: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    individual_income_tax_fen: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    actual_salary_deduction_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    salary_petty_cash_recovery_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    theoretical_individual_income_tax_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    unwithheld_individual_income_tax_fen: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0
    )
    net_amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    withholding_components: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "payout_run_id"],
            ["unified_payout_runs.org_id", "unified_payout_runs.id"],
            name="fk_payout_item_org_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_payout_item_org_open_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payroll_line_id"],
            ["payroll_lines.org_id", "payroll_lines.id"],
            name="fk_payout_item_org_payroll_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "labor_line_id"],
            ["labor_remuneration_lines.org_id", "labor_remuneration_lines.id"],
            name="fk_payout_item_org_labor_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "counterparty_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_payout_item_org_counterparty",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_payout_item_org_id"),
        UniqueConstraint("payout_run_id", "source_open_item_id", name="uq_payout_item_run_source"),
        CheckConstraint(
            "(item_kind = 'salary' AND payroll_line_id IS NOT NULL AND labor_line_id IS NULL "
            "AND settlement_mode = 'not_applicable') OR "
            "(item_kind = 'labor' AND payroll_line_id IS NULL AND labor_line_id IS NOT NULL "
            "AND actual_salary_deduction_fen = 0 "
            "AND salary_petty_cash_recovery_fen = 0 "
            "AND settlement_mode IN "
            "('net_after_withholding','gross_paid_without_withholding'))",
            name="ck_payout_item_source_kind",
        ),
        CheckConstraint(
            "gross_amount_fen > 0 AND employee_social_insurance_fen >= 0 "
            "AND employee_housing_fund_fen >= 0 AND individual_income_tax_fen >= 0 "
            "AND actual_salary_deduction_fen >= 0 "
            "AND salary_petty_cash_recovery_fen >= 0 "
            "AND salary_petty_cash_recovery_fen <= employee_social_insurance_fen "
            "+ employee_housing_fund_fen + individual_income_tax_fen "
            "AND theoretical_individual_income_tax_fen >= individual_income_tax_fen "
            "AND unwithheld_individual_income_tax_fen = "
            "theoretical_individual_income_tax_fen - individual_income_tax_fen "
            "AND net_amount_fen = gross_amount_fen - employee_social_insurance_fen "
            "- employee_housing_fund_fen - individual_income_tax_fen "
            "- actual_salary_deduction_fen + salary_petty_cash_recovery_fen "
            "AND net_amount_fen >= 0",
            name="ck_payout_item_totals",
        ),
        CheckConstraint(
            "(item_kind = 'salary' AND unwithheld_individual_income_tax_fen = 0) OR "
            "(item_kind = 'labor' AND settlement_mode = 'net_after_withholding' "
            "AND individual_income_tax_fen = theoretical_individual_income_tax_fen "
            "AND unwithheld_individual_income_tax_fen = 0) OR "
            "(item_kind = 'labor' AND settlement_mode = 'gross_paid_without_withholding' "
            "AND individual_income_tax_fen = 0 "
            "AND unwithheld_individual_income_tax_fen = "
            "theoretical_individual_income_tax_fen)",
            name="ck_payout_item_settlement_mode",
        ),
        CheckConstraint(
            "salary_petty_cash_recovery_fen = 0 OR "
            "(item_kind = 'salary' AND actual_salary_deduction_fen = 0 "
            "AND salary_petty_cash_recovery_fen = employee_social_insurance_fen "
            "+ employee_housing_fund_fen + individual_income_tax_fen)",
            name="ck_payout_item_petty_recovery",
        ),
    )


class UnifiedPayoutRunEvidence(Base):
    __tablename__ = "unified_payout_run_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    payout_run_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "payout_run_id"],
            ["unified_payout_runs.org_id", "unified_payout_runs.id"],
            name="fk_payout_evidence_org_run",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_payout_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
    )


class LaborWithholdingOpenItemSource(Base):
    """Exact per-person entitlement behind one labor-IIT payable open item."""

    __tablename__ = "labor_withholding_open_item_sources"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    open_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    entitlement_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    labor_line_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payment_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_labor_tax_source_org_open_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "entitlement_id"],
            ["labor_withholding_entitlements.org_id", "labor_withholding_entitlements.id"],
            name="fk_labor_tax_source_org_entitlement",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "labor_line_id"],
            ["labor_remuneration_lines.org_id", "labor_remuneration_lines.id"],
            name="fk_labor_tax_source_org_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_tax_source_org_payment",
            ondelete="RESTRICT",
        ),
        CheckConstraint("amount_fen > 0", name="ck_labor_tax_source_amount"),
    )


class LaborWithholdingTaxPaymentAllocation(Base):
    __tablename__ = "labor_withholding_tax_payment_allocations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    entitlement_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    open_item_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    payment_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    reversed: Mapped[bool] = mapped_column(default=False)
    reversed_by_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "entitlement_id"],
            ["labor_withholding_entitlements.org_id", "labor_withholding_entitlements.id"],
            name="fk_labor_tax_allocation_org_entitlement",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_labor_tax_allocation_org_open_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_tax_allocation_org_payment",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "reversed_by_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_tax_allocation_org_reversal",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "entitlement_id",
            "payment_event_id",
            name="uq_labor_tax_entitlement_event",
        ),
        CheckConstraint("amount_fen > 0", name="ck_labor_tax_allocation_amount"),
        CheckConstraint(
            "(reversed IS FALSE AND reversed_by_event_id IS NULL) OR "
            "(reversed IS TRUE AND reversed_by_event_id IS NOT NULL)",
            name="ck_labor_tax_allocation_reversal",
        ),
    )


class LaborRemunerationEventLink(Base):
    __tablename__ = "labor_remuneration_event_links"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    batch_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    labor_line_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    source_open_item_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    source_payment_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    link_kind: Mapped[str] = mapped_column(String(30), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_event_link_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "batch_id"],
            ["labor_remuneration_batches.org_id", "labor_remuneration_batches.id"],
            name="fk_labor_event_link_org_batch",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "labor_line_id"],
            ["labor_remuneration_lines.org_id", "labor_remuneration_lines.id"],
            name="fk_labor_event_link_org_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_labor_event_link_org_open_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_labor_event_link_org_source_payment",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "event_id", "batch_id", "labor_line_id", "link_kind", name="uq_labor_event_link"
        ),
        CheckConstraint(
            "link_kind IN ('accrual','payment','tax_payment','reversal')",
            name="ck_labor_event_link_kind",
        ),
    )


class FinancialStatementClassification(Base):
    """Append-only reporting allocation for one ambiguous expense voucher line."""

    __tablename__ = "financial_statement_classifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    voucher_line_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    parent_role: Mapped[str] = mapped_column(String(50), nullable=False)
    allocations: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    allocation_payload: Mapped[str] = mapped_column(Text, nullable=False)
    allocation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_note: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_references: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_financial_statement_classification_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["voucher_line_id"],
            ["voucher_lines.id"],
            name="fk_financial_statement_classification_voucher_line",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "supersedes_id"],
            [
                "financial_statement_classifications.org_id",
                "financial_statement_classifications.id",
            ],
            name="fk_financial_statement_classification_supersedes",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_financial_statement_classification_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_financial_statement_classification_org_id"),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_financial_statement_classification_idempotency",
        ),
        UniqueConstraint(
            "org_id",
            "supersedes_id",
            name="uq_financial_statement_classification_supersedes",
        ),
        CheckConstraint(
            "parent_role IN ('general_expense','sales_expense','finance_expense')",
            name="ck_financial_statement_classification_parent_role",
        ),
        CheckConstraint(
            "length(allocation_payload) > 0 AND length(allocation_hash) = 64",
            name="ck_financial_statement_classification_payload",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 AND length(request_payload_hash) = 64",
            name="ck_financial_statement_classification_request",
        ),
        CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_financial_statement_classification_note",
        ),
        CheckConstraint(
            "allocation_hash ~ '^[0-9a-f]{64}$' AND request_payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_financial_statement_classification_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
    )


class FinancialStatementOpeningBalanceConfirmation(Base):
    """Immutable zero-opening fact for a company's first, partial reporting year."""

    __tablename__ = "financial_statement_opening_balance_confirmations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    establishment_date: Mapped[date] = mapped_column(Date, nullable=False)
    treatment: Mapped[str] = mapped_column(String(40), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_note: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_references: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_fs_opening_confirmation_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_fs_opening_confirmation_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint(
            "org_id", "id", name="uq_fs_opening_confirmation_org_id"
        ),
        UniqueConstraint(
            "org_id",
            name="uq_fs_opening_confirmation_org",
        ),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_fs_opening_confirmation_idempotency",
        ),
        CheckConstraint(
            "treatment = 'zero_on_establishment'",
            name="ck_fs_opening_confirmation_treatment",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 "
            "AND length(request_payload_hash) = 64",
            name="ck_fs_opening_confirmation_request",
        ),
        CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_fs_opening_confirmation_note",
        ),
        CheckConstraint(
            "request_payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_fs_opening_confirmation_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
    )


class EnterpriseIncomeTaxQuarterConfirmation(Base):
    """Immutable accountant-approved enterprise-income-tax fact for one quarter."""

    __tablename__ = "enterprise_income_tax_quarter_confirmations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    calendar_year: Mapped[int] = mapped_column(Integer, nullable=False)
    calendar_quarter: Mapped[int] = mapped_column(Integer, nullable=False)
    treatment: Mapped[str] = mapped_column(String(30), nullable=False)
    amount_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    posting_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    business_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_payload: Mapped[str] = mapped_column(Text, nullable=False)
    calculation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    confirmation_note: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_references: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_enterprise_income_tax_confirmation_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "business_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_enterprise_income_tax_confirmation_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_enterprise_income_tax_confirmation_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_enterprise_income_tax_confirmation_org_id"),
        UniqueConstraint(
            "org_id",
            "calendar_year",
            "calendar_quarter",
            name="uq_enterprise_income_tax_confirmation_period",
        ),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_enterprise_income_tax_confirmation_idempotency",
        ),
        CheckConstraint(
            "calendar_year BETWEEN 1 AND 9999 AND calendar_quarter BETWEEN 1 AND 4",
            name="ck_enterprise_income_tax_confirmation_period",
        ),
        CheckConstraint(
            "treatment IN ('not_applicable','zero','accrue','reduce')",
            name="ck_enterprise_income_tax_confirmation_treatment",
        ),
        CheckConstraint(
            "(treatment IN ('not_applicable','zero') AND amount_fen = 0 "
            "AND posting_date IS NULL AND business_event_id IS NULL) OR "
            "(treatment IN ('accrue','reduce') AND amount_fen > 0 "
            "AND posting_date IS NOT NULL AND business_event_id IS NOT NULL)",
            name="ck_enterprise_income_tax_confirmation_shape",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 "
            "AND length(request_payload_hash) = 64 "
            "AND length(calculation_payload) > 0 AND length(calculation_hash) = 64",
            name="ck_enterprise_income_tax_confirmation_payload",
        ),
        CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_enterprise_income_tax_confirmation_note",
        ),
        CheckConstraint(
            "request_payload_hash ~ '^[0-9a-f]{64}$' AND calculation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_enterprise_income_tax_confirmation_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
    )


class ZeroTaxPeriodConfirmation(Base):
    """Immutable confirmation of a deterministic all-zero VAT/surtax period.

    A zero calculation has no accounting adjustment and therefore must not
    create a zero-value voucher.  This append-only control record preserves
    the calculation hash and authenticated execution attribution instead.
    """

    __tablename__ = "zero_tax_period_confirmations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    start_date: Mapped[date] = mapped_column(Date, nullable=False)
    end_date: Mapped[date] = mapped_column(Date, nullable=False)
    adjustment_posting_date: Mapped[date] = mapped_column(Date, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    rule_version: Mapped[str] = mapped_column(String(50), nullable=False)
    calculation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    calculation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_hash_payload: Mapped[str] = mapped_column(Text, nullable=False)
    filing_cycle_snapshot: Mapped[str] = mapped_column(String(20), nullable=False)
    jurisdiction_snapshot: Mapped[str] = mapped_column(String(100), nullable=False)
    urban_maintenance_rate_snapshot: Mapped[Decimal] = mapped_column(Numeric(6, 5), nullable=False)
    vat_rule_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    surtax_rule_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_zero_tax_confirmation_org",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["vat_rule_id"],
            ["tax_rules.id"],
            name="fk_zero_tax_confirmation_vat_rule",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["surtax_rule_id"],
            ["tax_rules.id"],
            name="fk_zero_tax_confirmation_surtax_rule",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_zero_tax_confirmation_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_zero_tax_confirmation_org_id"),
        UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_zero_tax_confirmation_idempotency",
        ),
        CheckConstraint("start_date <= end_date", name="ck_zero_tax_confirmation_dates"),
        CheckConstraint(
            "adjustment_posting_date >= end_date",
            name="ck_zero_tax_confirmation_posting_date",
        ),
        CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200",
            name="ck_zero_tax_confirmation_idempotency_length",
        ),
        CheckConstraint(
            "length(request_payload_hash) = 64",
            name="ck_zero_tax_confirmation_request_hash_length",
        ),
        CheckConstraint(
            "length(calculation_hash) = 64",
            name="ck_zero_tax_confirmation_hash_length",
        ),
        CheckConstraint(
            "length(calculation_hash_payload) > 0",
            name="ck_zero_tax_confirmation_hash_payload_nonempty",
        ),
        CheckConstraint(
            "filing_cycle_snapshot IN ('monthly','quarterly')",
            name="ck_zero_tax_confirmation_filing_cycle",
        ),
        CheckConstraint(
            "urban_maintenance_rate_snapshot IN (0.07, 0.05, 0.01)",
            name="ck_zero_tax_confirmation_urban_rate",
        ),
        CheckConstraint(
            "request_payload_hash ~ '^[0-9a-f]{64}$'",
            name="ck_zero_tax_confirmation_request_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
        CheckConstraint(
            "calculation_hash ~ '^[0-9a-f]{64}$'",
            name="ck_zero_tax_confirmation_hash_lower_hex",
        ).ddl_if(dialect="postgresql"),
    )


class TaxPeriodSource(Base):
    """Organization-bound immutable taxable-event snapshot for a tax period."""

    __tablename__ = "tax_period_sources"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    tax_period_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, index=True)
    source_event_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, index=True)
    gross_fen: Mapped[int] = mapped_column(BigInteger)
    net_fen: Mapped[int] = mapped_column(BigInteger)
    vat_fen: Mapped[int] = mapped_column(BigInteger)
    exemption_eligible: Mapped[bool] = mapped_column()

    tax_period: Mapped[TaxPeriod] = relationship(
        "TaxPeriod",
        back_populates="sources",
        foreign_keys=[org_id, tax_period_id],
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "tax_period_id"],
            ["tax_periods.org_id", "tax_periods.id"],
            name="fk_tax_period_source_org_period",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_tax_period_source_org_event",
            ondelete="RESTRICT",
        ),
    )


class BankStatementImportAction(Base):
    """Immutable audit root for one formal bank-statement confirmation."""

    __tablename__ = "bank_statement_import_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    bank_account_code: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    source_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    parser_request_fingerprint_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    calculation_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    calculation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    file_format: Mapped[str | None] = mapped_column(String(10), nullable=True)
    column_mapping: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    normalized_result: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    valid_row_count: Mapped[int] = mapped_column(Integer, nullable=False)
    imported_count: Mapped[int] = mapped_column(Integer, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, nullable=False)
    late_count: Mapped[int] = mapped_column(Integer, nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False)
    execution_attribution_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "bank_account_code"],
            ["accounts.org_id", "accounts.code"],
            name="fk_bank_import_action_org_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_bank_import_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_bank_import_action_org_id"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_bank_import_action_idempotency"),
        CheckConstraint(
            "status IN ('posted','partially_posted','rejected')",
            name="ck_bank_import_action_status",
        ),
        CheckConstraint(
            "row_count >= 0 AND valid_row_count >= 0 AND imported_count >= 0 "
            "AND duplicate_count >= 0 AND late_count >= 0 AND error_count >= 0 "
            "AND valid_row_count <= row_count "
            "AND imported_count + duplicate_count = valid_row_count "
            "AND late_count <= imported_count",
            name="ck_bank_import_action_counts",
        ),
        CheckConstraint(
            "(status = 'posted' AND error_count = 0 "
            "AND row_count = valid_row_count) OR "
            "(status = 'partially_posted' AND error_count > 0 "
            "AND row_count = valid_row_count + error_count) OR "
            "(status = 'rejected' AND error_count > 0 AND imported_count = 0 "
            "AND duplicate_count = 0 AND late_count = 0 AND valid_row_count = 0)",
            name="ck_bank_import_action_result_counts",
        ),
        CheckConstraint(
            "(status IN ('posted','partially_posted') "
            "AND calculation_payload IS NOT NULL AND calculation_hash IS NOT NULL "
            "AND source_sha256 IS NOT NULL "
            "AND parser_request_fingerprint_sha256 IS NOT NULL "
            "AND file_format IS NOT NULL AND column_mapping IS NOT NULL "
            "AND normalized_result IS NOT NULL) OR "
            "(status = 'rejected' AND calculation_payload IS NULL "
            "AND calculation_hash IS NULL AND file_format IS NULL "
            "AND column_mapping IS NULL AND normalized_result IS NULL)",
            name="ck_bank_import_action_payload_shape",
        ),
        CheckConstraint(
            "file_format IS NULL OR file_format = 'csv'",
            name="ck_bank_import_action_file_format",
        ),
        CheckConstraint(
            "length(request_payload_hash) = 64 "
            "AND (source_sha256 IS NULL OR length(source_sha256) = 64) "
            "AND (parser_request_fingerprint_sha256 IS NULL "
            "OR length(parser_request_fingerprint_sha256) = 64) "
            "AND ((source_sha256 IS NULL) = "
            "(parser_request_fingerprint_sha256 IS NULL)) "
            "AND (calculation_payload IS NULL OR length(calculation_payload) > 0) "
            "AND (calculation_hash IS NULL OR length(calculation_hash) = 64)",
            name="ck_bank_import_action_hash_lengths",
        ),
    )


class BankStatementImportFailure(Base):
    """Minimal, value-free row error retained only for a formal confirmation."""

    __tablename__ = "bank_statement_import_failures"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    error_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    field_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["bank_statement_import_actions.org_id", "bank_statement_import_actions.id"],
            name="fk_bank_import_failure_org_action",
            ondelete="RESTRICT",
        ),
        CheckConstraint("error_ordinal >= 1", name="ck_bank_import_failure_ordinal"),
        CheckConstraint(
            "row_number IS NULL OR row_number >= 2",
            name="ck_bank_import_failure_row_number",
        ),
        CheckConstraint(
            "length(code) BETWEEN 1 AND 100",
            name="ck_bank_import_failure_code",
        ),
    )


class BankStatementImportActionEvidence(Base):
    """Organization-bound evidence frozen for manual row identity resolution."""

    __tablename__ = "bank_statement_import_action_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_sha256_at_import: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["bank_statement_import_actions.org_id", "bank_statement_import_actions.id"],
            name="fk_bank_import_evidence_org_action",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_bank_import_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(evidence_sha256_at_import) = 64",
            name="ck_bank_import_evidence_hash_length",
        ),
    )


class BankTransaction(Base):
    __tablename__ = "bank_transactions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    bank_account_code: Mapped[str] = mapped_column(String(30), default="1002")
    fingerprint: Mapped[str] = mapped_column(String(64))
    external_id: Mapped[str | None] = mapped_column(String(100), nullable=True)
    booking_date: Mapped[date] = mapped_column(Date)
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    currency: Mapped[str] = mapped_column(String(3), default="CNY")
    counterparty_name: Mapped[str | None] = mapped_column(String(200), nullable=True)
    memo: Mapped[str] = mapped_column(Text, default="")
    source_sha256: Mapped[str] = mapped_column(String(64))
    matched_event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=True
    )
    import_action_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    import_row_number: Mapped[int | None] = mapped_column(Integer, nullable=True)
    row_identity_sha256: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_period_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    is_late: Mapped[bool] = mapped_column(nullable=False, default=False, server_default="0")
    original_close_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    original_close_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    original_closed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    execution_attribution_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    imported_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_bank_transaction_execution_attribution",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "matched_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_bank_transaction_org_matched_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "import_action_id"],
            ["bank_statement_import_actions.org_id", "bank_statement_import_actions.id"],
            name="fk_bank_transaction_org_import_action",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "original_period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_bank_transaction_org_original_period",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "original_close_id"],
            ["accounting_period_closes.org_id", "accounting_period_closes.id"],
            name="fk_bank_transaction_org_original_close",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_bank_transaction_org_id"),
        CheckConstraint("amount_fen <> 0", name="ck_bank_transaction_nonzero"),
        CheckConstraint("currency = 'CNY'", name="ck_bank_transaction_cny"),
        CheckConstraint(
            "(import_action_id IS NULL AND import_row_number IS NULL "
            "AND row_identity_sha256 IS NULL AND original_period_id IS NULL) OR "
            "(import_action_id IS NOT NULL AND import_row_number >= 2 "
            "AND row_identity_sha256 IS NOT NULL AND original_period_id IS NOT NULL)",
            name="ck_bank_transaction_import_origin",
        ),
        CheckConstraint(
            "row_identity_sha256 IS NULL OR length(row_identity_sha256) = 64",
            name="ck_bank_transaction_row_identity_hash",
        ),
        CheckConstraint(
            "(is_late IS FALSE AND original_close_id IS NULL "
            "AND original_close_hash IS NULL AND original_closed_at IS NULL) OR "
            "(is_late IS TRUE AND original_close_id IS NOT NULL "
            "AND original_close_hash IS NOT NULL AND original_closed_at IS NOT NULL)",
            name="ck_bank_transaction_late_origin",
        ),
        CheckConstraint(
            "original_close_hash IS NULL OR length(original_close_hash) = 64",
            name="ck_bank_transaction_original_close_hash",
        ),
    )


Index(
    "ix_bank_transaction_account_fingerprint",
    BankTransaction.org_id,
    BankTransaction.bank_account_code,
    BankTransaction.fingerprint,
)
Index(
    "uq_bank_transaction_account_external_id",
    BankTransaction.org_id,
    BankTransaction.bank_account_code,
    BankTransaction.external_id,
    unique=True,
    postgresql_where=BankTransaction.external_id.is_not(None),
    sqlite_where=BankTransaction.external_id.is_not(None),
)
Index(
    "uq_bank_transaction_account_source_row",
    BankTransaction.org_id,
    BankTransaction.bank_account_code,
    BankTransaction.row_identity_sha256,
    unique=True,
    postgresql_where=BankTransaction.row_identity_sha256.is_not(None),
    sqlite_where=BankTransaction.row_identity_sha256.is_not(None),
)
Index(
    "ix_bank_transaction_original_period_pending_late",
    BankTransaction.org_id,
    BankTransaction.original_period_id,
    BankTransaction.id,
    postgresql_where=BankTransaction.is_late.is_(True),
    sqlite_where=BankTransaction.is_late.is_(True),
)


class LateBankEvidenceAction(Base):
    """Append-only handling history for a late external bank fact."""

    __tablename__ = "late_bank_evidence_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    bank_transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    action_type: Mapped[str | None] = mapped_column(String(30), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_payload: Mapped[str | None] = mapped_column(Text, nullable=True)
    calculation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    handling_period_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    original_close_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    original_close_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    target_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    result_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    result_voucher_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    workflow_name: Mapped[str | None] = mapped_column(String(100), nullable=True)
    explanation: Mapped[str | None] = mapped_column(Text, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    error_field_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    execution_attribution_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "bank_transaction_id"],
            ["bank_transactions.org_id", "bank_transactions.id"],
            name="fk_late_bank_action_org_transaction",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "handling_period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_late_bank_action_org_handling_period",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "original_close_id"],
            ["accounting_period_closes.org_id", "accounting_period_closes.id"],
            name="fk_late_bank_action_org_original_close",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "target_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_late_bank_action_org_target_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "result_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_late_bank_action_org_result_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "result_voucher_id"],
            ["vouchers.org_id", "vouchers.id"],
            name="fk_late_bank_action_org_result_voucher",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_late_bank_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_late_bank_action_org_id"),
        UniqueConstraint("org_id", "idempotency_key", name="uq_late_bank_action_idempotency"),
        CheckConstraint(
            "status IN ('posted','rejected')",
            name="ck_late_bank_action_status",
        ),
        CheckConstraint(
            "action_type IS NULL OR action_type IN ('evidence_only','omitted_entry')",
            name="ck_late_bank_action_type",
        ),
        CheckConstraint(
            "length(request_payload_hash) = 64 AND "
            "(calculation_payload IS NULL OR length(calculation_payload) > 0) AND "
            "(calculation_hash IS NULL OR length(calculation_hash) = 64) AND "
            "(original_close_hash IS NULL OR length(original_close_hash) = 64)",
            name="ck_late_bank_action_hash_lengths",
        ),
        CheckConstraint(
            "(status = 'posted' AND action_type IS NOT NULL "
            "AND calculation_payload IS NOT NULL AND calculation_hash IS NOT NULL "
            "AND handling_period_id IS NOT NULL "
            "AND original_close_id IS NOT NULL AND original_close_hash IS NOT NULL "
            "AND explanation IS NOT NULL AND length(trim(explanation)) BETWEEN 1 AND 2000 "
            "AND error_code IS NULL AND error_field_path IS NULL AND error_count = 0) OR "
            "(status = 'rejected' AND action_type IS NULL "
            "AND calculation_payload IS NULL AND calculation_hash IS NULL "
            "AND handling_period_id IS NULL "
            "AND original_close_id IS NULL AND original_close_hash IS NULL "
            "AND target_event_id IS NULL AND result_event_id IS NULL "
            "AND result_voucher_id IS NULL AND workflow_name IS NULL "
            "AND explanation IS NULL AND error_code IS NOT NULL AND error_count > 0)",
            name="ck_late_bank_action_payload_shape",
        ),
        CheckConstraint(
            "status <> 'posted' OR "
            "(action_type = 'evidence_only' AND target_event_id IS NOT NULL "
            "AND result_event_id IS NULL AND result_voucher_id IS NULL "
            "AND workflow_name IS NULL) OR "
            "(action_type = 'omitted_entry' AND target_event_id IS NULL "
            "AND result_event_id IS NOT NULL AND result_voucher_id IS NOT NULL "
            "AND workflow_name IS NOT NULL AND length(trim(workflow_name)) > 0)",
            name="ck_late_bank_action_result_shape",
        ),
    )


class LateBankEvidenceActionEvidence(Base):
    __tablename__ = "late_bank_evidence_action_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_sha256_at_action: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["late_bank_evidence_actions.org_id", "late_bank_evidence_actions.id"],
            name="fk_late_bank_evidence_org_action",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_late_bank_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(evidence_sha256_at_action) = 64",
            name="ck_late_bank_evidence_hash_length",
        ),
    )


Index(
    "ix_late_bank_action_pending_projection",
    LateBankEvidenceAction.org_id,
    LateBankEvidenceAction.handling_period_id,
    LateBankEvidenceAction.bank_transaction_id,
)


class BankReconciliationAction(Base):
    """Immutable formal confirmation attempt; rejected rows retain no balances."""

    __tablename__ = "bank_reconciliation_actions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    period_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    bank_account_code: Mapped[str] = mapped_column(String(30), nullable=False)
    idempotency_key: Mapped[str] = mapped_column(String(200), nullable=False)
    request_payload_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    error_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    execution_attribution_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_bank_reconciliation_action_org_period",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "bank_account_code"],
            ["accounts.org_id", "accounts.code"],
            name="fk_bank_reconciliation_action_org_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_bank_reconciliation_action_execution_attribution",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_bank_reconciliation_action_org_id"),
        UniqueConstraint(
            "org_id", "idempotency_key", name="uq_bank_reconciliation_action_idempotency"
        ),
        CheckConstraint(
            "status IN ('posted','rejected')",
            name="ck_bank_reconciliation_action_status",
        ),
        CheckConstraint(
            "(status = 'posted' AND calculation_hash IS NOT NULL AND error_count = 0) OR "
            "(status = 'rejected' AND calculation_hash IS NULL AND error_count > 0)",
            name="ck_bank_reconciliation_action_result",
        ),
        CheckConstraint(
            "length(request_payload_hash) = 64 AND "
            "(calculation_hash IS NULL OR length(calculation_hash) = 64)",
            name="ck_bank_reconciliation_action_hash_lengths",
        ),
    )


class BankReconciliationFailure(Base):
    __tablename__ = "bank_reconciliation_failures"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    action_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    error_ordinal: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(100), nullable=False)
    field_path: Mapped[str | None] = mapped_column(String(500), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["bank_reconciliation_actions.org_id", "bank_reconciliation_actions.id"],
            name="fk_bank_reconciliation_failure_org_action",
            ondelete="RESTRICT",
        ),
        CheckConstraint("error_ordinal >= 1", name="ck_bank_reconciliation_failure_ordinal"),
        CheckConstraint(
            "length(code) BETWEEN 1 AND 100",
            name="ck_bank_reconciliation_failure_code",
        ),
    )


class BankReconciliation(Base):
    """One immutable, explicit bank-account reconciliation snapshot."""

    __tablename__ = "bank_reconciliations"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    action_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    period_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False)
    bank_account_code: Mapped[str] = mapped_column(String(30), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    calculation: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)
    calculation_payload: Mapped[str] = mapped_column(Text, nullable=False)
    calculation_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    coverage_start_date: Mapped[date] = mapped_column(Date, nullable=False)
    coverage_end_date: Mapped[date] = mapped_column(Date, nullable=False)
    statement_opening_balance_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    statement_closing_balance_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    statement_movement_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    statement_integrity_difference_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    book_closing_balance_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    statement_to_book_difference_fen: Mapped[int] = mapped_column(BigInteger, nullable=False)
    statement_transaction_count: Mapped[int] = mapped_column(Integer, nullable=False)
    unmatched_transaction_count: Mapped[int] = mapped_column(Integer, nullable=False)
    pending_late_transaction_count: Mapped[int] = mapped_column(Integer, nullable=False)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSON, nullable=False)
    confirmed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "action_id"],
            ["bank_reconciliation_actions.org_id", "bank_reconciliation_actions.id"],
            name="fk_bank_reconciliation_org_action",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_bank_reconciliation_org_period",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "bank_account_code"],
            ["accounts.org_id", "accounts.code"],
            name="fk_bank_reconciliation_org_account",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_bank_reconciliation_org_id"),
        UniqueConstraint(
            "org_id",
            "period_id",
            "bank_account_code",
            "version",
            name="uq_bank_reconciliation_period_account_version",
        ),
        CheckConstraint("version >= 1", name="ck_bank_reconciliation_version"),
        CheckConstraint(
            "coverage_start_date <= coverage_end_date",
            name="ck_bank_reconciliation_coverage",
        ),
        CheckConstraint(
            "statement_integrity_difference_fen = 0",
            name="ck_bank_reconciliation_statement_integrity",
        ),
        CheckConstraint(
            "statement_transaction_count >= 0 AND unmatched_transaction_count >= 0 "
            "AND pending_late_transaction_count >= 0",
            name="ck_bank_reconciliation_counts",
        ),
        CheckConstraint(
            "length(calculation_payload) > 0 AND length(calculation_hash) = 64",
            name="ck_bank_reconciliation_hash",
        ),
    )


Index(
    "ix_bank_reconciliation_period_account",
    BankReconciliation.org_id,
    BankReconciliation.period_id,
    BankReconciliation.bank_account_code,
    BankReconciliation.version,
)


class BankReconciliationEvidence(Base):
    __tablename__ = "bank_reconciliation_evidence"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    evidence_sha256_at_confirm: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "reconciliation_id"],
            ["bank_reconciliations.org_id", "bank_reconciliations.id"],
            name="fk_bank_reconciliation_evidence_org_reconciliation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_bank_reconciliation_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(evidence_sha256_at_confirm) = 64",
            name="ck_bank_reconciliation_evidence_hash",
        ),
    )


class BankReconciliationImportAction(Base):
    __tablename__ = "bank_reconciliation_import_actions"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    import_action_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    request_payload_hash_at_confirm: Mapped[str] = mapped_column(String(64), nullable=False)
    calculation_hash_at_confirm: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "reconciliation_id"],
            ["bank_reconciliations.org_id", "bank_reconciliations.id"],
            name="fk_bank_reconciliation_import_org_reconciliation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "import_action_id"],
            ["bank_statement_import_actions.org_id", "bank_statement_import_actions.id"],
            name="fk_bank_reconciliation_import_org_action",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(request_payload_hash_at_confirm) = 64 "
            "AND length(calculation_hash_at_confirm) = 64",
            name="ck_bank_reconciliation_import_hashes",
        ),
    )


class BankReconciliationTransaction(Base):
    __tablename__ = "bank_reconciliation_transactions"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    bank_transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    booking_date_at_confirm: Mapped[date] = mapped_column(Date, nullable=False)
    amount_fen_at_confirm: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "reconciliation_id"],
            ["bank_reconciliations.org_id", "bank_reconciliations.id"],
            name="fk_bank_reconciliation_transaction_org_reconciliation",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "bank_transaction_id"],
            ["bank_transactions.org_id", "bank_transactions.id"],
            name="fk_bank_reconciliation_transaction_org_transaction",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "amount_fen_at_confirm <> 0",
            name="ck_bank_reconciliation_transaction_nonzero",
        ),
    )


class AccountingPeriodCloseBankReconciliation(Base):
    """Append-only edge freezing which explicit reconciliation a close used."""

    __tablename__ = "accounting_period_close_bank_reconciliations"

    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    close_id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True)
    bank_account_code: Mapped[str] = mapped_column(String(30), primary_key=True)
    reconciliation_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, unique=True)
    reconciliation_hash_at_close: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=text("CURRENT_TIMESTAMP")
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "close_id"],
            ["accounting_period_closes.org_id", "accounting_period_closes.id"],
            name="fk_period_close_bank_reconciliation_org_close",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "reconciliation_id"],
            ["bank_reconciliations.org_id", "bank_reconciliations.id"],
            name="fk_period_close_bank_reconciliation_org_reconciliation",
            ondelete="RESTRICT",
        ),
        CheckConstraint(
            "length(reconciliation_hash_at_close) = 64",
            name="ck_period_close_bank_reconciliation_hash",
        ),
    )


class BankTransactionMatch(Base):
    """Append-only bank-to-event matching history with a single active edge."""

    __tablename__ = "bank_transaction_matches"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    bank_transaction_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    event_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    invalidated_by_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    invalidated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "bank_transaction_id"],
            ["bank_transactions.org_id", "bank_transactions.id"],
            name="fk_bank_match_org_transaction",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_bank_match_org_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "invalidated_by_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_bank_match_org_invalidation_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "bank_transaction_id", "event_id", name="uq_bank_match_event"),
        CheckConstraint(
            "(invalidated_by_event_id IS NULL AND invalidated_at IS NULL) OR "
            "(invalidated_by_event_id IS NOT NULL AND invalidated_at IS NOT NULL)",
            name="ck_bank_match_invalidation_pair",
        ),
    )


class OpenItem(Base):
    __tablename__ = "open_items"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    counterparty_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("counterparties.id", ondelete="RESTRICT"), index=True
    )
    source_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT")
    )
    item_type: Mapped[str] = mapped_column(String(20))
    original_amount_fen: Mapped[int] = mapped_column(BigInteger)
    settled_amount_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    status: Mapped[str] = mapped_column(String(20), default="open")
    due_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    payable_category: Mapped[str | None] = mapped_column(String(50), nullable=True)
    payable_agency_code: Mapped[str | None] = mapped_column(String(100), nullable=True)
    insurance_kind: Mapped[str | None] = mapped_column(String(50), nullable=True)

    settlements: Mapped[list[Settlement]] = relationship(
        back_populates="open_item", foreign_keys="Settlement.open_item_id"
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "counterparty_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_open_item_org_counterparty",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "source_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_open_item_org_source_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_open_item_org_id"),
        CheckConstraint("item_type IN ('receivable','payable')", name="ck_open_item_type"),
        CheckConstraint("original_amount_fen > 0", name="ck_open_item_original"),
        CheckConstraint("settled_amount_fen >= 0", name="ck_open_item_settled_positive"),
        CheckConstraint(
            "settled_amount_fen <= original_amount_fen", name="ck_open_item_no_oversettlement"
        ),
        CheckConstraint(
            "status IN ('open','partial','settled','reversed')", name="ck_open_item_status"
        ),
        CheckConstraint(
            "payable_category IS NULL OR (item_type = 'payable' AND payable_category IN "
            "('salary','employer_social','withheld_employee_social','employer_housing',"
            "'withheld_employee_housing','individual_income_tax','labor_remuneration',"
            "'labor_individual_income_tax'))",
            name="ck_open_item_payable_category",
        ),
        CheckConstraint(
            "payable_category IS NOT NULL OR "
            "(payable_agency_code IS NULL AND insurance_kind IS NULL)",
            name="ck_open_item_payable_metadata",
        ),
        CheckConstraint(
            "payable_category NOT IN "
            "('employer_social','withheld_employee_social','employer_housing',"
            "'withheld_employee_housing') OR "
            "(payable_agency_code IS NOT NULL AND insurance_kind IS NOT NULL)",
            name="ck_open_item_statutory_payable_target",
        ),
    )


class Settlement(Base):
    __tablename__ = "settlements"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    open_item_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("open_items.id", ondelete="RESTRICT")
    )
    payment_event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT")
    )
    amount_fen: Mapped[int] = mapped_column(BigInteger)
    reversed: Mapped[bool] = mapped_column(default=False)
    reversed_by_event_id: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)

    open_item: Mapped[OpenItem] = relationship(
        back_populates="settlements", foreign_keys=[open_item_id]
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "open_item_id"],
            ["open_items.org_id", "open_items.id"],
            name="fk_settlement_org_open_item",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "payment_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_settlement_org_payment_event",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "reversed_by_event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_settlement_org_reversal_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("open_item_id", "payment_event_id", name="uq_settlement_event_item"),
        CheckConstraint("amount_fen > 0", name="ck_settlement_amount"),
        CheckConstraint(
            "(reversed IS FALSE AND reversed_by_event_id IS NULL) OR "
            "(reversed IS TRUE AND reversed_by_event_id IS NOT NULL)",
            name="ck_settlement_reversal_audit",
        ),
    )


class VoucherSequence(Base):
    __tablename__ = "voucher_sequences"

    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), primary_key=True
    )
    period_key: Mapped[str] = mapped_column(String(6), primary_key=True)
    next_number: Mapped[int] = mapped_column(Integer, default=1)

    __table_args__ = (CheckConstraint("next_number > 0", name="ck_sequence_positive"),)


class Voucher(Base):
    __tablename__ = "vouchers"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), unique=True
    )
    voucher_number: Mapped[str] = mapped_column(String(30))
    posting_date: Mapped[date] = mapped_column(Date, index=True)
    description: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20), default="posted")
    reversal_of_voucher_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("vouchers.id", ondelete="RESTRICT"), nullable=True
    )
    posted_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    event: Mapped[BusinessEvent] = relationship(back_populates="vouchers", foreign_keys=[event_id])
    lines: Mapped[list[VoucherLine]] = relationship(
        back_populates="voucher",
        cascade="all, delete-orphan",
        foreign_keys="VoucherLine.voucher_id",
        lazy="selectin",
    )

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_voucher_org_event",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("org_id", "id", name="uq_voucher_org_id"),
        UniqueConstraint("org_id", "voucher_number", name="uq_voucher_number"),
        CheckConstraint("status IN ('draft','posted','reversed')", name="ck_voucher_status"),
    )


class VoucherLine(Base):
    __tablename__ = "voucher_lines"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(Uuid, nullable=False, index=True)
    voucher_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("vouchers.id", ondelete="RESTRICT"), index=True
    )
    line_number: Mapped[int] = mapped_column(Integer)
    account_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("accounts.id", ondelete="RESTRICT")
    )
    counterparty_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("counterparties.id", ondelete="RESTRICT"), nullable=True
    )
    debit_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    credit_fen: Mapped[int] = mapped_column(BigInteger, default=0)
    memo: Mapped[str] = mapped_column(Text, default="")

    voucher: Mapped[Voucher] = relationship(back_populates="lines", foreign_keys=[voucher_id])
    account: Mapped[Account] = relationship(lazy="joined", foreign_keys=[account_id])

    __table_args__ = (
        ForeignKeyConstraint(
            ["org_id", "voucher_id"],
            ["vouchers.org_id", "vouchers.id"],
            name="fk_voucher_line_org_voucher",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "account_id"],
            ["accounts.org_id", "accounts.id"],
            name="fk_voucher_line_org_account",
            ondelete="RESTRICT",
        ),
        ForeignKeyConstraint(
            ["org_id", "counterparty_id"],
            ["counterparties.org_id", "counterparties.id"],
            name="fk_voucher_line_org_counterparty",
            ondelete="RESTRICT",
        ),
        UniqueConstraint("voucher_id", "line_number", name="uq_voucher_line_number"),
        CheckConstraint("debit_fen >= 0 AND credit_fen >= 0", name="ck_line_nonnegative"),
        CheckConstraint(
            "(debit_fen > 0 AND credit_fen = 0) OR (credit_fen > 0 AND debit_fen = 0)",
            name="ck_line_one_side",
        ),
    )


class AuditLog(Base):
    __tablename__ = "audit_logs"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    org_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    event_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("business_events.id", ondelete="RESTRICT"), nullable=True
    )
    action: Mapped[str] = mapped_column(String(100))
    actor: Mapped[str] = mapped_column(String(100), default="agent")
    details: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


EXECUTION_ATTRIBUTION_SESSION_KEY = "finance_execution_attribution_id"
_ATTRIBUTED_ROOT_TYPES = (
    AccountingPeriodAction,
    BankTransaction,
    BusinessEvent,
    Employee,
    EmployeePayrollProfileVersion,
    EnterpriseIncomeTaxQuarterConfirmation,
    Evidence,
    FinancialStatementClassification,
    FinancialStatementOpeningBalanceConfirmation,
    LaborExternalDeclarationConfirmation,
    LaborRemunerationBatch,
    LaborServicePerson,
    LaborServicePersonEndAction,
    PayrollBatch,
    PayrollOpeningState,
    PayrollPolicyVersion,
    UnifiedPayoutRun,
    ZeroTaxPeriodConfirmation,
)


def _owner_attribution_mode(session: Session) -> bool:
    connection = session.connection()
    schema = inspect(connection)
    if schema.has_table("organization_database_metadata") and session.scalar(
        select(OrganizationDatabaseMetadata.singleton_key).limit(1)
    ) is not None:
        return True
    return schema.has_table("owner_accounts") and (
        session.scalar(select(OwnerAccount.id).limit(1)) is not None
    )


@event.listens_for(Session, "before_flush")
def _enforce_orm_execution_attribution(
    session: Session,
    _flush_context: object,
    _instances: object,
) -> None:
    """Keep owner-mode ORM writes fail-closed, including direct service calls.

    PostgreSQL receives equivalent guards in the schema baseline. This listener also
    protects SQLite/service tests and automatically attaches the already
    authenticated write-call attribution held in ``Session.info``.
    """

    expected = session.info.get(EXECUTION_ATTRIBUTION_SESSION_KEY)
    owner_mode: bool | None = None

    if any(isinstance(item, ExecutionAttribution) for item in session.deleted):
        raise ValueError("EXECUTION_ATTRIBUTION_APPEND_ONLY")
    for item in session.dirty:
        if isinstance(item, ExecutionAttribution) and session.is_modified(item):
            raise ValueError("EXECUTION_ATTRIBUTION_APPEND_ONLY")

    for root in session.new:
        if not isinstance(root, _ATTRIBUTED_ROOT_TYPES):
            continue
        if owner_mode is None:
            owner_mode = _owner_attribution_mode(session)
        supplied = root.execution_attribution_id
        if expected is not None:
            if supplied is not None and supplied != expected:
                raise ValueError("BUSINESS_EXECUTION_ATTRIBUTION_MISMATCH")
            root.execution_attribution_id = expected
        elif owner_mode:
            raise ValueError("BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED")

    for root in session.dirty:
        if not isinstance(root, _ATTRIBUTED_ROOT_TYPES):
            continue
        if root.execution_attribution_id is None:
            if expected is not None:
                root.execution_attribution_id = expected
            else:
                if owner_mode is None:
                    owner_mode = _owner_attribution_mode(session)
                if owner_mode:
                    raise ValueError("BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED")
        history = attributes.get_history(root, "execution_attribution_id")
        if history.has_changes() and not (
            list(history.deleted) == [None]
            and expected is not None
            and list(history.added) == [expected]
        ):
            raise ValueError("BUSINESS_EXECUTION_ATTRIBUTION_IMMUTABLE")


@event.listens_for(Session, "before_flush")
def _enforce_financial_statement_facts_append_only(
    session: Session, _flush_context: object, _instances: object
) -> None:
    fact_types = (
        FinancialStatementClassification,
        FinancialStatementOpeningBalanceConfirmation,
        EnterpriseIncomeTaxQuarterConfirmation,
    )
    if any(isinstance(item, fact_types) for item in session.deleted):
        raise ValueError("FINANCIAL_STATEMENT_FACT_IMMUTABLE")
    for item in session.dirty:
        if isinstance(item, fact_types) and session.is_modified(item):
            raise ValueError("FINANCIAL_STATEMENT_FACT_IMMUTABLE")


@event.listens_for(Session, "before_flush")
def _enforce_organization_profile_append_only(
    session: Session, _flush_context: object, _instances: object
) -> None:
    immutable_types = (OrganizationProfileVersion, OrganizationProfileVersionEvidence)
    if any(isinstance(item, immutable_types) for item in session.deleted):
        raise ValueError("ORGANIZATION_PROFILE_IMMUTABLE")
    for item in session.dirty:
        if isinstance(item, immutable_types) and session.is_modified(item):
            raise ValueError("ORGANIZATION_PROFILE_IMMUTABLE")


Index("ix_open_items_org_status", OpenItem.org_id, OpenItem.item_type, OpenItem.status)
Index(
    "ix_open_items_payable_category",
    OpenItem.org_id,
    OpenItem.payable_category,
    OpenItem.payable_agency_code,
    OpenItem.insurance_kind,
    OpenItem.status,
)
Index("ix_events_org_posting", BusinessEvent.org_id, BusinessEvent.posting_date)
Index(
    "uq_financial_statement_classification_initial",
    FinancialStatementClassification.org_id,
    FinancialStatementClassification.voucher_line_id,
    unique=True,
    postgresql_where=FinancialStatementClassification.supersedes_id.is_(None),
    sqlite_where=FinancialStatementClassification.supersedes_id.is_(None),
)
Index(
    "uq_bank_transaction_match_current",
    BankTransactionMatch.org_id,
    BankTransactionMatch.bank_transaction_id,
    unique=True,
    postgresql_where=BankTransactionMatch.invalidated_by_event_id.is_(None),
    sqlite_where=BankTransactionMatch.invalidated_by_event_id.is_(None),
)
Index(
    "uq_payroll_event_link_without_source",
    PayrollEventLink.org_id,
    PayrollEventLink.event_id,
    PayrollEventLink.link_kind,
    unique=True,
    postgresql_where=(
        PayrollEventLink.source_payment_event_id.is_(None)
        & PayrollEventLink.source_open_item_id.is_(None)
    ),
    sqlite_where=(
        PayrollEventLink.source_payment_event_id.is_(None)
        & PayrollEventLink.source_open_item_id.is_(None)
    ),
)
Index(
    "uq_payroll_event_link_salary_source",
    PayrollEventLink.org_id,
    PayrollEventLink.event_id,
    PayrollEventLink.link_kind,
    PayrollEventLink.source_open_item_id,
    unique=True,
    postgresql_where=(
        PayrollEventLink.source_payment_event_id.is_(None)
        & PayrollEventLink.source_open_item_id.is_not(None)
    ),
    sqlite_where=(
        PayrollEventLink.source_payment_event_id.is_(None)
        & PayrollEventLink.source_open_item_id.is_not(None)
    ),
)
Index(
    "uq_payroll_event_link_payment_source",
    PayrollEventLink.org_id,
    PayrollEventLink.event_id,
    PayrollEventLink.link_kind,
    PayrollEventLink.source_payment_event_id,
    PayrollEventLink.source_open_item_id,
    unique=True,
    postgresql_where=(
        PayrollEventLink.source_payment_event_id.is_not(None)
        & PayrollEventLink.source_open_item_id.is_not(None)
    ),
    sqlite_where=(
        PayrollEventLink.source_payment_event_id.is_not(None)
        & PayrollEventLink.source_open_item_id.is_not(None)
    ),
)
Index(
    "uq_payroll_event_link_reversal_source",
    PayrollEventLink.org_id,
    PayrollEventLink.event_id,
    PayrollEventLink.link_kind,
    PayrollEventLink.source_payment_event_id,
    unique=True,
    postgresql_where=(
        PayrollEventLink.source_payment_event_id.is_not(None)
        & PayrollEventLink.source_open_item_id.is_(None)
    ),
    sqlite_where=(
        PayrollEventLink.source_payment_event_id.is_not(None)
        & PayrollEventLink.source_open_item_id.is_(None)
    ),
)
Index(
    "ix_employee_payroll_profile_effective",
    EmployeePayrollProfileVersion.employee_id,
    EmployeePayrollProfileVersion.effective_from,
    EmployeePayrollProfileVersion.effective_to,
)
Index(
    "ix_payroll_policy_effective",
    PayrollPolicyVersion.org_id,
    PayrollPolicyVersion.region,
    PayrollPolicyVersion.effective_from,
    PayrollPolicyVersion.effective_to,
)
Index(
    "ix_payroll_batch_org_period",
    PayrollBatch.org_id,
    PayrollBatch.batch_kind,
    PayrollBatch.payroll_period,
    PayrollBatch.status,
)
Index(
    "uq_payroll_regular_posted_period",
    PayrollBatch.org_id,
    PayrollBatch.payroll_period,
    unique=True,
    postgresql_where=(
        (PayrollBatch.batch_kind == "regular")
        & (PayrollBatch.status == "posted")
        & PayrollBatch.reversal_of_batch_id.is_(None)
    ),
    sqlite_where=(
        (PayrollBatch.batch_kind == "regular")
        & (PayrollBatch.status == "posted")
        & PayrollBatch.reversal_of_batch_id.is_(None)
    ),
)
