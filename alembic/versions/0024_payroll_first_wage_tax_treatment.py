"""Add evidenced employee-year first-wage tax treatment.

Revision ID: 0024_first_wage_tax
Revises: 0023_payroll_contrib_actuals
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0024_first_wage_tax"
down_revision = "0023_payroll_contrib_actuals"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "payroll_first_wage_tax_treatments",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("tax_year", sa.Integer(), nullable=False),
        sa.Column("first_wage_month", sa.Integer(), nullable=False),
        sa.Column("treatment_state", sa.String(length=20), nullable=False),
        sa.Column("declaration_date", sa.Date(), nullable=False),
        sa.Column("confirmation_description", sa.Text(), nullable=False),
        sa.Column("legal_basis_url", sa.String(length=1000), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "tax_year BETWEEN 1900 AND 9999", name="ck_first_wage_treatment_year"
        ),
        sa.CheckConstraint(
            "first_wage_month BETWEEN 1 AND 12", name="ck_first_wage_treatment_month"
        ),
        sa.CheckConstraint(
            "treatment_state IN ('eligible','not_eligible')",
            name="ck_first_wage_treatment_state",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_first_wage_treatment_org_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_first_wage_treatment_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "supersedes_id"],
            ["payroll_first_wage_tax_treatments.org_id", "payroll_first_wage_tax_treatments.id"],
            name="fk_first_wage_treatment_org_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_first_wage_treatment_org_id"),
        sa.UniqueConstraint(
            "org_id", "idempotency_key", name="uq_first_wage_treatment_idempotency"
        ),
        sa.UniqueConstraint("supersedes_id"),
    )
    op.create_index(
        "ix_payroll_first_wage_tax_treatments_org_id",
        "payroll_first_wage_tax_treatments",
        ["org_id"],
    )
    op.create_index(
        "ix_payroll_first_wage_tax_treatments_employee_id",
        "payroll_first_wage_tax_treatments",
        ["employee_id"],
    )
    op.create_index(
        "ix_payroll_first_wage_tax_treatments_tax_year",
        "payroll_first_wage_tax_treatments",
        ["tax_year"],
    )
    op.create_index(
        "uq_first_wage_treatment_root",
        "payroll_first_wage_tax_treatments",
        ["org_id", "employee_id", "tax_year"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
        sqlite_where=sa.text("supersedes_id IS NULL"),
    )

    op.create_table(
        "payroll_first_wage_tax_treatment_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("treatment_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "treatment_id"],
            ["payroll_first_wage_tax_treatments.org_id", "payroll_first_wage_tax_treatments.id"],
            name="fk_first_wage_treatment_evidence_org_treatment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_first_wage_treatment_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "treatment_id", "evidence_id"),
    )

    op.create_table(
        "payroll_first_wage_tax_treatment_uses",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("treatment_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_batch_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "treatment_id"],
            ["payroll_first_wage_tax_treatments.org_id", "payroll_first_wage_tax_treatments.id"],
            name="fk_first_wage_treatment_use_org_treatment",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_first_wage_treatment_use_org_batch",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "treatment_id", "payroll_batch_id"),
    )

    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            """
            CREATE FUNCTION finance_first_wage_tax_fact_immutable_0024()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'payroll first-wage tax treatment facts are immutable';
            END;
            $$
            """
        )
        for table_name in (
            "payroll_first_wage_tax_treatments",
            "payroll_first_wage_tax_treatment_evidence",
            "payroll_first_wage_tax_treatment_uses",
        ):
            op.execute(
                f"CREATE TRIGGER {table_name}_immutable_0024 "
                f"BEFORE UPDATE OR DELETE ON {table_name} FOR EACH ROW "
                "EXECUTE FUNCTION finance_first_wage_tax_fact_immutable_0024()"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table_name in (
            "payroll_first_wage_tax_treatment_uses",
            "payroll_first_wage_tax_treatment_evidence",
            "payroll_first_wage_tax_treatments",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {table_name}_immutable_0024 ON {table_name}")
        op.execute("DROP FUNCTION finance_first_wage_tax_fact_immutable_0024()")
    op.drop_table("payroll_first_wage_tax_treatment_uses")
    op.drop_table("payroll_first_wage_tax_treatment_evidence")
    op.drop_index(
        "uq_first_wage_treatment_root", table_name="payroll_first_wage_tax_treatments"
    )
    op.drop_index(
        "ix_payroll_first_wage_tax_treatments_tax_year",
        table_name="payroll_first_wage_tax_treatments",
    )
    op.drop_index(
        "ix_payroll_first_wage_tax_treatments_employee_id",
        table_name="payroll_first_wage_tax_treatments",
    )
    op.drop_index(
        "ix_payroll_first_wage_tax_treatments_org_id",
        table_name="payroll_first_wage_tax_treatments",
    )
    op.drop_table("payroll_first_wage_tax_treatments")
