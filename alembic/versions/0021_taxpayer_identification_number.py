"""Require the organization's taxpayer identification number.

Revision ID: 0021_taxpayer_identification
Revises: 0020_salary_deduction_payout
Create Date: 2026-08-25
"""

from __future__ import annotations

import os
import re

import sqlalchemy as sa

from alembic import op

revision = "0021_taxpayer_identification"
down_revision = "0020_salary_deduction_payout"
branch_labels = None
depends_on = None

_TAXPAYER_IDENTIFICATION_NUMBER_ENV = (
    "AI_ACCOUNTING_TAXPAYER_IDENTIFICATION_NUMBER"
)
_ALPHABET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_WEIGHTS = (1, 3, 9, 27, 19, 26, 16, 17, 20, 29, 25, 13, 8, 24, 10, 30, 28)
_PATTERN = re.compile(rf"[{_ALPHABET}]{{18}}")


def _validated_taxpayer_identification_number() -> str:
    configured = os.getenv(_TAXPAYER_IDENTIFICATION_NUMBER_ENV)
    if configured is None:
        raise RuntimeError(
            "0021 requires AI_ACCOUNTING_TAXPAYER_IDENTIFICATION_NUMBER when an "
            "organization already exists"
        )
    normalized = configured.strip().upper()
    if _PATTERN.fullmatch(normalized) is None:
        raise RuntimeError("0021 received an invalid taxpayer identification number")
    weighted_sum = sum(
        _ALPHABET.index(character) * weight
        for character, weight in zip(normalized[:17], _WEIGHTS, strict=True)
    )
    expected = _ALPHABET[(31 - weighted_sum % 31) % 31]
    if normalized[-1] != expected:
        raise RuntimeError("0021 received an invalid taxpayer identification number")
    return normalized


def upgrade() -> None:
    connection = op.get_bind()
    op.add_column(
        "organizations",
        sa.Column("taxpayer_identification_number", sa.String(length=18), nullable=True),
    )

    organization_count = connection.scalar(sa.text("SELECT count(*) FROM organizations"))
    if organization_count:
        if organization_count != 1:
            raise RuntimeError("0021 private-pilot migration requires exactly one organization")
        taxpayer_identification_number = _validated_taxpayer_identification_number()
        connection.execute(
            sa.text(
                "UPDATE organizations SET taxpayer_identification_number = :value "
                "WHERE taxpayer_identification_number IS NULL"
            ),
            {"value": taxpayer_identification_number},
        )
        if connection.dialect.name == "postgresql":
            # Organization updates enqueue existing deferred constraint triggers.
            # PostgreSQL refuses to alter the same table until those events run.
            connection.exec_driver_sql("SET CONSTRAINTS ALL IMMEDIATE")

    with op.batch_alter_table("organizations") as batch:
        batch.alter_column(
            "taxpayer_identification_number",
            existing_type=sa.String(length=18),
            nullable=False,
        )
        batch.create_check_constraint(
            "ck_org_taxpayer_identification_number_length",
            "length(taxpayer_identification_number) = 18",
        )
        batch.create_check_constraint(
            "ck_org_taxpayer_identification_number_uppercase",
            "taxpayer_identification_number = upper(taxpayer_identification_number)",
        )


def downgrade() -> None:
    with op.batch_alter_table("organizations") as batch:
        batch.drop_constraint(
            "ck_org_taxpayer_identification_number_uppercase", type_="check"
        )
        batch.drop_constraint(
            "ck_org_taxpayer_identification_number_length", type_="check"
        )
        batch.drop_column("taxpayer_identification_number")
