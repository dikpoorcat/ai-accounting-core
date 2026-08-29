"""Add the single-company business-database half of catalog routing.

Revision ID: 0002_multi_company_business
Revises: 0001_formal_baseline
Create Date: 2026-08-28
"""

from __future__ import annotations

import uuid
from datetime import date

import sqlalchemy as sa

from alembic import context, op

revision = "0002_multi_company_business"
down_revision = "0001_formal_baseline"
branch_labels = None
depends_on = None

_ZERO_UUID = uuid.UUID(int=0)


def upgrade() -> None:
    bind = op.get_bind()
    attributes = context.config.attributes
    catalog_id = _uuid_attribute(attributes.get("catalog_instance_id"), _ZERO_UUID)
    database_identity = _uuid_attribute(
        attributes.get("company_database_identity"), uuid.uuid4()
    )
    configured_org_id = attributes.get("company_org_id")

    op.add_column(
        "execution_attributions",
        sa.Column(
            "catalog_instance_id",
            sa.Uuid(),
            nullable=False,
            server_default=str(catalog_id),
        ),
    )
    op.add_column(
        "accounting_period_close_approvals",
        sa.Column(
            "catalog_instance_id",
            sa.Uuid(),
            nullable=False,
            server_default=str(catalog_id),
        ),
    )
    op.create_table(
        "organization_database_metadata",
        sa.Column("singleton_key", sa.Integer(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("database_identity", sa.Uuid(), nullable=False),
        sa.Column("current_catalog_instance_id", sa.Uuid(), nullable=False),
        sa.Column("owner_approval_required", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("singleton_key = 1", name="ck_org_database_metadata_singleton"),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_org_database_metadata_org"
        ),
        sa.PrimaryKeyConstraint("singleton_key"),
        sa.UniqueConstraint("database_identity"),
        sa.UniqueConstraint("org_id"),
    )
    op.create_table(
        "organization_profile_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("effective_from", sa.Date(), nullable=False),
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("taxpayer_identification_number", sa.String(length=18), nullable=False),
        sa.Column("taxpayer_type", sa.String(length=30), nullable=False),
        sa.Column("filing_cycle", sa.String(length=20), nullable=False),
        sa.Column("jurisdiction", sa.String(length=100), nullable=False),
        sa.Column("urban_maintenance_rate", sa.Numeric(precision=6, scale=5), nullable=False),
        sa.Column("accounting_standard", sa.String(length=50), nullable=False),
        sa.Column("confirmation_note", sa.Text(), nullable=False),
        sa.Column("lifecycle_action_id", sa.Uuid(), nullable=True),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint(
            "accounting_standard = 'small_enterprise'",
            name="ck_org_profile_accounting_standard",
        ),
        sa.CheckConstraint(
            "filing_cycle IN ('monthly','quarterly')", name="ck_org_profile_filing_cycle"
        ),
        sa.CheckConstraint(
            "jurisdiction = 'CN'", name="ck_org_profile_jurisdiction"
        ),
        sa.CheckConstraint(
            "length(trim(confirmation_note)) > 0", name="ck_org_profile_confirmation_note"
        ),
        sa.CheckConstraint(
            "taxpayer_type = 'small_scale'", name="ck_org_profile_small_scale"
        ),
        sa.CheckConstraint(
            "length(taxpayer_identification_number) = 18 AND "
            "taxpayer_identification_number = upper(taxpayer_identification_number)",
            name="ck_org_profile_taxpayer_id",
        ),
        sa.CheckConstraint(
            "urban_maintenance_rate IN (0.07,0.05,0.01)",
            name="ck_org_profile_urban_rate",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"], ["organizations.id"], name="fk_org_profile_version_org"
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_org_profile_version_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id", "effective_from", name="uq_org_profile_version_effective_from"
        ),
        sa.UniqueConstraint("org_id", "id", name="uq_org_profile_version_org_id"),
    )
    op.create_index(
        op.f("ix_organization_profile_versions_org_id"),
        "organization_profile_versions",
        ["org_id"],
        unique=False,
    )
    op.create_table(
        "organization_profile_version_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("profile_version_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_org_profile_evidence_evidence",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "profile_version_id"],
            ["organization_profile_versions.org_id", "organization_profile_versions.id"],
            name="fk_org_profile_evidence_version",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "profile_version_id", "evidence_id"),
    )

    organizations = bind.execute(
        sa.text(
            "SELECT id, name, taxpayer_identification_number, taxpayer_type, filing_cycle, "
            "jurisdiction, urban_maintenance_rate, accounting_standard "
            "FROM organizations ORDER BY id"
        )
    ).mappings().all()
    if len(organizations) > 1:
        raise RuntimeError("MULTI_COMPANY_BUSINESS_DATABASE_REQUIRES_EXACTLY_ONE_ORGANIZATION")
    if organizations:
        organization = organizations[0]
        org_id = uuid.UUID(str(organization["id"]))
        if configured_org_id is not None and org_id != uuid.UUID(str(configured_org_id)):
            raise RuntimeError("COMPANY_ORGANIZATION_ID_MISMATCH")
        bind.execute(
            sa.text(
                "INSERT INTO organization_database_metadata "
                "(singleton_key, org_id, database_identity, current_catalog_instance_id, "
                "owner_approval_required, created_at) "
                "VALUES (1, :org_id, :database_identity, :catalog_id, true, CURRENT_TIMESTAMP)"
            ),
            {
                "org_id": org_id,
                "database_identity": database_identity,
                "catalog_id": catalog_id,
            },
        )
        bind.execute(
            sa.text(
                "INSERT INTO organization_profile_versions "
                "(id, org_id, effective_from, name, taxpayer_identification_number, "
                "taxpayer_type, filing_cycle, jurisdiction, urban_maintenance_rate, "
                "accounting_standard, confirmation_note, lifecycle_action_id, "
                "execution_attribution_id, created_at) "
                "VALUES (:id, :org_id, :effective_from, :name, :taxpayer_id, :taxpayer_type, "
                ":filing_cycle, :jurisdiction, :urban_rate, :accounting_standard, "
                ":confirmation_note, NULL, NULL, CURRENT_TIMESTAMP)"
            ),
            {
                "id": uuid.uuid5(org_id, "migration-baseline-profile"),
                "org_id": org_id,
                "effective_from": date(1, 1, 1),
                "name": organization["name"],
                "taxpayer_id": organization["taxpayer_identification_number"],
                "taxpayer_type": organization["taxpayer_type"],
                "filing_cycle": organization["filing_cycle"],
                "jurisdiction": organization["jurisdiction"],
                "urban_rate": organization["urban_maintenance_rate"],
                "accounting_standard": organization["accounting_standard"],
                "confirmation_note": "0001 正式库前向迁移基线；保持既有历史计算不变。",
            },
        )

    if bind.dialect.name == "postgresql":
        _install_postgresql_profile_guards()
        if attributes.get("identity_split_verified") is True:
            _split_postgresql_identity_tables()


def _install_postgresql_profile_guards() -> None:
    bind = op.get_bind()
    bind.exec_driver_sql(_PROFILE_APPEND_ONLY_FUNCTION)
    bind.exec_driver_sql(_PROFILE_INSERT_VALIDATION_FUNCTION)
    bind.exec_driver_sql(_PROFILE_PROJECTION_FUNCTION)
    bind.exec_driver_sql(
        "CREATE TRIGGER organization_profile_attribution_guard "
        "BEFORE INSERT OR UPDATE ON organization_profile_versions FOR EACH ROW "
        "EXECUTE FUNCTION finance_guard_attributed_root_0014()"
    )
    bind.exec_driver_sql(
        "CREATE TRIGGER organization_profile_append_only "
        "BEFORE UPDATE OR DELETE ON organization_profile_versions FOR EACH ROW "
        "EXECUTE FUNCTION finance_guard_organization_profile_append_only_0002()"
    )
    bind.exec_driver_sql(
        "CREATE TRIGGER organization_profile_evidence_append_only "
        "BEFORE UPDATE OR DELETE ON organization_profile_version_evidence FOR EACH ROW "
        "EXECUTE FUNCTION finance_guard_organization_profile_append_only_0002()"
    )
    bind.exec_driver_sql(
        "CREATE CONSTRAINT TRIGGER organization_profile_insert_validation "
        "AFTER INSERT ON organization_profile_versions DEFERRABLE INITIALLY DEFERRED "
        "FOR EACH ROW EXECUTE FUNCTION finance_validate_organization_profile_insert_0002()"
    )
    bind.exec_driver_sql(
        "CREATE TRIGGER organization_profile_projection_guard "
        "BEFORE UPDATE ON organizations FOR EACH ROW "
        "EXECUTE FUNCTION finance_guard_organization_profile_projection_0002()"
    )


def _split_postgresql_identity_tables() -> None:
    bind = op.get_bind()
    owner_count = bind.scalar(sa.text("SELECT COUNT(*) FROM owner_accounts"))
    session_count = bind.scalar(sa.text("SELECT COUNT(*) FROM owner_sessions"))
    recovery_count = bind.scalar(sa.text("SELECT COUNT(*) FROM owner_recovery_codes"))
    audit_count = bind.scalar(sa.text("SELECT COUNT(*) FROM identity_audit_events"))
    if any((owner_count, session_count, recovery_count, audit_count)):
        if context.config.attributes.get("identity_export_verified") is not True:
            raise RuntimeError("CATALOG_IDENTITY_EXPORT_NOT_VERIFIED")

    bind.exec_driver_sql(_EXTERNAL_EXECUTION_ATTRIBUTION_FUNCTION)
    bind.exec_driver_sql(_EXTERNAL_ATTRIBUTED_ROOT_FUNCTION)
    bind.exec_driver_sql(_EXTERNAL_PERIOD_APPROVAL_FUNCTION)
    bind.exec_driver_sql(_EXTERNAL_PAYROLL_IDENTITY_FUNCTION)
    bind.exec_driver_sql(_EXTERNAL_PERIOD_ACTION_IDENTITY_FUNCTION)
    for function_name in (
        "finance_block_identity_audit_mutation_0013",
        "finance_guard_owner_account_0013",
        "finance_guard_owner_recovery_code_0013",
        "finance_guard_owner_session_0013",
    ):
        bind.exec_driver_sql(f"DROP FUNCTION IF EXISTS {function_name}() CASCADE")
    for table_name in (
        "identity_audit_events",
        "owner_recovery_codes",
        "owner_sessions",
        "owner_accounts",
    ):
        bind.exec_driver_sql(f'DROP TABLE IF EXISTS "{table_name}" CASCADE')


def downgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("owner_accounts"):
        raise RuntimeError("MULTI_COMPANY_IDENTITY_SPLIT_HAS_NO_AUTOMATIC_DOWNGRADE")
    if bind.dialect.name == "postgresql":
        bind.exec_driver_sql(
            "DROP TRIGGER IF EXISTS organization_profile_projection_guard ON organizations"
        )
        bind.exec_driver_sql(
            "DROP TRIGGER IF EXISTS organization_profile_insert_validation "
            "ON organization_profile_versions"
        )
        bind.exec_driver_sql(
            "DROP TRIGGER IF EXISTS organization_profile_evidence_append_only "
            "ON organization_profile_version_evidence"
        )
        bind.exec_driver_sql(
            "DROP TRIGGER IF EXISTS organization_profile_append_only "
            "ON organization_profile_versions"
        )
        bind.exec_driver_sql(
            "DROP TRIGGER IF EXISTS organization_profile_attribution_guard "
            "ON organization_profile_versions"
        )
        bind.exec_driver_sql(
            "DROP FUNCTION IF EXISTS finance_guard_organization_profile_projection_0002()"
        )
        bind.exec_driver_sql(
            "DROP FUNCTION IF EXISTS finance_validate_organization_profile_insert_0002()"
        )
        bind.exec_driver_sql(
            "DROP FUNCTION IF EXISTS finance_guard_organization_profile_append_only_0002()"
        )
    op.drop_table("organization_profile_version_evidence")
    op.drop_index(
        op.f("ix_organization_profile_versions_org_id"),
        table_name="organization_profile_versions",
    )
    op.drop_table("organization_profile_versions")
    op.drop_table("organization_database_metadata")
    op.drop_column("accounting_period_close_approvals", "catalog_instance_id")
    op.drop_column("execution_attributions", "catalog_instance_id")


def _uuid_attribute(value: object, default: uuid.UUID) -> uuid.UUID:
    return default if value is None else uuid.UUID(str(value))


_PROFILE_APPEND_ONLY_FUNCTION = r"""
CREATE OR REPLACE FUNCTION finance_guard_organization_profile_append_only_0002()
RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'ORGANIZATION_PROFILE_IMMUTABLE';
END;
$$;
"""

_PROFILE_INSERT_VALIDATION_FUNCTION = r"""
CREATE OR REPLACE FUNCTION finance_validate_organization_profile_insert_0002()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE configured text;
DECLARE attribution_xmin xid;
DECLARE profile_count bigint;
BEGIN
    configured := current_setting('finance.execution_attribution_id', true);
    SELECT xmin INTO attribution_xmin FROM execution_attributions
     WHERE org_id = NEW.org_id AND id = NEW.execution_attribution_id;
    SELECT count(*) INTO profile_count FROM organization_profile_versions
     WHERE org_id = NEW.org_id;
    IF NEW.execution_attribution_id IS NULL
       OR configured IS NULL
       OR configured <> NEW.execution_attribution_id::text
       OR attribution_xmin IS NULL
       OR pg_xact_status((attribution_xmin::text)::xid8) <> 'in progress'
       OR (profile_count > 1 AND NEW.lifecycle_action_id IS NULL)
       OR (profile_count > 1 AND NOT EXISTS (
            SELECT 1 FROM organization_profile_version_evidence evidence
             WHERE evidence.org_id = NEW.org_id
               AND evidence.profile_version_id = NEW.id)) THEN
        RAISE EXCEPTION 'ORGANIZATION_PROFILE_AUTHORITY_INVALID';
    END IF;
    RETURN NULL;
END;
$$;
"""

_PROFILE_PROJECTION_FUNCTION = r"""
CREATE OR REPLACE FUNCTION finance_guard_organization_profile_projection_0002()
RETURNS trigger LANGUAGE plpgsql AS $$
DECLARE configured text;
DECLARE profile_record record;
DECLARE profile_xmin xid;
BEGIN
    IF NEW.name IS NOT DISTINCT FROM OLD.name
       AND NEW.taxpayer_identification_number IS NOT DISTINCT FROM
           OLD.taxpayer_identification_number
       AND NEW.taxpayer_type IS NOT DISTINCT FROM OLD.taxpayer_type
       AND NEW.filing_cycle IS NOT DISTINCT FROM OLD.filing_cycle
       AND NEW.jurisdiction IS NOT DISTINCT FROM OLD.jurisdiction
       AND NEW.urban_maintenance_rate IS NOT DISTINCT FROM OLD.urban_maintenance_rate
       AND NEW.accounting_standard IS NOT DISTINCT FROM OLD.accounting_standard THEN
        RETURN NEW;
    END IF;
    SELECT profile.*, profile.xmin INTO profile_record
      FROM organization_profile_versions profile
     WHERE profile.org_id = NEW.id
     ORDER BY profile.effective_from DESC, profile.id DESC
     LIMIT 1;
    profile_xmin := profile_record.xmin;
    configured := current_setting('finance.execution_attribution_id', true);
    IF profile_record.id IS NULL
       OR profile_record.lifecycle_action_id IS NULL
       OR configured IS NULL
       OR configured <> profile_record.execution_attribution_id::text
       OR profile_xmin IS NULL
       OR pg_xact_status((profile_xmin::text)::xid8) <> 'in progress'
       OR NEW.name IS DISTINCT FROM profile_record.name
       OR NEW.taxpayer_identification_number IS DISTINCT FROM
           profile_record.taxpayer_identification_number
       OR NEW.taxpayer_type IS DISTINCT FROM profile_record.taxpayer_type
       OR NEW.filing_cycle IS DISTINCT FROM profile_record.filing_cycle
       OR NEW.jurisdiction IS DISTINCT FROM profile_record.jurisdiction
       OR NEW.urban_maintenance_rate IS DISTINCT FROM
           profile_record.urban_maintenance_rate
       OR NEW.accounting_standard IS DISTINCT FROM profile_record.accounting_standard THEN
        RAISE EXCEPTION 'ORGANIZATION_PROFILE_PROJECTION_INVALID';
    END IF;
    RETURN NEW;
END;
$$;
"""


_EXTERNAL_EXECUTION_ATTRIBUTION_FUNCTION = r"""
CREATE OR REPLACE FUNCTION finance_guard_execution_attribution_0014() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE configured text;
DECLARE binding organization_database_metadata%%ROWTYPE;
BEGIN
    IF TG_OP <> 'INSERT' THEN
        RAISE EXCEPTION 'EXECUTION_ATTRIBUTION_APPEND_ONLY';
    END IF;
    SELECT * INTO binding FROM organization_database_metadata WHERE singleton_key = 1;
    configured := current_setting('finance.execution_attribution_id', true);
    IF binding.org_id IS NULL
       OR NEW.org_id <> binding.org_id
       OR NEW.catalog_instance_id <> binding.current_catalog_instance_id
       OR NEW.catalog_instance_id = '00000000-0000-0000-0000-000000000000'::uuid
       OR NEW.owner_credential_version < 1
       OR NEW.created_at > clock_timestamp()
       OR configured IS NULL
       OR configured <> NEW.id::text
       OR NEW.executor_name !~ '^[A-Za-z0-9._:-]{1,100}$'
       OR NEW.executor_version !~ '^[A-Za-z0-9._:-]{1,100}$'
       OR NEW.tool_name !~ '^finance_[a-z0-9_]{1,92}$' THEN
        RAISE EXCEPTION 'EXECUTION_ATTRIBUTION_AUTHORITY_INVALID';
    END IF;
    RETURN NEW;
END;
$$;
"""

_EXTERNAL_ATTRIBUTED_ROOT_FUNCTION = r"""
CREATE OR REPLACE FUNCTION finance_guard_attributed_root_0014() RETURNS trigger
LANGUAGE plpgsql AS $$
DECLARE attribution_xmin xid;
DECLARE configured text;
BEGIN
    IF TG_OP = 'UPDATE'
       AND NEW.execution_attribution_id IS DISTINCT FROM OLD.execution_attribution_id
       AND NOT (OLD.execution_attribution_id IS NULL
                AND NEW.execution_attribution_id IS NOT NULL) THEN
        RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_IMMUTABLE';
    END IF;
    IF NEW.execution_attribution_id IS NULL THEN
        RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_REQUIRED';
    END IF;
    SELECT xmin INTO attribution_xmin FROM execution_attributions
     WHERE org_id = NEW.org_id AND id = NEW.execution_attribution_id;
    IF attribution_xmin IS NULL THEN
        RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_MISMATCH';
    END IF;
    IF TG_OP = 'INSERT' OR OLD.execution_attribution_id IS NULL THEN
        configured := current_setting('finance.execution_attribution_id', true);
        IF configured IS NULL
           OR configured <> NEW.execution_attribution_id::text
           OR pg_xact_status((attribution_xmin::text)::xid8) <> 'in progress' THEN
            RAISE EXCEPTION 'BUSINESS_EXECUTION_ATTRIBUTION_NOT_CURRENT';
        END IF;
    END IF;
    RETURN NEW;
END;
$$;
"""

_EXTERNAL_PERIOD_APPROVAL_FUNCTION = r"""
CREATE OR REPLACE FUNCTION finance_assert_period_close_owner_approval(target_close_id uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE target_close accounting_period_closes%%ROWTYPE;
DECLARE target_action accounting_period_actions%%ROWTYPE;
DECLARE owner_required boolean;
BEGIN
    SELECT * INTO target_close FROM accounting_period_closes WHERE id = target_close_id;
    IF NOT FOUND THEN RETURN; END IF;
    SELECT * INTO target_action FROM accounting_period_actions
     WHERE org_id = target_close.org_id AND id = target_close.action_id;
    SELECT owner_approval_required INTO owner_required
      FROM organization_database_metadata WHERE singleton_key = 1;
    IF target_action.id IS NULL
       OR target_action.input_facts::jsonb ->> 'owner_approval_id'
            IS DISTINCT FROM target_close.owner_approval_id::text
       OR (owner_required AND (
            target_close.owner_approval_id IS NULL OR NOT EXISTS (
                SELECT 1 FROM accounting_period_close_approvals AS approval
                 WHERE approval.org_id = target_close.org_id
                   AND approval.id = target_close.owner_approval_id
                   AND approval.catalog_instance_id = (
                       SELECT current_catalog_instance_id
                         FROM organization_database_metadata WHERE singleton_key = 1)
                   AND approval.period_id = target_close.period_id
                   AND approval.calculation_hash = target_close.calculation_hash
                   AND approval.confirmation_method = 'local_password_reauthentication'
                   AND approval.consumed_at IS NOT NULL
                   AND approval.confirmed_at <= approval.consumed_at
                   AND approval.consumed_at <= target_close.confirmed_at
                   AND approval.expires_at >= target_close.confirmed_at)))
       OR (NOT owner_required AND target_close.owner_approval_id IS NOT NULL) THEN
        RAISE EXCEPTION 'ACCOUNTING_PERIOD_OWNER_APPROVAL_INVALID';
    END IF;
END;
$$;
"""

_EXTERNAL_PAYROLL_IDENTITY_FUNCTION = r"""
CREATE OR REPLACE FUNCTION finance_guard_payroll_batch_identity_0014() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF (TG_OP = 'INSERT' AND NEW.confirmed_by IS NOT NULL)
       OR (TG_OP = 'UPDATE' AND OLD.confirmed_by IS NULL AND NEW.confirmed_by IS NOT NULL) THEN
        RAISE EXCEPTION 'CALLER_CONFIRMER_IDENTITY_FORBIDDEN';
    END IF;
    RETURN NEW;
END;
$$;
"""

_EXTERNAL_PERIOD_ACTION_IDENTITY_FUNCTION = r"""
CREATE OR REPLACE FUNCTION finance_guard_period_action_identity_0014() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.confirmed_by IS NOT NULL
       OR jsonb_path_exists(NEW.input_facts::jsonb, '$.**.confirmed_by') THEN
        RAISE EXCEPTION 'CALLER_CONFIRMER_IDENTITY_FORBIDDEN';
    END IF;
    RETURN NEW;
END;
$$;
"""
