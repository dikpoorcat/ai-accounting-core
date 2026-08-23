"""Separate payroll identity from actual company contribution participation.

Revision ID: 0018_payroll_participation
Revises: 0017_payroll_reported_salary
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0018_payroll_participation"
down_revision = "0017_payroll_reported_salary"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "employee_payroll_profile_versions",
        sa.Column(
            "social_insurance_participating",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )
    op.add_column(
        "employee_payroll_profile_versions",
        sa.Column(
            "housing_fund_participating",
            sa.Boolean(),
            nullable=False,
            server_default=sa.true(),
        ),
    )


def downgrade() -> None:
    op.drop_column(
        "employee_payroll_profile_versions", "housing_fund_participating"
    )
    op.drop_column(
        "employee_payroll_profile_versions", "social_insurance_participating"
    )
