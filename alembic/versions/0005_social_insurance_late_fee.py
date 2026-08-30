"""Add a controlled account for evidenced social-insurance late fees.

Revision ID: 0005_social_insurance_late_fee
Revises: 0004_payroll_wage_tax_difference
Create Date: 2026-08-30
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from alembic import op

revision = "0005_social_insurance_late_fee"
down_revision = "0004_payroll_wage_tax_difference"
branch_labels = None
depends_on = None

_CODE = "571103"
_ROLE = "social_insurance_late_fee_expense"


def _tables() -> tuple[sa.TableClause, sa.TableClause, sa.TableClause]:
    organizations = sa.table("organizations", sa.column("id", sa.Uuid()))
    accounts = sa.table(
        "accounts",
        sa.column("id", sa.Uuid()),
        sa.column("org_id", sa.Uuid()),
        sa.column("code", sa.String()),
        sa.column("name", sa.String()),
        sa.column("category", sa.String()),
        sa.column("normal_side", sa.String()),
        sa.column("system_role", sa.String()),
        sa.column("active", sa.Boolean()),
        sa.column("requires_bank_reconciliation", sa.Boolean()),
    )
    voucher_lines = sa.table(
        "voucher_lines",
        sa.column("account_id", sa.Uuid()),
    )
    return organizations, accounts, voucher_lines


def upgrade() -> None:
    bind = op.get_bind()
    organizations, accounts, _ = _tables()
    for org_id in bind.scalars(sa.select(organizations.c.id)).all():
        existing = bind.execute(
            sa.select(accounts.c.code, accounts.c.system_role).where(
                accounts.c.org_id == org_id,
                sa.or_(accounts.c.code == _CODE, accounts.c.system_role == _ROLE),
            )
        ).one_or_none()
        if existing is not None:
            if existing.code == _CODE and existing.system_role == _ROLE:
                continue
            raise RuntimeError("SOCIAL_INSURANCE_LATE_FEE_ACCOUNT_CONFLICT")
        bind.execute(
            accounts.insert().values(
                id=uuid.uuid4(),
                org_id=org_id,
                code=_CODE,
                name="营业外支出—社保滞纳金",
                category="expense",
                normal_side="debit",
                system_role=_ROLE,
                active=True,
                requires_bank_reconciliation=False,
            )
        )


def downgrade() -> None:
    bind = op.get_bind()
    _, accounts, voucher_lines = _tables()
    account_ids = bind.scalars(
        sa.select(accounts.c.id).where(accounts.c.system_role == _ROLE)
    ).all()
    if account_ids and bind.scalar(
        sa.select(sa.func.count())
        .select_from(voucher_lines)
        .where(voucher_lines.c.account_id.in_(account_ids))
    ):
        raise RuntimeError("SOCIAL_INSURANCE_LATE_FEE_ACCOUNT_IN_USE")
    bind.execute(accounts.delete().where(accounts.c.system_role == _ROLE))
