"""Persist owner-confirmed historical statutory-obligation cutoffs.

Revision ID: 0018_historical_obligation
Revises: 0017_owner_workflow
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_historical_obligation"
down_revision = "0017_owner_workflow"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "historical_obligation_completion_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("obligation_code", sa.String(length=80), nullable=False),
        sa.Column("obligation_scope", sa.String(length=20), nullable=False),
        sa.Column("completion_through_identity", sa.String(length=7), nullable=False),
        sa.Column("completion_date_status", sa.String(length=30), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_note", sa.Text(), nullable=False),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint(
            "id", name="pk_historical_obligation_completion_confirmations"
        ),
        sa.UniqueConstraint(
            "org_id", "id", name="uq_historical_obligation_completion_org_id"
        ),
        sa.UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_historical_obligation_completion_idempotency",
        ),
        sa.UniqueConstraint(
            "supersedes_id", name="uq_historical_obligation_completion_successor"
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_historical_obligation_completion_org",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "supersedes_id"],
            [
                "historical_obligation_completion_confirmations.org_id",
                "historical_obligation_completion_confirmations.id",
            ],
            name="fk_historical_obligation_completion_org_supersedes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_historical_obligation_completion_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "obligation_code IN ('periodic_tax_reporting',"
            "'annual_enterprise_income_tax','annual_business_report')",
            name="ck_historical_obligation_completion_code",
        ),
        sa.CheckConstraint(
            "(obligation_code = 'periodic_tax_reporting' AND obligation_scope IN "
            "('month','quarter')) OR "
            "(obligation_code IN ('annual_enterprise_income_tax','annual_business_report') "
            "AND obligation_scope = 'year')",
            name="ck_historical_obligation_completion_scope",
        ),
        sa.CheckConstraint(
            "completion_date_status = 'not_established'",
            name="ck_historical_obligation_completion_date_status",
        ),
        sa.CheckConstraint(
            "length(source_snapshot_hash) = 64 AND length(request_payload_hash) = 64",
            name="ck_historical_obligation_completion_hashes",
        ),
        sa.CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_historical_obligation_completion_note",
        ),
    )
    op.create_index(
        "ix_historical_obligation_completion_confirmations_org_id",
        "historical_obligation_completion_confirmations",
        ["org_id"],
    )
    op.create_index(
        "uq_historical_obligation_completion_root",
        "historical_obligation_completion_confirmations",
        ["org_id", "obligation_code"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
        sqlite_where=sa.text("supersedes_id IS NULL"),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER historical_obligation_completion_execution_guard "
            "BEFORE INSERT OR UPDATE ON historical_obligation_completion_confirmations "
            "FOR EACH ROW EXECUTE FUNCTION finance_guard_attributed_root_0014()"
        )
        op.execute(
            "CREATE TRIGGER historical_obligation_completion_append_only_guard "
            "BEFORE DELETE OR UPDATE ON historical_obligation_completion_confirmations "
            "FOR EACH ROW EXECUTE FUNCTION finance_block_financial_statement_fact_0028()"
        )


def downgrade() -> None:
    op.drop_index(
        "uq_historical_obligation_completion_root",
        table_name="historical_obligation_completion_confirmations",
    )
    op.drop_index(
        "ix_historical_obligation_completion_confirmations_org_id",
        table_name="historical_obligation_completion_confirmations",
    )
    op.drop_table("historical_obligation_completion_confirmations")
