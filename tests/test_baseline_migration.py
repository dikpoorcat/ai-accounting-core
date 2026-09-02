from __future__ import annotations

import json
import shutil
import uuid
from datetime import date

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.models import AccountingPeriod, Evidence
from alembic import command


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_sqlite_formal_baseline_round_trip(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'baseline.db').as_posix()}"
    config = _config(database_url)
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["0021_optional_declaration_dates"]
    assert [revision.revision for revision in scripts.walk_revisions()] == [
        "0021_optional_declaration_dates",
        "0020_payroll_accrual_date",
        "0019_declaration_only",
        "0018_historical_obligation",
        "0017_owner_workflow",
        "0016_owner_reserve_settlement",
        "0015_cash_reimbursement",
        "0014_person_reimbursement",
        "0013_close_gate_hardening",
        "0012_close_checker_v7",
        "0011_expense_recovery_received",
        "0010_fs_close_profile",
        "0009_fs_close_readiness",
        "0008_fs_opening_unique_org",
        "0007_fs_opening_balance",
        "0006_payment_platform_transfer",
        "0005_social_insurance_late_fee",
        "0004_payroll_wage_tax_difference",
        "0003_historical_tax_rules",
        "0002_multi_company_business",
        "0001_formal_baseline",
    ]

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "organizations",
            "business_events",
            "vouchers",
            "tax_rules",
            "late_bank_evidence_actions",
            "accounting_period_close_approvals",
            "fixed_asset_cost_sources",
            "fixed_asset_depreciation_batches",
            "zero_tax_period_confirmations",
            "labor_service_persons",
            "labor_remuneration_batches",
            "unified_payout_runs",
            "deferred_output_vat_transfers",
            "payroll_contribution_actual_sets",
            "payroll_first_wage_tax_treatments",
            "financial_statement_classifications",
            "financial_statement_opening_balance_confirmations",
            "enterprise_income_tax_quarter_confirmations",
            "accounting_period_close_commentaries",
            "owner_period_confirmations",
            "payroll_contribution_assessment_confirmations",
            "payroll_tax_import_exports",
            "external_obligation_confirmations",
            "historical_obligation_completion_confirmations",
            "organization_establishment_confirmations",
        } <= tables
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == "0021_optional_declaration_dates"
            )
            organization_columns = {
                column["name"] for column in inspect(connection).get_columns("organizations")
            }
            assert "taxpayer_identification_number" in organization_columns
            taxpayer_column = next(
                column
                for column in inspect(connection).get_columns("organizations")
                if column["name"] == "taxpayer_identification_number"
            )
            assert taxpayer_column["nullable"] is False
            assert "reimbursing_employee_id" in {
                column["name"] for column in inspect(connection).get_columns("fixed_assets")
            }
            assert "owner_approval_id" in {
                column["name"]
                for column in inspect(connection).get_columns("accounting_period_closes")
            }
            assert {
                "depreciation_group_code",
                "depreciation_rounding_policy",
            } <= {
                column["name"]
                for column in inspect(connection).get_columns("fixed_asset_activations")
            }
            assert {
                "settlement_mode",
                "theoretical_individual_income_tax_fen",
                "unwithheld_individual_income_tax_fen",
                "salary_petty_cash_recovery_fen",
            } <= {
                column["name"]
                for column in inspect(connection).get_columns("unified_payout_run_items")
            }
            assert "batch_id" in {
                column["name"]
                for column in inspect(connection).get_columns("fixed_asset_depreciations")
            }
            payroll_line_columns = {
                column["name"] for column in inspect(connection).get_columns("payroll_lines")
            }
            assert {
                "tax_reported_salary_fen",
                "tax_reporting_difference_reason",
                "wage_tax_declaration_state",
            } <= payroll_line_columns
            assert {
                "base_salary_fen",
                "performance_pay_fen",
                "taxable_allowance_fen",
                "tax_exempt_income_fen",
                "attendance_deduction_fen",
            }.isdisjoint(payroll_line_columns)
            payroll_payment_date_column = next(
                column
                for column in inspect(connection).get_columns("payroll_batches")
                if column["name"] == "payment_date"
            )
            assert payroll_payment_date_column["nullable"] is True
            contribution_columns = {
                column["name"]: column
                for column in inspect(connection).get_columns(
                    "payroll_contribution_assessment_confirmations"
                )
            }
            assert contribution_columns["declaration_date"]["nullable"] is True
            assert contribution_columns["declaration_date_status"]["nullable"] is False
            obligation_columns = {
                column["name"]: column
                for column in inspect(connection).get_columns(
                    "external_obligation_confirmations"
                )
            }
            assert obligation_columns["completion_date"]["nullable"] is True
            assert obligation_columns["completion_date_status"]["nullable"] is False
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM tax_rules "
                        "WHERE code = 'small_scale_used_fixed_asset_vat_2026'"
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM labor_remuneration_tax_policy_versions "
                        "WHERE code = 'cn_resident_labor_remuneration_withholding'"
                    )
                )
                == 1
            )
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM accounts WHERE system_role = 'deferred_output_vat'"
                    )
                )
                == 0
            )
        command.check(config)

        command.downgrade(config, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}

        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == "0021_optional_declaration_dates"
            )
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
def test_postgres_0021_backfills_append_only_confirmation_rows() -> None:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = _config(database_url)
        config.attributes["database_url_override"] = database_url
        command.upgrade(config, "0020_payroll_accrual_date")
        engine = create_engine(database_url)
        contribution_id = uuid.uuid4()
        obligation_id = uuid.uuid4()
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="0021 PostgreSQL migration test",
                )
                evidence = Evidence(
                    org_id=organization.id,
                    sha256="f" * 64,
                    original_name="migration-fixture.txt",
                    media_type="text/plain",
                    source="test",
                    size_bytes=1,
                    storage_path="test/migration-fixture.txt",
                )
                session.add(evidence)
                session.flush()
                generated = AccountingPeriodService(
                    session, current_date=date(2026, 9, 2)
                ).generate_accounting_period(
                    GenerateAccountingPeriodRequest(
                        org_id=organization.id,
                        period_month="2026-08",
                        idempotency_key="0021-postgres-period",
                        confirmation_note="create migration fixture period",
                        evidence_references=[evidence.id],
                    )
                )
                assert generated.status == "posted"
                period = session.get(AccountingPeriod, generated.period_id)
                assert period is not None
                connection = session.connection()
                connection.exec_driver_sql(
                    "ALTER TABLE payroll_contribution_assessment_confirmations "
                    "DISABLE TRIGGER contribution_assessment_execution_attribution_guard"
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO payroll_contribution_assessment_confirmations "
                        "(id, org_id, idempotency_key, request_payload_hash, period_id, "
                        "contribution_period, declaration_status, declaration_date, "
                        "payment_status, payment_date, calculation_hash, calculation, "
                        "employee_social_insurance_fen, employer_social_insurance_fen, "
                        "employee_housing_fund_fen, employer_housing_fund_fen, "
                        "confirmation_note, evidence_snapshot) VALUES "
                        "(:id, :org_id, :key, :request_hash, :period_id, '2026-08', "
                        "'declared', :declaration_date, 'not_tracked', NULL, :calculation_hash, "
                        "CAST(:calculation AS json), 100, 200, 0, 0, :note, "
                        "CAST(:evidence AS json))"
                    ),
                    {
                        "id": contribution_id,
                        "org_id": organization.id,
                        "key": "0021-existing-contribution",
                        "request_hash": "b" * 64,
                        "period_id": period.id,
                        "declaration_date": date(2026, 9, 1),
                        "calculation_hash": "c" * 64,
                        "calculation": json.dumps({}),
                        "note": "existing contribution confirmation",
                        "evidence": json.dumps([]),
                    },
                )
                connection.exec_driver_sql(
                    "ALTER TABLE payroll_contribution_assessment_confirmations "
                    "ENABLE TRIGGER contribution_assessment_execution_attribution_guard"
                )
                connection.exec_driver_sql(
                    "ALTER TABLE external_obligation_confirmations "
                    "DISABLE TRIGGER external_obligation_execution_attribution_guard"
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO external_obligation_confirmations "
                        "(id, org_id, obligation_id, obligation_code, obligation_scope, "
                        "source_snapshot_hash, completion_status, completion_date, "
                        "idempotency_key, request_payload_hash, confirmation_note, "
                        "evidence_snapshot) VALUES "
                        "(:id, :org_id, :obligation_id, 'individual_income_tax', 'month', "
                        ":source_hash, 'submitted', :completion_date, :key, :request_hash, "
                        ":note, CAST(:evidence AS json))"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "org_id": organization.id,
                        "obligation_id": obligation_id,
                        "source_hash": "d" * 64,
                        "completion_date": date(2026, 9, 2),
                        "key": "0021-existing-obligation",
                        "request_hash": "e" * 64,
                        "note": "existing external confirmation",
                        "evidence": json.dumps([]),
                    },
                )
                connection.exec_driver_sql(
                    "ALTER TABLE external_obligation_confirmations "
                    "ENABLE TRIGGER external_obligation_execution_attribution_guard"
                )
                session.commit()

            command.upgrade(config, "head")
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "0021_optional_declaration_dates"
                )
                assert connection.scalar(
                    sa.text(
                        "SELECT declaration_date_status FROM "
                        "payroll_contribution_assessment_confirmations WHERE id = :id"
                    ),
                    {"id": contribution_id},
                ) == "established"
                assert connection.scalar(
                    sa.text(
                        "SELECT completion_date_status FROM "
                        "external_obligation_confirmations WHERE obligation_id = :id"
                    ),
                    {"id": obligation_id},
                ) == "established"
                assert dict(
                    connection.execute(
                        sa.text(
                            "SELECT tgname, tgenabled FROM pg_trigger WHERE NOT tgisinternal "
                            "AND tgname IN ('contribution_assessment_append_only_guard', "
                            "'external_obligation_append_only_guard')"
                        )
                    ).all()
                ) == {
                    "contribution_assessment_append_only_guard": "O",
                    "external_obligation_append_only_guard": "O",
                }
        finally:
            engine.dispose()
