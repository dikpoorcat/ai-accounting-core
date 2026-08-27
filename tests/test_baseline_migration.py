from __future__ import annotations

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_sqlite_baseline_and_forward_revision_round_trip(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'baseline.db').as_posix()}"
    config = _config(database_url)
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["0030_commentary_action"]
    assert [revision.revision for revision in scripts.walk_revisions()] == [
        "0030_commentary_action",
        "0029_close_commentary",
        "0028_quarterly_statements",
        "0027_period_close_perf",
        "0026_salary_petty_recovery",
        "0025_payroll_tax_declaration",
        "0024_first_wage_tax",
        "0023_payroll_contrib_actuals",
        "0022_bank_recon_multi_match",
        "0021_taxpayer_identification",
        "0020_salary_deduction_payout",
        "0019_deferred_output_vat",
        "0018_payroll_participation",
        "0017_payroll_reported_salary",
        "0016_close_labor_module",
        "0015_labor_final_events",
        "0014_labor_gross_unwithheld",
        "0013_labor_remuneration",
        "0012_zero_tax_confirmation",
        "0011_close_as_of_items",
        "0010_depreciation_batch",
        "0009_canonical_asset_sources",
        "0008_grouped_depreciation",
        "0007_refundable_deposit",
        "0006_bank_interest",
        "0005_ready_fixed_asset",
        "0004_close_approval_width",
        "0003_owner_close_approval",
        "0002_pilot_events",
        "0001_baseline",
    ]

    command.upgrade(config, "0001_baseline")
    engine = create_engine(database_url)
    try:
        tables = set(inspect(engine).get_table_names())
        assert {
            "organizations",
            "business_events",
            "vouchers",
            "tax_rules",
            "late_bank_evidence_actions",
        } <= tables
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == "0001_baseline"
            )
            assert "reimbursing_employee_id" not in {
                column["name"] for column in inspect(connection).get_columns("fixed_assets")
            }
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM tax_rules "
                        "WHERE code = 'small_scale_used_fixed_asset_vat_2026'"
                    )
                )
                == 1
            )
        command.upgrade(config, "head")
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == "0030_commentary_action"
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
            assert "accounting_period_close_approvals" in set(inspect(connection).get_table_names())
            activation_columns = {
                column["name"]
                for column in inspect(connection).get_columns("fixed_asset_activations")
            }
            assert {
                "depreciation_group_code",
                "depreciation_rounding_policy",
            } <= activation_columns
            assert {
                "fixed_asset_cost_sources",
                "fixed_asset_depreciation_batches",
                "zero_tax_period_confirmations",
                "labor_service_persons",
                "labor_remuneration_batches",
                "unified_payout_runs",
                "financial_statement_classifications",
                "enterprise_income_tax_quarter_confirmations",
                "accounting_period_close_commentaries",
            } <= set(inspect(connection).get_table_names())
            payout_item_columns = {
                column["name"]
                for column in inspect(connection).get_columns("unified_payout_run_items")
            }
            assert {
                "settlement_mode",
                "theoretical_individual_income_tax_fen",
                "unwithheld_individual_income_tax_fen",
            } <= payout_item_columns
            depreciation_columns = {
                column["name"]
                for column in inspect(connection).get_columns("fixed_asset_depreciations")
            }
            assert "batch_id" in depreciation_columns
            payroll_line_columns = {
                column["name"] for column in inspect(connection).get_columns("payroll_lines")
            }
            assert "tax_reported_salary_fen" in payroll_line_columns
            assert "wage_tax_declaration_state" in payroll_line_columns
            assert {
                "base_salary_fen",
                "performance_pay_fen",
                "taxable_allowance_fen",
                "tax_exempt_income_fen",
                "attendance_deduction_fen",
            }.isdisjoint(payroll_line_columns)
            assert "deferred_output_vat_transfers" in set(
                inspect(connection).get_table_names()
            )
            assert (
                connection.scalar(
                    sa.text(
                        "SELECT COUNT(*) FROM accounts "
                        "WHERE system_role = 'deferred_output_vat'"
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
                == "0030_commentary_action"
            )
    finally:
        engine.dispose()


def _seed_pre_taxpayer_identification_organization(database_url: str) -> None:
    config = _config(database_url)
    command.upgrade(config, "0020_salary_deduction_payout")
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.execute(
                sa.text(
                    """
                    INSERT INTO organizations (
                        id, name, taxpayer_type, filing_cycle, jurisdiction,
                        urban_maintenance_rate, accounting_standard,
                        accounting_period_control_enabled, created_at
                    ) VALUES (
                        '11111111111111111111111111111111', '迁移前企业', 'small_scale',
                        'quarterly', 'CN', 0.07, 'small_enterprise', true,
                        CURRENT_TIMESTAMP
                    )
                    """
                )
            )
    finally:
        engine.dispose()


def test_existing_organization_taxpayer_identification_backfill_is_explicit(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing_url = f"sqlite+pysqlite:///{(tmp_path / 'missing-tax-id.db').as_posix()}"
    _seed_pre_taxpayer_identification_organization(missing_url)
    monkeypatch.delenv("AI_ACCOUNTING_TAXPAYER_IDENTIFICATION_NUMBER", raising=False)
    with pytest.raises(RuntimeError, match="requires AI_ACCOUNTING"):
        command.upgrade(_config(missing_url), "head")

    backfill_url = f"sqlite+pysqlite:///{(tmp_path / 'tax-id-backfill.db').as_posix()}"
    _seed_pre_taxpayer_identification_organization(backfill_url)
    monkeypatch.setenv(
        "AI_ACCOUNTING_TAXPAYER_IDENTIFICATION_NUMBER", "91330106MA1234567T"
    )
    command.upgrade(_config(backfill_url), "head")
    engine = create_engine(backfill_url)
    try:
        with engine.connect() as connection:
            assert connection.scalar(
                sa.text("SELECT taxpayer_identification_number FROM organizations")
            ) == "91330106MA1234567T"
    finally:
        engine.dispose()
