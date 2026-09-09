from __future__ import annotations

import os
import shutil
from contextlib import nullcontext

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from alembic.util import CommandError
from sqlalchemy import create_engine, inspect
from testcontainers.community.postgres import PostgresContainer

from alembic import command

BUSINESS_REVISION = "0001_business_baseline_v4"
BUSINESS_HEAD = "0005_payroll_provenance"
POSTGRES_IMAGE = (
    "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"  # noqa: E501
)


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
    config.attributes["database_url_override"] = database_url
    return config


def test_revision_identifiers_fit_alembic_version_column() -> None:
    for filename in ("alembic.ini", "catalog_alembic.ini"):
        scripts = ScriptDirectory.from_config(Config(filename))
        assert all(len(revision.revision) <= 32 for revision in scripts.walk_revisions())


def _assert_business_baseline(engine: sa.Engine) -> None:
    inspector = inspect(engine)
    tables = set(inspector.get_table_names())
    assert {
        "unified_payout_runs",
        "unified_payout_run_items",
        "unified_payout_run_evidence",
        "unified_payout_run_bank_transactions",
    }.isdisjoint(tables)
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
    expected_component_columns = {
        "borrowings": "component_id",
        "borrowing_interest_accruals": "component_id",
        "borrowing_payments": "component_id",
        "payroll_withholding_payment_allocations": "payment_component_id",
        "payroll_salary_actual_deduction_allocations": "payment_component_id",
        "enterprise_income_tax_settlements": "component_id",
        "open_items": "account_id",
    }
    for table, column in expected_component_columns.items():
        assert column in {item["name"] for item in inspector.get_columns(table)}
    borrowing_accrual_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("borrowing_interest_accruals")
    }
    borrowing_payment_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("borrowing_payments")
    }
    dependency_uniques = {
        tuple(item["column_names"])
        for item in inspector.get_unique_constraints("business_event_dependencies")
    }
    assert ("component_id",) in borrowing_accrual_uniques
    assert ("component_id",) in borrowing_payment_uniques
    assert ("event_id",) not in borrowing_accrual_uniques
    assert ("event_id",) not in borrowing_payment_uniques
    assert ("parent_component_id", "child_component_id") in dependency_uniques
    assert ("child_component_id",) not in dependency_uniques
    with engine.connect() as connection:
        assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
            BUSINESS_HEAD
        )
        payroll_columns = {
            column["name"] for column in inspect(connection).get_columns("payroll_lines")
        }
        assert {
            "tax_reported_salary_fen",
            "tax_reporting_difference_reason",
            "wage_tax_scope",
        } <= payroll_columns
        assert {
            "base_salary_fen",
            "performance_pay_fen",
            "attendance_deduction_fen",
        }.isdisjoint(payroll_columns)
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM tax_rules "
                    "WHERE code = 'small_scale_used_fixed_asset_vat_2026'"
                )
            )
            == 1
        )
        dependency_sql = (
            connection.scalar(
                sa.text(
                    "SELECT sql FROM sqlite_master "
                    "WHERE type='table' AND name='business_event_dependencies'"
                )
            )
            if connection.dialect.name == "sqlite"
            else None
        )
        if dependency_sql is not None:
            assert "dependency_kind = 'component_source'" in dependency_sql
            assert "advance_fulfillment" not in dependency_sql
        assert (
            connection.scalar(
                sa.text(
                    "SELECT COUNT(*) FROM labor_remuneration_tax_policy_versions "
                    "WHERE code = 'cn_resident_labor_remuneration_withholding'"
                )
            )
            == 1
        )


def test_sqlite_business_baseline_upgrade_downgrade_upgrade(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'baseline.db').as_posix()}"
    config = _config(database_url)
    scripts = ScriptDirectory.from_config(config)

    assert scripts.get_heads() == [BUSINESS_HEAD]
    assert [revision.revision for revision in scripts.walk_revisions()] == [
        BUSINESS_HEAD,
        "0004_payroll_dependency_scope",
        "0003_payroll_correction_uses",
        "0002_atomic_corrections",
        BUSINESS_REVISION,
    ]

    command.upgrade(config, BUSINESS_REVISION)
    command.downgrade(config, "base")
    command.upgrade(config, "head")
    engine = create_engine(database_url)
    try:
        _assert_business_baseline(engine)
        command.check(config)
        with pytest.raises(RuntimeError, match="payroll provenance protection"):
            command.downgrade(config, "base")
    finally:
        engine.dispose()


def test_unknown_database_is_rejected_without_even_creating_version_table(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'unknown.db').as_posix()}"
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE unknown_business (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO unknown_business VALUES (1)")
        with pytest.raises(RuntimeError, match="BUSINESS_V4_REQUIRES_EMPTY_DATABASE"):
            command.upgrade(_config(database_url), "head")
        assert set(inspect(engine).get_table_names()) == {"unknown_business"}
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT id FROM unknown_business")) == 1
    finally:
        engine.dispose()


@pytest.mark.parametrize("old_revision", ["0001_business_baseline_v3", "0004_fact_precision"])
def test_retired_revision_is_rejected_without_modifying_old_database(tmp_path, old_revision):
    url = f"sqlite+pysqlite:///{(tmp_path / 'retired.db').as_posix()}"
    engine = create_engine(url)
    try:
        with engine.begin() as connection:
            connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32))")
            connection.execute(
                sa.text("INSERT INTO alembic_version VALUES (:revision)"),
                {"revision": old_revision},
            )
            connection.exec_driver_sql("CREATE TABLE old_business (id INTEGER PRIMARY KEY)")
            connection.exec_driver_sql("INSERT INTO old_business VALUES (1)")
        with pytest.raises(CommandError, match="Can't locate revision"):
            command.upgrade(_config(url), "head")
        assert set(inspect(engine).get_table_names()) == {"alembic_version", "old_business"}
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                old_revision
            )
            assert connection.scalar(sa.text("SELECT id FROM old_business")) == 1
    finally:
        engine.dispose()


def test_new_baseline_seeds_purchase_accounts_and_refuses_populated_downgrade(tmp_path):
    from sqlalchemy.orm import Session

    from ai_accounting.coa import seed_organization
    from ai_accounting.models import Account

    url = f"sqlite+pysqlite:///{(tmp_path / 'baseline-v4.db').as_posix()}"
    config = _config(url)
    command.upgrade(config, BUSINESS_REVISION)
    engine = create_engine(url)
    roles = {"prepayments", "intangible_project_cost", "development_expenditure"}
    try:
        with Session(engine) as session:
            org = seed_organization(
                session, name="迁移测试", taxpayer_identification_number="91330106MA1234567T"
            )
            org_id = org.id
            session.commit()
        command.upgrade(config, "head")
        with Session(engine) as session:
            accounts = session.scalars(
                sa.select(Account).where(Account.org_id == org_id, Account.system_role.in_(roles))
            ).all()
            assert {account.business_class for account in accounts} == roles
            assert len(accounts) == 3
        command.check(config)
        with pytest.raises(
            RuntimeError, match="payroll provenance protection"
        ):
            command.downgrade(config, "base")
    finally:
        engine.dispose()


@pytest.mark.postgres
@pytest.mark.postgres_current
@pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed")
def test_postgres_business_baseline_upgrade_check_downgrade_upgrade() -> None:
    external = os.environ.get("FINANCE_BASELINE_TEST_URL")
    container = (
        nullcontext(None) if external else PostgresContainer(POSTGRES_IMAGE, driver="psycopg")
    )
    with container as postgres:
        database_url = external or postgres.get_connection_url(driver="psycopg")
        config = _config(database_url)
        command.upgrade(config, BUSINESS_REVISION)
        engine = create_engine(database_url)
        try:
            command.downgrade(config, "base")
            assert set(inspect(engine).get_table_names()) == {"alembic_version"}
            with engine.begin() as connection:
                connection.exec_driver_sql("DROP TABLE alembic_version")
                connection.exec_driver_sql("CREATE TABLE unknown_business (id INTEGER PRIMARY KEY)")
                connection.exec_driver_sql("INSERT INTO unknown_business VALUES (1)")
            with pytest.raises(RuntimeError, match="BUSINESS_V4_REQUIRES_EMPTY_DATABASE"):
                command.upgrade(config, "head")
            assert set(inspect(engine).get_table_names()) == {"unknown_business"}
            with engine.begin() as connection:
                assert connection.scalar(sa.text("SELECT id FROM unknown_business")) == 1
                connection.exec_driver_sql("DROP TABLE unknown_business")
            command.upgrade(config, "head")
            _assert_business_baseline(engine)
            command.check(config)
            with engine.connect() as connection:
                function_body = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_accounting_period_close(uuid)'::regprocedure)"
                    )
                )
                component_guard = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_component_semantics(uuid)'::regprocedure)"
                    )
                )
                final_guard = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_final_business_event(uuid)'::regprocedure)"
                    )
                )
                dependency_guard = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_business_event_dependency(uuid)'::regprocedure)"
                    )
                )
                legacy_functions = connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname='public' AND p.proname IN ("
                        "'finance_assert_final_business_event_0010',"
                        "'finance_assert_final_business_event_0014',"
                        "'finance_assert_fixed_asset_event_shape_0014',"
                        "'finance_assert_intangible_borrowing_event_shape_0014')"
                    )
                )
                dependency_constraint = connection.scalar(
                    sa.text(
                        "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                        "WHERE conname='ck_business_event_dependency_kind'"
                    )
                )
                event_type_function_names = set(
                    connection.scalars(
                        sa.text(
                            "SELECT p.proname FROM pg_proc p "
                            "JOIN pg_namespace n ON n.oid=p.pronamespace "
                            "WHERE n.nspname='public' AND p.prokind='f' "
                            "AND pg_get_functiondef(p.oid) LIKE '%event_type%'"
                        )
                    )
                )
                amendment_guard = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef('finance_guard_event_amendment()'::regprocedure)"
                    )
                )
                payroll_link_guard = connection.scalar(
                    sa.text(
                        "SELECT pg_get_functiondef("
                        "'finance_assert_final_payroll_event_links(uuid)'::regprocedure)"
                    )
                )
                obsolete_unified_payout_runtime = connection.scalar(
                    sa.text(
                        "SELECT count(*) FROM pg_proc p "
                        "JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname='public' AND p.prokind='f' AND ("
                        "p.proname LIKE '%unified_payout%' OR "
                        "pg_get_functiondef(p.oid) LIKE '%unified_payout%')"
                    )
                )
            assert "regular_payroll_plan_v1" in function_body
            assert "line_snapshot" in function_body
            assert "request_payload_hash_at_close" in function_body
            assert "ACCOUNTING_PERIOD_SNAPSHOT_IMMUTABLE" in function_body
            assert "payroll_source_hash" not in function_body
            assert "UNSUPPORTED_BUSINESS_COMPONENT_KIND" in component_guard
            assert "FIXED_ASSET_COMPONENT_FACTS_MISMATCH" in component_guard
            assert "REVERSAL_COMPONENT_INVERSE_MISMATCH" in component_guard
            assert "event_type" not in component_guard
            assert "finance_assert_component_event(target_event_id)" in final_guard
            assert "event_type" not in final_guard
            assert "_0010" not in final_guard and "_0014" not in final_guard
            assert "component_source" in dependency_guard
            assert "event_type" not in dependency_guard
            assert legacy_functions == 0
            assert "component_source" in dependency_constraint
            assert "advance_fulfillment" not in dependency_constraint
            assert '"business_event_components"' in amendment_guard
            assert '"component_cash_flow_allocations"' in amendment_guard
            assert "component_id=component_id" not in payroll_link_guard
            assert event_type_function_names == {
                "finance_assert_accounting_period_close",
                "finance_assert_event_amendment",
                "finance_guard_late_bank_action_0015",
            }
            assert obsolete_unified_payout_runtime == 0
            with pytest.raises(RuntimeError, match="payroll provenance protection"):
                command.downgrade(config, "base")
        finally:
            engine.dispose()
