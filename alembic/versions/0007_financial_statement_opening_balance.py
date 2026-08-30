"""Add explicit first-year financial-statement opening-balance confirmations.

Revision ID: 0007_fs_opening_balance
Revises: 0006_payment_platform_transfer
Create Date: 2026-08-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0007_fs_opening_balance"
down_revision = "0006_payment_platform_transfer"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    hash_check = (
        "request_payload_hash ~ '^[0-9a-f]{64}$'"
        if bind.dialect.name == "postgresql"
        else "request_payload_hash NOT GLOB '*[^0-9a-f]*'"
    )
    op.create_table(
        "financial_statement_opening_balance_confirmations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("establishment_date", sa.Date(), nullable=False),
        sa.Column("treatment", sa.String(length=40), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_note", sa.Text(), nullable=False),
        sa.Column("evidence_references", sa.JSON(), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "treatment = 'zero_on_establishment'",
            name="ck_fs_opening_confirmation_treatment",
        ),
        sa.CheckConstraint(
            "length(idempotency_key) BETWEEN 1 AND 200 AND length(request_payload_hash) = 64",
            name="ck_fs_opening_confirmation_request",
        ),
        sa.CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_fs_opening_confirmation_note",
        ),
        sa.CheckConstraint(
            hash_check,
            name="ck_fs_opening_confirmation_hash_lower_hex",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_fs_opening_confirmation_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_fs_opening_confirmation_org",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "org_id",
            "establishment_date",
            name="uq_fs_opening_confirmation_date",
        ),
        sa.UniqueConstraint(
            "org_id",
            "id",
            name="uq_fs_opening_confirmation_org_id",
        ),
        sa.UniqueConstraint(
            "org_id",
            "idempotency_key",
            name="uq_fs_opening_confirmation_idempotency",
        ),
    )
    op.create_index(
        "ix_financial_statement_opening_balance_confirmations_org_id",
        "financial_statement_opening_balance_confirmations",
        ["org_id"],
    )
    if bind.dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER fs_opening_balance_execution_attribution_guard "
            "BEFORE INSERT OR UPDATE ON financial_statement_opening_balance_confirmations "
            "FOR EACH ROW EXECUTE FUNCTION finance_guard_attributed_root_0014()"
        )
        op.execute(
            "CREATE TRIGGER fs_opening_balance_immutable_0007 "
            "BEFORE DELETE OR UPDATE ON financial_statement_opening_balance_confirmations "
            "FOR EACH ROW EXECUTE FUNCTION finance_block_financial_statement_fact_0028()"
        )


def downgrade() -> None:
    op.drop_index(
        "ix_financial_statement_opening_balance_confirmations_org_id",
        table_name="financial_statement_opening_balance_confirmations",
    )
    op.drop_table("financial_statement_opening_balance_confirmations")
