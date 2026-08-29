"""Add owner-configured automatic post-close company backups.

Revision ID: 0003_close_backup
Revises: 0002_company_primary
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_close_backup"
down_revision = "0002_company_primary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "close_backup_location_versions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("backup_directory", sa.Text(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_note", sa.Text(), nullable=False),
        sa.Column("owner_account_id", sa.Uuid(), nullable=False),
        sa.Column("owner_session_id", sa.Uuid(), nullable=False),
        sa.Column("owner_credential_version", sa.Integer(), nullable=False),
        sa.Column("executor_kind", sa.String(length=30), nullable=False),
        sa.Column("executor_name", sa.String(length=100), nullable=False),
        sa.Column("executor_version", sa.String(length=100), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.CheckConstraint("version >= 1", name="ck_close_backup_location_version"),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64",
            name="ck_close_backup_location_request_hash",
        ),
        sa.CheckConstraint(
            "owner_credential_version >= 1",
            name="ck_close_backup_location_credential_version",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("version"),
    )
    op.create_table(
        "accounting_period_close_backups",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("close_id", sa.Uuid(), nullable=False),
        sa.Column("period_id", sa.Uuid(), nullable=False),
        sa.Column("period_month", sa.String(length=7), nullable=False),
        sa.Column("database_identity", sa.Uuid(), nullable=False),
        sa.Column("location_version_id", sa.Uuid(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("archive_file", sa.Text(), nullable=True),
        sa.Column("archive_sha256", sa.String(length=64), nullable=True),
        sa.Column("manifest_sha256", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column(
            "requested_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("attempt_count >= 0", name="ck_close_backup_attempt_count"),
        sa.CheckConstraint(
            "(status = 'completed' AND archive_file IS NOT NULL "
            "AND archive_sha256 IS NOT NULL AND manifest_sha256 IS NOT NULL "
            "AND error_code IS NULL AND completed_at IS NOT NULL) OR "
            "(status <> 'completed' AND archive_file IS NULL "
            "AND archive_sha256 IS NULL AND manifest_sha256 IS NULL)",
            name="ck_close_backup_completion",
        ),
        sa.CheckConstraint(
            "length(period_month) = 7 AND substr(period_month, 5, 1) = '-'",
            name="ck_close_backup_period_month",
        ),
        sa.CheckConstraint(
            "status IN ('pending','running','completed','failed')",
            name="ck_close_backup_status",
        ),
        sa.ForeignKeyConstraint(
            ["location_version_id"],
            ["close_backup_location_versions.id"],
            name="fk_close_backup_location_version",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["company_registry.org_id"],
            name="fk_close_backup_company",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "close_id", name="uq_close_backup_org_close"),
    )
    op.create_index(
        op.f("ix_accounting_period_close_backups_org_id"),
        "accounting_period_close_backups",
        ["org_id"],
    )


def downgrade() -> None:
    raise RuntimeError("CATALOG_DATABASE_HAS_NO_AUTOMATIC_DOWNGRADE")
