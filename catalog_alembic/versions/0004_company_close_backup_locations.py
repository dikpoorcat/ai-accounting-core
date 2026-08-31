"""Scope automatic close-backup locations to one company.

Revision ID: 0004_company_backup_locations
Revises: 0003_close_backup
Create Date: 2026-08-31
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0004_company_backup_locations"
down_revision = "0003_close_backup"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "close_backup_location_versions",
        sa.Column("org_id", sa.Uuid(), nullable=True),
    )
    op.create_foreign_key(
        "fk_close_backup_location_company",
        "close_backup_location_versions",
        "company_registry",
        ["org_id"],
        ["org_id"],
        ondelete="RESTRICT",
    )
    op.drop_constraint(
        "close_backup_location_versions_version_key",
        "close_backup_location_versions",
        type_="unique",
    )
    op.drop_constraint(
        "close_backup_location_versions_idempotency_key_key",
        "close_backup_location_versions",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_close_backup_location_org_version",
        "close_backup_location_versions",
        ["org_id", "version"],
    )
    op.create_unique_constraint(
        "uq_close_backup_location_org_idempotency",
        "close_backup_location_versions",
        ["org_id", "idempotency_key"],
    )
    op.create_index(
        op.f("ix_close_backup_location_versions_org_id"),
        "close_backup_location_versions",
        ["org_id"],
    )


def downgrade() -> None:
    raise RuntimeError("CATALOG_DATABASE_HAS_NO_AUTOMATIC_DOWNGRADE")
