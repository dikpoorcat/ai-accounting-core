"""Persist typed owner workflow facts and verified payroll-tax exports.

Revision ID: 0017_owner_workflow
Revises: 0016_owner_reserve_settlement
Create Date: 2026-09-01
"""

from __future__ import annotations

import re

import sqlalchemy as sa

from alembic import op

revision = "0017_owner_workflow"
down_revision = "0016_owner_reserve_settlement"
branch_labels = None
depends_on = None

_V5 = "accounting_period_close_checker_2026.5"
_V6 = "accounting_period_close_checker_2026.6"
_V7 = "accounting_period_close_checker_2026.7"
_V8 = "accounting_period_close_checker_2026.8"
_OWNER_GATE_ANCHOR = (
    "            SELECT count(*) INTO unfinished_payroll FROM payroll_batches\n"
)
_OWNER_GATE = f"""            IF target_close.checker_version = '{_V8}'
               AND target_period.start_date >= DATE '2026-08-01' THEN
                IF target_close.calculation::jsonb #>>
                       '{{owner_workflow_close_gates,version}}'
                       IS DISTINCT FROM 'owner_workflow_close_gates_2026.1'
                   OR COALESCE((target_close.calculation::jsonb #>>
                       '{{owner_workflow_close_gates,enforced_for_period}}')::boolean,
                       false) IS NOT TRUE
                   OR COALESCE((target_close.calculation::jsonb #>>
                       '{{owner_workflow_close_gates,gates,workforce_review,satisfied}}')::boolean,
                       false) IS NOT TRUE
                   OR COALESCE((target_close.calculation::jsonb #>>
                       '{{owner_workflow_close_gates,gates,contribution_accounting,satisfied}}')::boolean,
                       false) IS NOT TRUE
                   OR COALESCE((target_close.calculation::jsonb #>>
                       '{{owner_workflow_close_gates,gates,non_bank_materials,satisfied}}')::boolean,
                       false) IS NOT TRUE
                   OR NOT EXISTS (
                       SELECT 1 FROM owner_period_confirmations AS confirmation
                        WHERE confirmation.org_id = target_period.org_id
                          AND confirmation.period_id = target_period.id
                          AND confirmation.fact_type = 'workforce_review'
                          AND confirmation.id = (target_close.calculation::jsonb #>>
                              '{{owner_workflow_close_gates,gates,workforce_review,confirmation_id}}')::uuid
                          AND confirmation.source_snapshot_hash =
                              target_close.calculation::jsonb #>>
                              '{{owner_workflow_close_gates,gates,workforce_review,source_snapshot_hash}}'
                          AND NOT EXISTS (
                              SELECT 1 FROM owner_period_confirmations AS successor
                               WHERE successor.supersedes_id = confirmation.id))
                   OR NOT EXISTS (
                       SELECT 1 FROM owner_period_confirmations AS confirmation
                        WHERE confirmation.org_id = target_period.org_id
                          AND confirmation.period_id = target_period.id
                          AND confirmation.fact_type = 'non_bank_materials'
                          AND confirmation.id = (target_close.calculation::jsonb #>>
                              '{{owner_workflow_close_gates,gates,non_bank_materials,confirmation_id}}')::uuid
                          AND confirmation.source_snapshot_hash =
                              target_close.calculation::jsonb #>>
                              '{{owner_workflow_close_gates,gates,non_bank_materials,source_snapshot_hash}}'
                          AND NOT EXISTS (
                              SELECT 1 FROM owner_period_confirmations AS successor
                               WHERE successor.supersedes_id = confirmation.id))
                   OR (
                       EXISTS (
                           SELECT 1 FROM employees AS employee
                            WHERE employee.org_id = target_period.org_id
                              AND employee.status = 'active'
                              AND employee.employment_start_date <= target_period.end_date
                              AND (employee.employment_end_date IS NULL OR
                                   employee.employment_end_date >= target_period.start_date))
                       AND (
                           NOT EXISTS (
                               SELECT 1
                                 FROM payroll_contribution_assessment_confirmations AS assessment
                                WHERE assessment.org_id = target_period.org_id
                                  AND assessment.period_id = target_period.id
                                  AND assessment.id = (target_close.calculation::jsonb #>>
                                      '{{owner_workflow_close_gates,gates,contribution_accounting,confirmation_id}}')::uuid
                                  AND assessment.calculation_hash =
                                      target_close.calculation::jsonb #>>
                                      '{{owner_workflow_close_gates,gates,contribution_accounting,calculation_hash}}'
                                  AND NOT EXISTS (
                                      SELECT 1
                                      FROM payroll_contribution_assessment_confirmations
                                           AS successor
                                       WHERE successor.supersedes_id = assessment.id))
                           OR NOT EXISTS (
                               SELECT 1 FROM payroll_batches AS batch
                                WHERE batch.org_id = target_period.org_id
                                  AND batch.id = (target_close.calculation::jsonb #>>
                                      '{{owner_workflow_close_gates,gates,contribution_accounting,payroll,batch_id}}')::uuid
                                  AND batch.payroll_period =
                                      to_char(target_period.start_date, 'YYYY-MM')
                                  AND batch.batch_kind = 'regular'
                                  AND batch.status = 'posted')))
                THEN
                    RAISE EXCEPTION 'ACCOUNTING_PERIOD_OWNER_WORKFLOW_GATE_INVALID';
                END IF;
            END IF;

"""


def _audit_columns() -> list[sa.Column]:
    return [
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("org_id", sa.Uuid(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_payload_hash", sa.String(length=64), nullable=False),
        sa.Column("supersedes_id", sa.Uuid(), nullable=True),
        sa.Column("execution_attribution_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("CURRENT_TIMESTAMP"),
            nullable=False,
        ),
    ]


def _audit_constraints(prefix: str, table: str) -> list[sa.Constraint]:
    return [
        sa.PrimaryKeyConstraint("id", name=f"pk_{table}"),
        sa.UniqueConstraint("org_id", "id", name=f"uq_{prefix}_org_id"),
        sa.UniqueConstraint("org_id", "idempotency_key", name=f"uq_{prefix}_idempotency"),
        sa.UniqueConstraint("supersedes_id", name=f"uq_{prefix}_successor"),
        sa.ForeignKeyConstraint(
            ["org_id", "supersedes_id"],
            [f"{table}.org_id", f"{table}.id"],
            name=f"fk_{prefix}_org_supersedes",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "execution_attribution_id"],
            ["execution_attributions.org_id", "execution_attributions.id"],
            name=f"fk_{prefix}_execution_attribution",
            ondelete="RESTRICT",
        ),
    ]


def _replace_once(source: str, old: str, new: str) -> str:
    if source.count(old) != 1:
        raise RuntimeError("ACCOUNTING_PERIOD_CLOSE_V8_VALIDATOR_MISMATCH")
    return source.replace(old, new, 1)


def _replace_regex_count(
    source: str,
    pattern: str,
    replacement: str,
    *,
    expected_count: int,
) -> str:
    result, count = re.subn(pattern, replacement, source)
    if count != expected_count:
        raise RuntimeError("ACCOUNTING_PERIOD_CLOSE_V8_VALIDATOR_MISMATCH")
    return result


def _close_function_definition() -> str:
    return op.get_bind().execute(
        sa.text(
            "SELECT pg_get_functiondef("
            "'public.finance_assert_accounting_period_close(uuid)'::regprocedure)"
        )
    ).scalar_one()


def _upgrade_close_validator_to_v8() -> None:
    definition = _close_function_definition()
    definition = _replace_regex_count(
        definition,
        rf"'{re.escape(_V5)}'\s*,\s*'{re.escape(_V6)}'\s*,\s*"
        rf"'{re.escape(_V7)}'(\s*\))",
        f"'{_V5}', '{_V6}', '{_V7}', '{_V8}'" + r"\1",
        expected_count=5,
    )
    definition = _replace_regex_count(
        definition,
        rf"target_close\.checker_version IN \('{re.escape(_V6)}',\s*"
        rf"'{re.escape(_V7)}'\)",
        f"target_close.checker_version IN ('{_V6}', '{_V7}', '{_V8}')",
        expected_count=2,
    )
    definition = _replace_regex_count(
        definition,
        rf"target_close\.checker_version = '{re.escape(_V7)}'",
        f"target_close.checker_version IN ('{_V7}', '{_V8}')",
        expected_count=2,
    )
    definition = _replace_once(
        definition,
        _OWNER_GATE_ANCHOR,
        _OWNER_GATE + _OWNER_GATE_ANCHOR,
    )
    op.execute(sa.text(definition))


def _downgrade_close_validator_from_v8() -> None:
    definition = _replace_once(_close_function_definition(), _OWNER_GATE, "")
    definition = _replace_regex_count(
        definition,
        rf"target_close\.checker_version IN \('{re.escape(_V7)}',\s*"
        rf"'{re.escape(_V8)}'\)",
        f"target_close.checker_version = '{_V7}'",
        expected_count=2,
    )
    definition = _replace_regex_count(
        definition,
        rf"target_close\.checker_version IN \('{re.escape(_V6)}',\s*"
        rf"'{re.escape(_V7)}',\s*'{re.escape(_V8)}'\)",
        f"target_close.checker_version IN ('{_V6}', '{_V7}')",
        expected_count=2,
    )
    definition = _replace_regex_count(
        definition,
        rf"'{re.escape(_V5)}'\s*,\s*'{re.escape(_V6)}'\s*,\s*"
        rf"'{re.escape(_V7)}'\s*,\s*'{re.escape(_V8)}'(\s*\))",
        f"'{_V5}', '{_V6}', '{_V7}'" + r"\1",
        expected_count=5,
    )
    op.execute(sa.text(definition))


def upgrade() -> None:
    bind = op.get_bind()
    op.create_table(
        "owner_period_confirmations",
        *_audit_columns(),
        sa.Column("period_id", sa.Uuid(), nullable=False),
        sa.Column("fact_type", sa.String(length=40), nullable=False),
        sa.Column("confirmation_state", sa.String(length=40), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot", sa.JSON(), nullable=False),
        sa.Column("confirmation_note", sa.Text(), nullable=False),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
        *_audit_constraints("owner_period_confirmation", "owner_period_confirmations"),
        sa.ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_owner_period_confirmation_org_period",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "fact_type IN ('workforce_review','non_bank_materials')",
            name="ck_owner_period_confirmation_fact_type",
        ),
        sa.CheckConstraint(
            "(fact_type = 'workforce_review' AND confirmation_state IN "
            "('no_change','changes_resolved')) OR "
            "(fact_type = 'non_bank_materials' AND confirmation_state = 'complete')",
            name="ck_owner_period_confirmation_state",
        ),
        sa.CheckConstraint(
            "length(source_snapshot_hash) = 64 AND length(request_payload_hash) = 64",
            name="ck_owner_period_confirmation_hashes",
        ),
        sa.CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_owner_period_confirmation_note",
        ),
    )
    op.create_index(
        "ix_owner_period_confirmations_org_id",
        "owner_period_confirmations",
        ["org_id"],
    )
    op.create_index(
        "ix_owner_period_confirmations_period_id",
        "owner_period_confirmations",
        ["period_id"],
    )
    op.create_index(
        "uq_owner_period_confirmation_root",
        "owner_period_confirmations",
        ["org_id", "period_id", "fact_type"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
        sqlite_where=sa.text("supersedes_id IS NULL"),
    )

    op.create_table(
        "payroll_contribution_assessment_confirmations",
        *_audit_columns(),
        sa.Column("period_id", sa.Uuid(), nullable=False),
        sa.Column("contribution_period", sa.String(length=7), nullable=False),
        sa.Column("declaration_status", sa.String(length=30), nullable=False),
        sa.Column("declaration_date", sa.Date(), nullable=True),
        sa.Column("payment_status", sa.String(length=20), nullable=False),
        sa.Column("payment_date", sa.Date(), nullable=True),
        sa.Column("external_reference", sa.String(length=300), nullable=True),
        sa.Column("calculation_hash", sa.String(length=64), nullable=False),
        sa.Column("calculation", sa.JSON(), nullable=False),
        sa.Column("employee_social_insurance_fen", sa.BigInteger(), nullable=False),
        sa.Column("employer_social_insurance_fen", sa.BigInteger(), nullable=False),
        sa.Column("employee_housing_fund_fen", sa.BigInteger(), nullable=False),
        sa.Column("employer_housing_fund_fen", sa.BigInteger(), nullable=False),
        sa.Column("confirmation_note", sa.Text(), nullable=False),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
        *_audit_constraints(
            "contribution_assessment",
            "payroll_contribution_assessment_confirmations",
        ),
        sa.ForeignKeyConstraint(
            ["org_id", "period_id"],
            ["accounting_periods.org_id", "accounting_periods.id"],
            name="fk_contribution_assessment_org_period",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "declaration_status IN ('declared_paid','declared_unpaid','not_declared')",
            name="ck_contribution_assessment_declaration_status",
        ),
        sa.CheckConstraint(
            "payment_status IN ('paid','unpaid','not_applicable')",
            name="ck_contribution_assessment_payment_status",
        ),
        sa.CheckConstraint(
            "(declaration_status = 'declared_paid' AND declaration_date IS NOT NULL "
            "AND payment_status = 'paid' AND payment_date IS NOT NULL) OR "
            "(declaration_status = 'declared_unpaid' AND declaration_date IS NOT NULL "
            "AND payment_status = 'unpaid' AND payment_date IS NULL) OR "
            "(declaration_status = 'not_declared' AND declaration_date IS NULL "
            "AND payment_status = 'not_applicable' AND payment_date IS NULL)",
            name="ck_contribution_assessment_status_dates",
        ),
        sa.CheckConstraint(
            "length(contribution_period) = 7 AND substr(contribution_period, 5, 1) = '-'",
            name="ck_contribution_assessment_period",
        ),
        sa.CheckConstraint(
            "length(calculation_hash) = 64 AND length(request_payload_hash) = 64",
            name="ck_contribution_assessment_hashes",
        ),
        sa.CheckConstraint(
            "employee_social_insurance_fen >= 0 AND employer_social_insurance_fen >= 0 "
            "AND employee_housing_fund_fen >= 0 AND employer_housing_fund_fen >= 0",
            name="ck_contribution_assessment_amounts",
        ),
        sa.CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_contribution_assessment_note",
        ),
    )
    op.create_index(
        "ix_payroll_contribution_assessment_confirmations_org_id",
        "payroll_contribution_assessment_confirmations",
        ["org_id"],
    )
    op.create_index(
        "ix_payroll_contribution_assessment_confirmations_period_id",
        "payroll_contribution_assessment_confirmations",
        ["period_id"],
    )
    op.create_index(
        "ix_contribution_assessment_period",
        "payroll_contribution_assessment_confirmations",
        ["contribution_period"],
    )
    op.create_index(
        "uq_contribution_assessment_root",
        "payroll_contribution_assessment_confirmations",
        ["org_id", "period_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
        sqlite_where=sa.text("supersedes_id IS NULL"),
    )

    op.create_table(
        "payroll_tax_import_exports",
        *_audit_columns(),
        sa.Column("payroll_period", sa.String(length=7), nullable=False),
        sa.Column("payroll_source_hash", sa.String(length=64), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("source_batches", sa.JSON(), nullable=False),
        sa.Column("relative_storage_path", sa.Text(), nullable=False),
        sa.Column("file_name", sa.String(length=255), nullable=False),
        sa.Column("file_sha256", sa.String(length=64), nullable=False),
        sa.Column("row_count", sa.Integer(), nullable=False),
        *_audit_constraints("payroll_tax_import_export", "payroll_tax_import_exports"),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_payroll_tax_import_export_org",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(payroll_period) = 7 AND substr(payroll_period, 5, 1) = '-'",
            name="ck_payroll_tax_import_export_period",
        ),
        sa.CheckConstraint(
            "length(payroll_source_hash) = 64 AND length(source_snapshot_hash) = 64 "
            "AND length(request_payload_hash) = 64 "
            "AND length(file_sha256) = 64",
            name="ck_payroll_tax_import_export_hashes",
        ),
        sa.CheckConstraint("row_count > 0", name="ck_payroll_tax_import_export_row_count"),
    )
    op.create_index(
        "ix_payroll_tax_import_exports_org_id",
        "payroll_tax_import_exports",
        ["org_id"],
    )
    op.create_index(
        "ix_payroll_tax_import_exports_payroll_period",
        "payroll_tax_import_exports",
        ["payroll_period"],
    )

    op.create_table(
        "external_obligation_confirmations",
        *_audit_columns(),
        sa.Column("obligation_id", sa.Uuid(), nullable=False),
        sa.Column("obligation_code", sa.String(length=80), nullable=False),
        sa.Column("obligation_scope", sa.String(length=20), nullable=False),
        sa.Column("source_snapshot_hash", sa.String(length=64), nullable=False),
        sa.Column("completion_status", sa.String(length=30), nullable=False),
        sa.Column("completion_date", sa.Date(), nullable=False),
        sa.Column("external_reference", sa.String(length=300), nullable=True),
        sa.Column("confirmation_note", sa.Text(), nullable=False),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
        *_audit_constraints(
            "external_obligation_confirmation",
            "external_obligation_confirmations",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_external_obligation_confirmation_org",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "obligation_code IN ('individual_income_tax','periodic_tax_reporting',"
            "'annual_enterprise_income_tax','annual_business_report')",
            name="ck_external_obligation_confirmation_code",
        ),
        sa.CheckConstraint(
            "obligation_scope IN ('month','quarter','year')",
            name="ck_external_obligation_confirmation_scope",
        ),
        sa.CheckConstraint(
            "completion_status IN ('submitted','not_applicable')",
            name="ck_external_obligation_confirmation_status",
        ),
        sa.CheckConstraint(
            "length(source_snapshot_hash) = 64 AND length(request_payload_hash) = 64",
            name="ck_external_obligation_confirmation_hashes",
        ),
        sa.CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_external_obligation_confirmation_note",
        ),
    )
    op.create_index(
        "ix_external_obligation_confirmations_org_id",
        "external_obligation_confirmations",
        ["org_id"],
    )
    op.create_index(
        "ix_external_obligation_confirmations_obligation_id",
        "external_obligation_confirmations",
        ["obligation_id"],
    )
    op.create_index(
        "uq_external_obligation_confirmation_root",
        "external_obligation_confirmations",
        ["org_id", "obligation_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
        sqlite_where=sa.text("supersedes_id IS NULL"),
    )

    op.create_table(
        "organization_establishment_confirmations",
        *_audit_columns(),
        sa.Column("establishment_date", sa.Date(), nullable=False),
        sa.Column("confirmation_note", sa.Text(), nullable=False),
        sa.Column("evidence_snapshot", sa.JSON(), nullable=False),
        *_audit_constraints(
            "establishment_confirmation",
            "organization_establishment_confirmations",
        ),
        sa.ForeignKeyConstraint(
            ["org_id"],
            ["organizations.id"],
            name="fk_establishment_confirmation_org",
            ondelete="RESTRICT",
        ),
        sa.CheckConstraint(
            "length(request_payload_hash) = 64",
            name="ck_establishment_confirmation_request_hash",
        ),
        sa.CheckConstraint(
            "length(trim(confirmation_note)) BETWEEN 1 AND 2000",
            name="ck_establishment_confirmation_note",
        ),
    )
    op.create_index(
        "ix_organization_establishment_confirmations_org_id",
        "organization_establishment_confirmations",
        ["org_id"],
    )
    op.create_index(
        "uq_establishment_confirmation_root",
        "organization_establishment_confirmations",
        ["org_id"],
        unique=True,
        postgresql_where=sa.text("supersedes_id IS NULL"),
        sqlite_where=sa.text("supersedes_id IS NULL"),
    )
    if bind.dialect.name == "postgresql":
        trigger_prefixes = {
            "owner_period_confirmations": "owner_period_fact",
            "payroll_contribution_assessment_confirmations": "contribution_assessment",
            "payroll_tax_import_exports": "payroll_tax_import_export",
            "external_obligation_confirmations": "external_obligation",
            "organization_establishment_confirmations": "establishment_confirmation",
        }
        for table, trigger_prefix in trigger_prefixes.items():
            op.execute(
                f"CREATE TRIGGER {trigger_prefix}_execution_attribution_guard "
                f"BEFORE INSERT OR UPDATE ON {table} FOR EACH ROW "
                "EXECUTE FUNCTION finance_guard_attributed_root_0014()"
            )
            op.execute(
                f"CREATE TRIGGER {trigger_prefix}_append_only_guard "
                f"BEFORE DELETE OR UPDATE ON {table} FOR EACH ROW "
                "EXECUTE FUNCTION finance_block_financial_statement_fact_0028()"
            )
        _upgrade_close_validator_to_v8()


def downgrade() -> None:
    if op.get_bind().dialect.name == "postgresql":
        _downgrade_close_validator_from_v8()
    op.drop_index(
        "ix_organization_establishment_confirmations_org_id",
        table_name="organization_establishment_confirmations",
    )
    op.drop_index(
        "uq_establishment_confirmation_root",
        table_name="organization_establishment_confirmations",
    )
    op.drop_table("organization_establishment_confirmations")
    op.drop_index(
        "ix_external_obligation_confirmations_obligation_id",
        table_name="external_obligation_confirmations",
    )
    op.drop_index(
        "ix_external_obligation_confirmations_org_id",
        table_name="external_obligation_confirmations",
    )
    op.drop_index(
        "uq_external_obligation_confirmation_root",
        table_name="external_obligation_confirmations",
    )
    op.drop_table("external_obligation_confirmations")
    op.drop_index(
        "ix_payroll_tax_import_exports_payroll_period",
        table_name="payroll_tax_import_exports",
    )
    op.drop_index(
        "ix_payroll_tax_import_exports_org_id",
        table_name="payroll_tax_import_exports",
    )
    op.drop_table("payroll_tax_import_exports")
    op.drop_index(
        "ix_contribution_assessment_period",
        table_name="payroll_contribution_assessment_confirmations",
    )
    op.drop_index(
        "ix_payroll_contribution_assessment_confirmations_period_id",
        table_name="payroll_contribution_assessment_confirmations",
    )
    op.drop_index(
        "ix_payroll_contribution_assessment_confirmations_org_id",
        table_name="payroll_contribution_assessment_confirmations",
    )
    op.drop_index(
        "uq_contribution_assessment_root",
        table_name="payroll_contribution_assessment_confirmations",
    )
    op.drop_table("payroll_contribution_assessment_confirmations")
    op.drop_index(
        "ix_owner_period_confirmations_period_id",
        table_name="owner_period_confirmations",
    )
    op.drop_index(
        "ix_owner_period_confirmations_org_id",
        table_name="owner_period_confirmations",
    )
    op.drop_index(
        "uq_owner_period_confirmation_root",
        table_name="owner_period_confirmations",
    )
    op.drop_table("owner_period_confirmations")
