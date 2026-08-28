from __future__ import annotations

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect

from alembic import command


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_sqlite_formal_baseline_round_trip(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'baseline.db').as_posix()}"
    config = _config(database_url)
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["0001_formal_baseline"]
    assert [revision.revision for revision in scripts.walk_revisions()] == ["0001_formal_baseline"]

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
            "enterprise_income_tax_quarter_confirmations",
            "accounting_period_close_commentaries",
        } <= tables
        with engine.connect() as connection:
            assert (
                connection.scalar(sa.text("SELECT version_num FROM alembic_version"))
                == "0001_formal_baseline"
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
            assert {"tax_reported_salary_fen", "wage_tax_declaration_state"} <= (
                payroll_line_columns
            )
            assert {
                "base_salary_fen",
                "performance_pay_fen",
                "taxable_allowance_fen",
                "tax_exempt_income_fen",
                "attendance_deduction_fen",
            }.isdisjoint(payroll_line_columns)
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
                == "0001_formal_baseline"
            )
    finally:
        engine.dispose()
