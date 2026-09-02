"""Allow owner workflow completion without inventing declaration dates.

Revision ID: 0021_optional_declaration_dates
Revises: 0020_payroll_accrual_date
Create Date: 2026-09-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0021_optional_declaration_dates"
down_revision = "0020_payroll_accrual_date"
branch_labels = None
depends_on = None

_CONTRIBUTION = "payroll_contribution_assessment_confirmations"
_EXTERNAL = "external_obligation_confirmations"
_HISTORICAL = "historical_obligation_completion_confirmations"

_CONTRIBUTION_APPEND_ONLY_TRIGGER = "contribution_assessment_append_only_guard"
_EXTERNAL_APPEND_ONLY_TRIGGER = "external_obligation_append_only_guard"

_CONTRIBUTION_DATE_STATUS = (
    "declaration_date_status IN ('established','not_established','not_applicable')"
)
_CONTRIBUTION_STATUS_DATES = (
    "(declaration_status = 'declared' AND "
    "((declaration_date_status = 'established' AND declaration_date IS NOT NULL) OR "
    "(declaration_date_status = 'not_established' AND declaration_date IS NULL)) "
    "AND payment_status = 'not_tracked' AND payment_date IS NULL) OR "
    "(declaration_status = 'declared_paid' AND declaration_date_status = 'established' "
    "AND declaration_date IS NOT NULL AND payment_status = 'paid' "
    "AND payment_date IS NOT NULL) OR "
    "(declaration_status = 'declared_unpaid' AND declaration_date_status = 'established' "
    "AND declaration_date IS NOT NULL AND payment_status = 'unpaid' "
    "AND payment_date IS NULL) OR "
    "(declaration_status = 'not_declared' "
    "AND declaration_date_status = 'not_applicable' AND declaration_date IS NULL "
    "AND payment_status = 'not_applicable' AND payment_date IS NULL)"
)
_OLD_CONTRIBUTION_STATUS_DATES = (
    "(declaration_status = 'declared' AND declaration_date IS NOT NULL "
    "AND payment_status = 'not_tracked' AND payment_date IS NULL) OR "
    "(declaration_status = 'declared_paid' AND declaration_date IS NOT NULL "
    "AND payment_status = 'paid' AND payment_date IS NOT NULL) OR "
    "(declaration_status = 'declared_unpaid' AND declaration_date IS NOT NULL "
    "AND payment_status = 'unpaid' AND payment_date IS NULL) OR "
    "(declaration_status = 'not_declared' AND declaration_date IS NULL "
    "AND payment_status = 'not_applicable' AND payment_date IS NULL)"
)
_EXTERNAL_DATE_STATUS = (
    "completion_date_status IN ('established','not_established','not_applicable')"
)
_EXTERNAL_DATE = (
    "(completion_status = 'submitted' AND "
    "((completion_date_status = 'established' AND completion_date IS NOT NULL) OR "
    "(completion_date_status = 'not_established' AND completion_date IS NULL))) OR "
    "(completion_status = 'not_applicable' "
    "AND completion_date_status = 'not_applicable' AND completion_date IS NULL)"
)
_HISTORICAL_CODE = (
    "obligation_code IN ('individual_income_tax','periodic_tax_reporting',"
    "'annual_enterprise_income_tax','annual_business_report')"
)
_OLD_HISTORICAL_CODE = (
    "obligation_code IN ('periodic_tax_reporting',"
    "'annual_enterprise_income_tax','annual_business_report')"
)
_HISTORICAL_SCOPE = (
    "(obligation_code = 'individual_income_tax' AND obligation_scope = 'month') OR "
    "(obligation_code = 'periodic_tax_reporting' AND obligation_scope IN "
    "('month','quarter')) OR "
    "(obligation_code IN ('annual_enterprise_income_tax','annual_business_report') "
    "AND obligation_scope = 'year')"
)
_OLD_HISTORICAL_SCOPE = (
    "(obligation_code = 'periodic_tax_reporting' AND obligation_scope IN "
    "('month','quarter')) OR "
    "(obligation_code IN ('annual_enterprise_income_tax','annual_business_report') "
    "AND obligation_scope = 'year')"
)


def _upgrade_contribution() -> None:
    op.add_column(
        _CONTRIBUTION,
        sa.Column("declaration_date_status", sa.String(length=30), nullable=True),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"ALTER TABLE {_CONTRIBUTION} DISABLE TRIGGER "
            f"{_CONTRIBUTION_APPEND_ONLY_TRIGGER}"
        )
    op.execute(
        sa.text(
            f"UPDATE {_CONTRIBUTION} SET declaration_date_status = CASE "
            "WHEN declaration_date IS NOT NULL THEN 'established' ELSE 'not_applicable' END"
        )
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"ALTER TABLE {_CONTRIBUTION} ENABLE TRIGGER "
            f"{_CONTRIBUTION_APPEND_ONLY_TRIGGER}"
        )
    with op.batch_alter_table(
        _CONTRIBUTION,
        recreate="always" if op.get_bind().dialect.name == "sqlite" else "auto",
    ) as batch_op:
        batch_op.drop_constraint("ck_contribution_assessment_status_dates", type_="check")
        batch_op.alter_column(
            "declaration_date_status",
            existing_type=sa.String(length=30),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_contribution_assessment_declaration_date_status",
            _CONTRIBUTION_DATE_STATUS,
        )
        batch_op.create_check_constraint(
            "ck_contribution_assessment_status_dates",
            _CONTRIBUTION_STATUS_DATES,
        )


def _upgrade_external() -> None:
    op.add_column(
        _EXTERNAL,
        sa.Column("completion_date_status", sa.String(length=30), nullable=True),
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"ALTER TABLE {_EXTERNAL} DISABLE TRIGGER {_EXTERNAL_APPEND_ONLY_TRIGGER}"
        )
    op.execute(
        sa.text(
            f"UPDATE {_EXTERNAL} SET completion_date_status = CASE "
            "WHEN completion_status = 'not_applicable' THEN 'not_applicable' "
            "WHEN completion_date IS NOT NULL THEN 'established' ELSE 'not_established' END"
        )
    )
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            f"ALTER TABLE {_EXTERNAL} ENABLE TRIGGER {_EXTERNAL_APPEND_ONLY_TRIGGER}"
        )
    with op.batch_alter_table(
        _EXTERNAL,
        recreate="always" if op.get_bind().dialect.name == "sqlite" else "auto",
    ) as batch_op:
        batch_op.alter_column(
            "completion_date",
            existing_type=sa.Date(),
            nullable=True,
        )
        batch_op.alter_column(
            "completion_date_status",
            existing_type=sa.String(length=30),
            nullable=False,
        )
        batch_op.create_check_constraint(
            "ck_external_obligation_confirmation_date_status",
            _EXTERNAL_DATE_STATUS,
        )
        batch_op.create_check_constraint(
            "ck_external_obligation_confirmation_date",
            _EXTERNAL_DATE,
        )


def _replace_historical_constraints(*, code: str, scope: str) -> None:
    with op.batch_alter_table(
        _HISTORICAL,
        recreate="always" if op.get_bind().dialect.name == "sqlite" else "auto",
    ) as batch_op:
        batch_op.drop_constraint("ck_historical_obligation_completion_code", type_="check")
        batch_op.drop_constraint("ck_historical_obligation_completion_scope", type_="check")
        batch_op.create_check_constraint("ck_historical_obligation_completion_code", code)
        batch_op.create_check_constraint("ck_historical_obligation_completion_scope", scope)


def upgrade() -> None:
    _upgrade_contribution()
    _upgrade_external()
    _replace_historical_constraints(code=_HISTORICAL_CODE, scope=_HISTORICAL_SCOPE)


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(
        sa.text(
            f"SELECT COUNT(*) FROM {_CONTRIBUTION} "
            "WHERE declaration_status IN ('declared','declared_paid','declared_unpaid') "
            "AND declaration_date IS NULL"
        )
    ):
        raise RuntimeError("cannot downgrade 0021 with unknown contribution declaration dates")
    if connection.scalar(
        sa.text(
            f"SELECT COUNT(*) FROM {_EXTERNAL} "
            "WHERE completion_status = 'submitted' AND completion_date IS NULL"
        )
    ):
        raise RuntimeError("cannot downgrade 0021 with unknown external completion dates")
    if connection.scalar(
        sa.text(
            f"SELECT COUNT(*) FROM {_HISTORICAL} "
            "WHERE obligation_code = 'individual_income_tax'"
        )
    ):
        raise RuntimeError("cannot downgrade 0021 with historical IIT confirmations")

    _replace_historical_constraints(code=_OLD_HISTORICAL_CODE, scope=_OLD_HISTORICAL_SCOPE)
    with op.batch_alter_table(
        _EXTERNAL,
        recreate="always" if connection.dialect.name == "sqlite" else "auto",
    ) as batch_op:
        batch_op.drop_constraint("ck_external_obligation_confirmation_date", type_="check")
        batch_op.drop_constraint(
            "ck_external_obligation_confirmation_date_status", type_="check"
        )
        batch_op.alter_column("completion_date", existing_type=sa.Date(), nullable=False)
        batch_op.drop_column("completion_date_status")
    with op.batch_alter_table(
        _CONTRIBUTION,
        recreate="always" if connection.dialect.name == "sqlite" else "auto",
    ) as batch_op:
        batch_op.drop_constraint("ck_contribution_assessment_status_dates", type_="check")
        batch_op.drop_constraint(
            "ck_contribution_assessment_declaration_date_status", type_="check"
        )
        batch_op.create_check_constraint(
            "ck_contribution_assessment_status_dates",
            _OLD_CONTRIBUTION_STATUS_DATES,
        )
        batch_op.drop_column("declaration_date_status")
