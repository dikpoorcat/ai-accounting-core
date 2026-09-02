"""Create the flattened catalog schema from an empty PostgreSQL database.

Revision ID: 0001_catalog_baseline_v2
Revises:
Create Date: 2026-09-02
"""

from __future__ import annotations

import uuid
from pathlib import Path

import sqlalchemy as sa

from alembic import context, op

revision = "0001_catalog_baseline_v2"
down_revision = None
branch_labels = None
depends_on = None


_BASELINE_SQL = Path(__file__).resolve().parents[1] / "baseline" / "postgresql.sql"


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        raise RuntimeError("CATALOG_BASELINE_REQUIRES_POSTGRESQL")
    raw_connection = bind.connection.driver_connection
    with raw_connection.cursor() as cursor:
        cursor.execute(_BASELINE_SQL.read_text(encoding="utf-8"))
        cursor.execute("SET search_path TO public")
    catalog_id = uuid.UUID(
        str(context.config.attributes.get("catalog_instance_id", uuid.uuid4()))
    )
    bind.execute(
        sa.text(
            "INSERT INTO catalog_metadata "
            "(singleton_key, catalog_instance_id, created_at) "
            "VALUES (1, :catalog_id, CURRENT_TIMESTAMP)"
        ),
        {"catalog_id": catalog_id},
    )


def downgrade() -> None:
    raise RuntimeError("CATALOG_DATABASE_HAS_NO_AUTOMATIC_DOWNGRADE")
