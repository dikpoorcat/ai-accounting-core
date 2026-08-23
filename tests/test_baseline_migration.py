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


def test_sqlite_baseline_and_forward_revision_round_trip(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'baseline.db').as_posix()}"
    config = _config(database_url)
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == ["0019_deferred_output_vat"]
    assert [revision.revision for revision in scripts.walk_revisions()] == [
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
                == "0019_deferred_output_vat"
            )
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
                == "0019_deferred_output_vat"
            )
    finally:
        engine.dispose()
