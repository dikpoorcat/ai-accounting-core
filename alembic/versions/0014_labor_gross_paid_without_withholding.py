"""Add an auditable gross-paid-without-withholding labor settlement mode.

Revision ID: 0014_labor_gross_unwithheld
Revises: 0013_labor_remuneration
Create Date: 2026-08-21
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_labor_gross_unwithheld"
down_revision = "0013_labor_remuneration"
branch_labels = None
depends_on = None


_EVIDENCE_INVARIANT = """
    IF (
        EXISTS (
            SELECT 1 FROM unified_payout_run_items AS run_item
             WHERE run_item.org_id = target.org_id
               AND run_item.payout_run_id = target.id
               AND run_item.settlement_mode = 'gross_paid_without_withholding'
        ) AND (
            jsonb_typeof(target.calculation_input::jsonb
                         #> '{request,withholding_exception_evidence_references}')
                IS DISTINCT FROM 'array'
            OR jsonb_array_length(target.calculation_input::jsonb
                                  #> '{request,withholding_exception_evidence_references}') = 0
            OR EXISTS (
                SELECT 1
                  FROM jsonb_array_elements_text(
                       target.calculation_input::jsonb
                       #> '{request,withholding_exception_evidence_references}'
                  ) AS exception_evidence(evidence_id)
                 WHERE NOT EXISTS (
                    SELECT 1 FROM unified_payout_run_evidence AS run_evidence
                     WHERE run_evidence.org_id = target.org_id
                       AND run_evidence.payout_run_id = target.id
                       AND run_evidence.evidence_id::text = exception_evidence.evidence_id
                 )
            )
        )
    ) OR (
        NOT EXISTS (
            SELECT 1 FROM unified_payout_run_items AS run_item
             WHERE run_item.org_id = target.org_id
               AND run_item.payout_run_id = target.id
               AND run_item.settlement_mode = 'gross_paid_without_withholding'
        ) AND coalesce(jsonb_array_length(
            target.calculation_input::jsonb
            #> '{request,withholding_exception_evidence_references}'
        ), 0) <> 0
    ) THEN
        RAISE EXCEPTION 'UNIFIED_PAYOUT_WITHHOLDING_EXCEPTION_EVIDENCE_MISMATCH';
    END IF;
"""


def _replace_postgresql_function(
    regprocedure: str, replacements: tuple[tuple[str, str], ...]
) -> None:
    connection = op.get_bind()
    definition = connection.scalar(
        sa.text("SELECT pg_get_functiondef(CAST(:identity AS regprocedure))"),
        {"identity": regprocedure},
    )
    if not isinstance(definition, str):
        raise RuntimeError(f"required PostgreSQL function is missing: {regprocedure}")
    for old, new in replacements:
        if definition.count(old) != 1:
            raise RuntimeError(f"unexpected PostgreSQL function shape for {regprocedure}: {old}")
        definition = definition.replace(old, new)
    connection.exec_driver_sql(definition.replace("%", "%%"))


def _new_source_kind_check() -> str:
    return (
        "(item_kind = 'salary' AND payroll_line_id IS NOT NULL AND labor_line_id IS NULL "
        "AND settlement_mode = 'not_applicable') OR "
        "(item_kind = 'labor' AND payroll_line_id IS NULL AND labor_line_id IS NOT NULL "
        "AND settlement_mode IN "
        "('net_after_withholding','gross_paid_without_withholding'))"
    )


def _new_totals_check() -> str:
    return (
        "gross_amount_fen > 0 AND employee_social_insurance_fen >= 0 "
        "AND employee_housing_fund_fen >= 0 AND individual_income_tax_fen >= 0 "
        "AND theoretical_individual_income_tax_fen >= individual_income_tax_fen "
        "AND unwithheld_individual_income_tax_fen = "
        "theoretical_individual_income_tax_fen - individual_income_tax_fen "
        "AND net_amount_fen = gross_amount_fen - employee_social_insurance_fen "
        "- employee_housing_fund_fen - individual_income_tax_fen "
        "AND net_amount_fen >= 0"
    )


def _settlement_mode_check() -> str:
    return (
        "(item_kind = 'salary' AND unwithheld_individual_income_tax_fen = 0) OR "
        "(item_kind = 'labor' AND settlement_mode = 'net_after_withholding' "
        "AND individual_income_tax_fen = theoretical_individual_income_tax_fen "
        "AND unwithheld_individual_income_tax_fen = 0) OR "
        "(item_kind = 'labor' AND settlement_mode = 'gross_paid_without_withholding' "
        "AND individual_income_tax_fen = 0 "
        "AND unwithheld_individual_income_tax_fen = "
        "theoretical_individual_income_tax_fen)"
    )


def _old_source_kind_check() -> str:
    return (
        "(item_kind = 'salary' AND payroll_line_id IS NOT NULL AND labor_line_id IS NULL) OR "
        "(item_kind = 'labor' AND payroll_line_id IS NULL AND labor_line_id IS NOT NULL)"
    )


def _old_totals_check() -> str:
    return (
        "gross_amount_fen > 0 AND employee_social_insurance_fen >= 0 "
        "AND employee_housing_fund_fen >= 0 AND individual_income_tax_fen >= 0 "
        "AND net_amount_fen = gross_amount_fen - employee_social_insurance_fen "
        "- employee_housing_fund_fen - individual_income_tax_fen "
        "AND net_amount_fen >= 0"
    )


def _drop_item_guard_if_postgresql() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "DROP TRIGGER unified_payout_run_items_immutability_guard ON unified_payout_run_items"
        )


def _create_item_guard_if_postgresql() -> None:
    if op.get_bind().dialect.name == "postgresql":
        op.execute(
            "CREATE TRIGGER unified_payout_run_items_immutability_guard "
            "BEFORE UPDATE OR DELETE ON unified_payout_run_items FOR EACH ROW "
            "EXECUTE FUNCTION finance_block_final_labor_graph_0013()"
        )


def upgrade() -> None:
    _drop_item_guard_if_postgresql()
    with op.batch_alter_table("unified_payout_run_items") as batch:
        batch.add_column(sa.Column("settlement_mode", sa.String(50), nullable=True))
        batch.add_column(
            sa.Column("theoretical_individual_income_tax_fen", sa.BigInteger(), nullable=True)
        )
        batch.add_column(
            sa.Column("unwithheld_individual_income_tax_fen", sa.BigInteger(), nullable=True)
        )
    op.execute(
        "UPDATE unified_payout_run_items "
        "SET settlement_mode = CASE WHEN item_kind = 'salary' THEN 'not_applicable' "
        "ELSE 'net_after_withholding' END, "
        "theoretical_individual_income_tax_fen = individual_income_tax_fen, "
        "unwithheld_individual_income_tax_fen = 0"
    )
    with op.batch_alter_table("unified_payout_run_items") as batch:
        batch.drop_constraint("ck_payout_item_source_kind", type_="check")
        batch.drop_constraint("ck_payout_item_totals", type_="check")
        batch.alter_column("settlement_mode", existing_type=sa.String(50), nullable=False)
        batch.alter_column(
            "theoretical_individual_income_tax_fen",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
        batch.alter_column(
            "unwithheld_individual_income_tax_fen",
            existing_type=sa.BigInteger(),
            nullable=False,
        )
        batch.create_check_constraint("ck_payout_item_source_kind", _new_source_kind_check())
        batch.create_check_constraint("ck_payout_item_totals", _new_totals_check())
        batch.create_check_constraint("ck_payout_item_settlement_mode", _settlement_mode_check())
    _create_item_guard_if_postgresql()

    if op.get_bind().dialect.name != "postgresql":
        return
    old_hash_end = "        RAISE EXCEPTION 'UNIFIED_PAYOUT_HASH_MISMATCH';\n    END IF;"
    _replace_postgresql_function(
        "finance_assert_unified_payout_0013(uuid)",
        (
            (
                "entitlement.amount_fen = run_item.individual_income_tax_fen",
                "entitlement.amount_fen = run_item.theoretical_individual_income_tax_fen",
            ),
            (old_hash_end, old_hash_end + "\n" + _EVIDENCE_INVARIANT.rstrip()),
        ),
    )


def downgrade() -> None:
    gross_count = int(
        op.get_bind().scalar(
            sa.text(
                "SELECT count(*) FROM unified_payout_run_items "
                "WHERE settlement_mode = 'gross_paid_without_withholding'"
            )
        )
        or 0
    )
    if gross_count:
        raise RuntimeError("LABOR_GROSS_UNWITHHELD_DOWNGRADE_UNSAFE")

    if op.get_bind().dialect.name == "postgresql":
        old_hash_end = "        RAISE EXCEPTION 'UNIFIED_PAYOUT_HASH_MISMATCH';\n    END IF;"
        _replace_postgresql_function(
            "finance_assert_unified_payout_0013(uuid)",
            (
                (
                    "entitlement.amount_fen = run_item.theoretical_individual_income_tax_fen",
                    "entitlement.amount_fen = run_item.individual_income_tax_fen",
                ),
                (old_hash_end + "\n" + _EVIDENCE_INVARIANT.rstrip(), old_hash_end),
            ),
        )

    _drop_item_guard_if_postgresql()
    with op.batch_alter_table("unified_payout_run_items") as batch:
        batch.drop_constraint("ck_payout_item_settlement_mode", type_="check")
        batch.drop_constraint("ck_payout_item_source_kind", type_="check")
        batch.drop_constraint("ck_payout_item_totals", type_="check")
        batch.create_check_constraint("ck_payout_item_source_kind", _old_source_kind_check())
        batch.create_check_constraint("ck_payout_item_totals", _old_totals_check())
        batch.drop_column("unwithheld_individual_income_tax_fen")
        batch.drop_column("theoretical_individual_income_tax_fen")
        batch.drop_column("settlement_mode")
    _create_item_guard_if_postgresql()
