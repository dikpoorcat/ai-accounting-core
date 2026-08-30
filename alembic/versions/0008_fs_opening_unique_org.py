"""Allow exactly one financial-statement opening confirmation per company.

Revision ID: 0008_fs_opening_unique_org
Revises: 0007_fs_opening_balance
Create Date: 2026-08-30
"""

from __future__ import annotations

from alembic import op

revision = "0008_fs_opening_unique_org"
down_revision = "0007_fs_opening_balance"
branch_labels = None
depends_on = None


def _replace_unique(*, old_name: str, new_name: str, new_columns: list[str]) -> None:
    if op.get_bind().dialect.name == "sqlite":
        with op.batch_alter_table(
            "financial_statement_opening_balance_confirmations",
            recreate="always",
        ) as batch_op:
            batch_op.drop_constraint(old_name, type_="unique")
            batch_op.create_unique_constraint(new_name, new_columns)
        return
    op.drop_constraint(
        old_name,
        "financial_statement_opening_balance_confirmations",
        type_="unique",
    )
    op.create_unique_constraint(
        new_name,
        "financial_statement_opening_balance_confirmations",
        new_columns,
    )


def upgrade() -> None:
    _replace_unique(
        old_name="uq_fs_opening_confirmation_date",
        new_name="uq_fs_opening_confirmation_org",
        new_columns=["org_id"],
    )


def downgrade() -> None:
    _replace_unique(
        old_name="uq_fs_opening_confirmation_org",
        new_name="uq_fs_opening_confirmation_date",
        new_columns=["org_id", "establishment_date"],
    )
