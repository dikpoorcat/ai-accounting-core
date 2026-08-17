"""Require password-reauthenticated owner approval for period close.

Revision ID: 0003_owner_close_approval
Revises: 0002_pilot_events
Create Date: 2026-08-17
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0003_owner_close_approval"
down_revision = "0002_pilot_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "accounting_period_close_approvals",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("period_id", sa.Uuid(), nullable=False),
        sa.Column("owner_account_id", sa.Uuid(), nullable=False),
        sa.Column("owner_session_id", sa.Uuid(), nullable=False),
        sa.Column("owner_credential_version", sa.Integer(), nullable=False),
        sa.Column("calculation_hash", sa.String(length=64), nullable=False),
        sa.Column("confirmation_method", sa.String(length=30), nullable=False),
        sa.Column("confirmed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "owner_credential_version >= 1",
            name="ck_period_close_approval_credential_version",
        ),
        sa.CheckConstraint(
            "length(calculation_hash) = 64",
            name="ck_period_close_approval_hash_length",
        ),
        sa.CheckConstraint(
            "confirmation_method = 'local_password_reauthentication'",
            name="ck_period_close_approval_method",
        ),
        sa.CheckConstraint(
            "expires_at > confirmed_at",
            name="ck_period_close_approval_expiry",
        ),
        sa.CheckConstraint(
            "consumed_at IS NULL OR consumed_at >= confirmed_at",
            name="ck_period_close_approval_consumed_at",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_period_close_approval_org_period",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "owner_account_id"],
            ["owner_accounts.org_id", "owner_accounts.id"],
            name="fk_period_close_approval_org_owner",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_period_close_approval_org_id"),
    )
    op.create_index(
        op.f("ix_accounting_period_close_approvals_org_id"),
        "accounting_period_close_approvals",
        ["org_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_accounting_period_close_approvals_period_id"),
        "accounting_period_close_approvals",
        ["period_id"],
        unique=False,
    )
    with op.batch_alter_table("accounting_period_closes") as batch_op:
        batch_op.add_column(sa.Column("owner_approval_id", sa.Uuid(), nullable=True))
        batch_op.create_unique_constraint(
            "uq_accounting_period_close_owner_approval",
            ["owner_approval_id"],
        )
        batch_op.create_foreign_key(
            "fk_accounting_period_close_org_owner_approval",
            "accounting_period_close_approvals",
            ["org_id", "owner_approval_id"],
            ["org_id", "id"],
            ondelete="RESTRICT",
        )


def downgrade() -> None:
    with op.batch_alter_table("accounting_period_closes") as batch_op:
        batch_op.drop_constraint(
            "fk_accounting_period_close_org_owner_approval",
            type_="foreignkey",
        )
        batch_op.drop_constraint(
            "uq_accounting_period_close_owner_approval",
            type_="unique",
        )
        batch_op.drop_column("owner_approval_id")
    op.drop_index(
        op.f("ix_accounting_period_close_approvals_period_id"),
        table_name="accounting_period_close_approvals",
    )
    op.drop_index(
        op.f("ix_accounting_period_close_approvals_org_id"),
        table_name="accounting_period_close_approvals",
    )
    op.drop_table("accounting_period_close_approvals")
