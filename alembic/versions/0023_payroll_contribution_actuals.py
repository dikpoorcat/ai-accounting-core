"""Add evidenced per-kind contribution actuals and historical supplements.

Revision ID: 0023_payroll_contrib_actuals
Revises: 0022_bank_recon_multi_match
Create Date: 2026-08-25
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0023_payroll_contrib_actuals"
down_revision = "0022_bank_recon_multi_match"
branch_labels = None
depends_on = None


_OLD_SPECIALIZED_EVENTS = (
    "                'borrowing_principal_repayment','labor_remuneration_accrual',\n"
    "                'unified_payout_run','labor_withholding_tax_payment'"
)
_NEW_SPECIALIZED_EVENTS = (
    "                'borrowing_principal_repayment','labor_remuneration_accrual',\n"
    "                'unified_payout_run','labor_withholding_tax_payment',\n"
    "                'payroll_contribution_supplement'"
)


def _replace_final_event_wrapper(*, upgrade: bool) -> None:
    connection = op.get_bind()
    definition = connection.scalar(
        sa.text(
            "SELECT pg_get_functiondef("
            "'finance_assert_final_business_event_0014(uuid)'::regprocedure)"
        )
    )
    if not isinstance(definition, str):
        raise RuntimeError("required PostgreSQL final-event wrapper is missing")
    old = _OLD_SPECIALIZED_EVENTS if upgrade else _NEW_SPECIALIZED_EVENTS
    new = _NEW_SPECIALIZED_EVENTS if upgrade else _OLD_SPECIALIZED_EVENTS
    if definition.count(old) != 1:
        raise RuntimeError("unexpected PostgreSQL final-event wrapper shape")
    connection.exec_driver_sql(definition.replace(old, new).replace("%", "%%"))


def upgrade() -> None:
    op.create_table(
        "payroll_contribution_actual_sets",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("contribution_period", sa.String(length=7), nullable=False),
        sa.Column("declaration_date", sa.Date(), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=False),
        sa.Column("reason_description", sa.Text(), nullable=False),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(contribution_period) = 7 AND substr(contribution_period, 5, 1) = '-' "
            "AND substr(contribution_period, 6, 2) BETWEEN '01' AND '12'",
            name="ck_contribution_actual_set_period",
        ),
        sa.CheckConstraint(
            "reason_code IN ('late_enrollment','missing_declaration','partial_declaration',"
            "'agency_assessment','documented_correction','other_documented')",
            name="ck_contribution_actual_set_reason",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_contribution_actual_set_org_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name="fk_contribution_actual_set_execution_attribution",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_contribution_actual_set_org_id"),
        sa.UniqueConstraint(
            "org_id", "idempotency_key", name="uq_contribution_actual_set_idempotency"
        ),
    )
    op.create_index(
        "ix_payroll_contribution_actual_sets_org_id",
        "payroll_contribution_actual_sets",
        ["org_id"],
    )
    op.create_index(
        "ix_payroll_contribution_actual_sets_employee_id",
        "payroll_contribution_actual_sets",
        ["employee_id"],
    )
    op.create_index(
        "ix_payroll_contribution_actual_sets_contribution_period",
        "payroll_contribution_actual_sets",
        ["contribution_period"],
    )

    op.create_table(
        "payroll_contribution_actual_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("actual_set_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("contribution_period", sa.String(length=7), nullable=False),
        sa.Column("contribution_group", sa.String(length=30), nullable=False),
        sa.Column("insurance_kind", sa.String(length=50), nullable=False),
        sa.Column("actual_state", sa.String(length=20), nullable=False),
        sa.Column("employee_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("employer_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "contribution_group IN ('social_insurance','housing_fund')",
            name="ck_contribution_actual_item_group",
        ),
        sa.CheckConstraint(
            "actual_state IN ('declared','not_declared')",
            name="ck_contribution_actual_item_state",
        ),
        sa.CheckConstraint(
            "employee_amount_fen >= 0 AND employer_amount_fen >= 0",
            name="ck_contribution_actual_item_amounts",
        ),
        sa.CheckConstraint(
            "actual_state <> 'not_declared' OR "
            "(employee_amount_fen = 0 AND employer_amount_fen = 0)",
            name="ck_contribution_actual_item_non_declaration_zero",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "actual_set_id"],
            ["payroll_contribution_actual_sets.org_id", "payroll_contribution_actual_sets.id"],
            name="fk_contribution_actual_item_org_set",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_contribution_actual_item_org_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "supersedes_id"],
            ["payroll_contribution_actual_items.org_id", "payroll_contribution_actual_items.id"],
            name="fk_contribution_actual_item_org_supersedes",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_contribution_actual_item_org_id"),
        sa.UniqueConstraint("supersedes_id"),
        sa.UniqueConstraint(
            "actual_set_id",
            "contribution_group",
            "insurance_kind",
            name="uq_contribution_actual_item_set_kind",
        ),
    )
    op.create_index(
        "ix_payroll_contribution_actual_items_org_id",
        "payroll_contribution_actual_items",
        ["org_id"],
    )
    op.create_index(
        "ix_payroll_contribution_actual_items_employee_id",
        "payroll_contribution_actual_items",
        ["employee_id"],
    )
    op.create_index(
        "ix_payroll_contribution_actual_items_actual_set_id",
        "payroll_contribution_actual_items",
        ["actual_set_id"],
    )
    op.create_index(
        "ix_payroll_contribution_actual_items_contribution_period",
        "payroll_contribution_actual_items",
        ["contribution_period"],
    )
    op.create_index(
        "uq_contribution_actual_root_kind",
        "payroll_contribution_actual_items",
        [
            "org_id",
            "employee_id",
            "contribution_period",
            "contribution_group",
            "insurance_kind",
        ],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
        sqlite_where=sa.text("supersedes_id IS NULL"),
    )

    op.create_table(
        "payroll_contribution_actual_evidence",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("actual_set_id", sa.Uuid(), nullable=False),
        sa.Column("evidence_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "actual_set_id"],
            ["payroll_contribution_actual_sets.org_id", "payroll_contribution_actual_sets.id"],
            name="fk_contribution_actual_evidence_org_set",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "evidence_id"],
            ["evidence.org_id", "evidence.id"],
            name="fk_contribution_actual_evidence_org_evidence",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "actual_set_id", "evidence_id"),
    )

    op.create_table(
        "payroll_contribution_actual_uses",
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("actual_item_id", sa.Uuid(), nullable=False),
        sa.Column("payroll_batch_id", sa.Uuid(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "actual_item_id"],
            ["payroll_contribution_actual_items.org_id", "payroll_contribution_actual_items.id"],
            name="fk_contribution_actual_use_org_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_contribution_actual_use_org_batch",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("org_id", "actual_item_id", "payroll_batch_id"),
    )

    op.create_table(
        "payroll_contribution_supplements",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("event_id", sa.Uuid(), nullable=False),
        sa.Column("employee_id", sa.Uuid(), nullable=False),
        sa.Column("source_payroll_batch_id", sa.Uuid(), nullable=False),
        sa.Column("contribution_period", sa.String(length=7), nullable=False),
        sa.Column("assessment_reference", sa.String(length=200), nullable=False),
        sa.Column("reason_code", sa.String(length=40), nullable=False),
        sa.Column("reason_description", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "length(contribution_period) = 7 AND substr(contribution_period, 5, 1) = '-' "
            "AND substr(contribution_period, 6, 2) BETWEEN '01' AND '12'",
            name="ck_contribution_supplement_period",
        ),
        sa.CheckConstraint(
            "reason_code IN ('late_enrollment','missing_declaration','agency_assessment',"
            "'documented_correction','other_documented')",
            name="ck_contribution_supplement_reason",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "event_id"],
            ["business_events.org_id", "business_events.id"],
            name="fk_contribution_supplement_org_event",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "employee_id"],
            ["employees.org_id", "employees.id"],
            name="fk_contribution_supplement_org_employee",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "source_payroll_batch_id"],
            ["payroll_batches.org_id", "payroll_batches.id"],
            name="fk_contribution_supplement_org_source_batch",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("event_id"),
        sa.UniqueConstraint("org_id", "id", name="uq_contribution_supplement_org_id"),
        sa.UniqueConstraint(
            "org_id",
            "employee_id",
            "assessment_reference",
            name="uq_contribution_supplement_assessment",
        ),
    )
    op.create_index(
        "ix_payroll_contribution_supplements_org_id",
        "payroll_contribution_supplements",
        ["org_id"],
    )
    op.create_index(
        "ix_payroll_contribution_supplements_employee_id",
        "payroll_contribution_supplements",
        ["employee_id"],
    )
    op.create_index(
        "ix_payroll_contribution_supplements_contribution_period",
        "payroll_contribution_supplements",
        ["contribution_period"],
    )
    op.create_index(
        "ix_payroll_contribution_supplements_source_payroll_batch_id",
        "payroll_contribution_supplements",
        ["source_payroll_batch_id"],
    )

    with op.batch_alter_table("payroll_event_links") as batch:
        batch.drop_constraint("ck_payroll_event_link_kind", type_="check")
        batch.create_check_constraint(
            "ck_payroll_event_link_kind",
            "link_kind IN ('payroll_accrual','salary_payment','contribution_supplement',"
            "'statutory_payment','reversal')",
        )

    op.create_table(
        "payroll_contribution_supplement_items",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("supplement_id", sa.Uuid(), nullable=False),
        sa.Column("contribution_group", sa.String(length=30), nullable=False),
        sa.Column("insurance_kind", sa.String(length=50), nullable=False),
        sa.Column("employee_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("employer_amount_fen", sa.BigInteger(), nullable=False),
        sa.Column("employee_amount_treatment", sa.String(length=30), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "contribution_group IN ('social_insurance','housing_fund')",
            name="ck_contribution_supplement_item_group",
        ),
        sa.CheckConstraint(
            "employee_amount_fen >= 0 AND employer_amount_fen >= 0 "
            "AND employee_amount_fen + employer_amount_fen > 0",
            name="ck_contribution_supplement_item_amounts",
        ),
        sa.CheckConstraint(
            "employee_amount_treatment IN ('employer_borne','employee_receivable')",
            name="ck_contribution_supplement_item_treatment",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "supplement_id"],
            ["payroll_contribution_supplements.org_id", "payroll_contribution_supplements.id"],
            name="fk_contribution_supplement_item_org_supplement",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("org_id", "id", name="uq_contribution_supplement_item_org_id"),
        sa.UniqueConstraint(
            "supplement_id",
            "contribution_group",
            "insurance_kind",
            name="uq_contribution_supplement_item_kind",
        ),
    )
    op.create_index(
        "ix_payroll_contribution_supplement_items_org_id",
        "payroll_contribution_supplement_items",
        ["org_id"],
    )
    op.create_index(
        "ix_payroll_contribution_supplement_items_supplement_id",
        "payroll_contribution_supplement_items",
        ["supplement_id"],
    )

    if op.get_bind().dialect.name == "postgresql":
        _replace_final_event_wrapper(upgrade=True)
        op.execute(
            """
            CREATE FUNCTION finance_payroll_contribution_fact_immutable_0023()
            RETURNS trigger
            LANGUAGE plpgsql
            AS $$
            BEGIN
                RAISE EXCEPTION 'payroll contribution actual and supplement facts are immutable';
            END;
            $$
            """
        )
        for table_name in (
            "payroll_contribution_actual_sets",
            "payroll_contribution_actual_items",
            "payroll_contribution_actual_evidence",
            "payroll_contribution_actual_uses",
            "payroll_contribution_supplements",
            "payroll_contribution_supplement_items",
        ):
            op.execute(
                f"CREATE TRIGGER {table_name}_immutable_0023 "
                f"BEFORE UPDATE OR DELETE ON {table_name} FOR EACH ROW "
                "EXECUTE FUNCTION finance_payroll_contribution_fact_immutable_0023()"
            )


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        for table_name in (
            "payroll_contribution_supplement_items",
            "payroll_contribution_supplements",
            "payroll_contribution_actual_uses",
            "payroll_contribution_actual_evidence",
            "payroll_contribution_actual_items",
            "payroll_contribution_actual_sets",
        ):
            op.execute(f"DROP TRIGGER IF EXISTS {table_name}_immutable_0023 ON {table_name}")
        op.execute("DROP FUNCTION finance_payroll_contribution_fact_immutable_0023()")
        _replace_final_event_wrapper(upgrade=False)
    with op.batch_alter_table("payroll_event_links") as batch:
        batch.drop_constraint("ck_payroll_event_link_kind", type_="check")
        batch.create_check_constraint(
            "ck_payroll_event_link_kind",
            "link_kind IN ('payroll_accrual','salary_payment','statutory_payment','reversal')",
        )
    op.drop_index(
        "ix_payroll_contribution_supplement_items_supplement_id",
        table_name="payroll_contribution_supplement_items",
    )
    op.drop_index(
        "ix_payroll_contribution_supplement_items_org_id",
        table_name="payroll_contribution_supplement_items",
    )
    op.drop_table("payroll_contribution_supplement_items")
    op.drop_index(
        "ix_payroll_contribution_supplements_source_payroll_batch_id",
        table_name="payroll_contribution_supplements",
    )
    op.drop_index(
        "ix_payroll_contribution_supplements_contribution_period",
        table_name="payroll_contribution_supplements",
    )
    op.drop_index(
        "ix_payroll_contribution_supplements_employee_id",
        table_name="payroll_contribution_supplements",
    )
    op.drop_index(
        "ix_payroll_contribution_supplements_org_id",
        table_name="payroll_contribution_supplements",
    )
    op.drop_table("payroll_contribution_supplements")
    op.drop_table("payroll_contribution_actual_uses")
    op.drop_table("payroll_contribution_actual_evidence")
    op.drop_index(
        "uq_contribution_actual_root_kind",
        table_name="payroll_contribution_actual_items",
    )
    op.drop_index(
        "ix_payroll_contribution_actual_items_contribution_period",
        table_name="payroll_contribution_actual_items",
    )
    op.drop_index(
        "ix_payroll_contribution_actual_items_actual_set_id",
        table_name="payroll_contribution_actual_items",
    )
    op.drop_index(
        "ix_payroll_contribution_actual_items_employee_id",
        table_name="payroll_contribution_actual_items",
    )
    op.drop_index(
        "ix_payroll_contribution_actual_items_org_id",
        table_name="payroll_contribution_actual_items",
    )
    op.drop_table("payroll_contribution_actual_items")
    op.drop_index(
        "ix_payroll_contribution_actual_sets_contribution_period",
        table_name="payroll_contribution_actual_sets",
    )
    op.drop_index(
        "ix_payroll_contribution_actual_sets_employee_id",
        table_name="payroll_contribution_actual_sets",
    )
    op.drop_index(
        "ix_payroll_contribution_actual_sets_org_id",
        table_name="payroll_contribution_actual_sets",
    )
    op.drop_table("payroll_contribution_actual_sets")
