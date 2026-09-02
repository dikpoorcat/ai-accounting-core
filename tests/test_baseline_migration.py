from __future__ import annotations

import shutil

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import create_engine, inspect
from testcontainers.community.postgres import PostgresContainer

from alembic import command

BUSINESS_REVISION = "0001_business_baseline_v2"
POSTGRES_IMAGE = "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"  # noqa: E501


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["database_url_override"] = database_url
    return config


def _assert_business_baseline(engine: sa.Engine) -> None:
    tables = set(inspect(engine).get_table_names())
    assert {
        "organizations",
        "business_events",
        "vouchers",
        "tax_rules",
        "accounting_period_close_approvals",
        "fixed_asset_depreciation_batches",
        "payroll_contribution_actual_sets",
        "owner_period_confirmations",
        "payroll_contribution_assessment_confirmations",
        "external_obligation_confirmations",
        "historical_obligation_completion_confirmations",
        "financial_statement_classifications",
        "enterprise_income_tax_quarter_confirmations",
    } <= tables
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            BUSINESS_REVISION
        )
        payroll_columns = {
            column["name"] for column in inspect(connection).get_columns("payroll_lines")
        }
        assert {
            "tax_reported_salary_fen",
            "tax_reporting_difference_reason",
            "wage_tax_declaration_state",
        } <= payroll_columns
        assert {
            "base_salary_fen",
            "performance_pay_fen",
            "attendance_deduction_fen",
        }.isdisjoint(payroll_columns)
        assert connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM tax_rules "
                "WHERE code = 'small_scale_used_fixed_asset_vat_2026'"
            )
        ) == 1
        assert connection.scalar(
            sa.text(
                "SELECT COUNT(*) FROM labor_remuneration_tax_policy_versions "
                "WHERE code = 'cn_resident_labor_remuneration_withholding'"
            )
        ) == 1


def test_sqlite_business_baseline_upgrade_downgrade_upgrade(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'baseline.db').as_posix()}"
    config = _config(database_url)
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == [BUSINESS_REVISION]
    assert [revision.revision for revision in scripts.walk_revisions()] == [
        BUSINESS_REVISION
    ]

    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        _assert_business_baseline(engine)
        command.check(config)
        command.downgrade(config, "base")
        assert set(inspect(engine).get_table_names()) == {"alembic_version"}
        command.upgrade(config, "head")
        _assert_business_baseline(engine)
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.postgres_current
@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
def test_postgres_business_baseline_upgrade_check_downgrade_upgrade() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = _config(database_url)
        command.upgrade(config, "head")
        engine = create_engine(database_url)
        try:
            _assert_business_baseline(engine)
            command.check(config)
            with engine.connect() as connection:
                function_body = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_accounting_period_close(uuid)'::regprocedure)"
                    )
                )
            assert "regular_payroll_plan_v1" in function_body
            assert "source_snapshot_hash" in function_body
            assert "payroll_source_hash" not in function_body
            command.downgrade(config, "base")
            assert set(inspect(engine).get_table_names()) == {"alembic_version"}
            command.upgrade(config, "head")
            _assert_business_baseline(engine)
        finally:
            engine.dispose()
