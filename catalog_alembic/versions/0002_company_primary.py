"""Add an explicit primary company for deterministic default selection.

Revision ID: 0002_company_primary
Revises: 0001_catalog
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0002_company_primary"
down_revision = "0001_catalog"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "company_registry",
        sa.Column(
            "is_primary",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(
        sa.text(
            """
            UPDATE company_registry
               SET is_primary = TRUE
             WHERE org_id = (
                 SELECT org_id
                   FROM company_registry
                  ORDER BY created_at, org_id
                  LIMIT 1
             )
            """
        )
    )
    op.create_index(
        "uq_company_registry_single_primary",
        "company_registry",
        ["is_primary"],
        unique=True,
        postgresql_where=sa.text("is_primary"),
    )
    op.alter_column("company_registry", "is_primary", server_default=None)


def downgrade() -> None:
    op.drop_index("uq_company_registry_single_primary", table_name="company_registry")
    op.drop_column("company_registry", "is_primary")
