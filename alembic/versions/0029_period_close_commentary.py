"""Add immutable AI management commentary for accounting-period closes.

Revision ID: 0029_close_commentary
Revises: 0028_quarterly_statements
Create Date: 2026-08-27
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0029_close_commentary"
down_revision = "0028_quarterly_statements"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_period_close_commentaries",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("close_id", sa.Uuid(), nullable=False),
        sa.Column("commentary", sa.Text(), nullable=False),
        sa.Column("prompt_version", sa.String(length=80), nullable=False),
        sa.Column("context_payload", sa.JSON(), nullable=False),
        sa.Column("context_hash", sa.String(length=64), nullable=False),
        sa.Column("generation_method", sa.String(length=40), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "close_id"],
            ["accounting_period_closes.org_id", "accounting_period_closes.id"],
            name="fk_period_close_commentary_org_close",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("close_id"),
        sa.UniqueConstraint("org_id", "id", name="uq_period_close_commentary_org_id"),
        sa.CheckConstraint(
            "length(trim(commentary)) BETWEEN 1 AND 2000",
            name="ck_period_close_commentary_text",
        ),
        sa.CheckConstraint(
            "length(prompt_version) BETWEEN 1 AND 80",
            name="ck_period_close_commentary_prompt_version",
        ),
        sa.CheckConstraint(
            "length(context_hash) = 64",
            name="ck_period_close_commentary_context_hash_length",
        ),
        sa.CheckConstraint(
            "generation_method IN ('close_ai_agent','historical_ai_backfill')",
            name="ck_period_close_commentary_generation_method",
        ),
    )
    op.create_index(
        "ix_accounting_period_close_commentaries_org_id",
        "accounting_period_close_commentaries",
        ["org_id"],
    )

    if op.get_bind().dialect.name == "postgresql":
        op.create_check_constraint(
            "ck_period_close_commentary_context_hash_lower_hex",
            "accounting_period_close_commentaries",
            "context_hash ~ '^[0-9a-f]{64}$'",
        )
        op.execute(
            """
            CREATE FUNCTION finance_block_period_close_commentary_0029()
            RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN
                RAISE EXCEPTION 'ACCOUNTING_PERIOD_CLOSE_COMMENTARY_IMMUTABLE';
            END;
            $$
            """
        )
        op.execute(
            """
            CREATE TRIGGER accounting_period_close_commentaries_immutable_0029
            BEFORE UPDATE OR DELETE ON accounting_period_close_commentaries
            FOR EACH ROW EXECUTE FUNCTION finance_block_period_close_commentary_0029()
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text("SELECT EXISTS (SELECT 1 FROM accounting_period_close_commentaries)")
    ):
        raise RuntimeError("ACCOUNTING_PERIOD_CLOSE_COMMENTARY_DOWNGRADE_UNSAFE")
    if bind.dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER accounting_period_close_commentaries_immutable_0029 "
            "ON accounting_period_close_commentaries"
        )
        op.execute("DROP FUNCTION finance_block_period_close_commentary_0029()")
    op.drop_index(
        "ix_accounting_period_close_commentaries_org_id",
        table_name="accounting_period_close_commentaries",
    )
    op.drop_table("accounting_period_close_commentaries")
