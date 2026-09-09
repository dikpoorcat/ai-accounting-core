from __future__ import annotations

import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import ProgrammingError

from ai_accounting import company_cli, mcp_server
from ai_accounting.schema_readiness import (
    DatabaseSchemaError,
    require_current_schema,
    schema_state,
)


@pytest.mark.parametrize(
    ("revisions", "expected_code"),
    [
        ([], "DATABASE_SCHEMA_UNSUPPORTED"),
        (["0001_business_baseline_v4"], "DATABASE_SCHEMA_UPGRADE_REQUIRED"),
        (["0004_fact_precision"], "DATABASE_SCHEMA_UNSUPPORTED"),
        (["future_revision"], "DATABASE_SCHEMA_UNSUPPORTED"),
        (["0002_atomic_corrections", "another_head"], "DATABASE_SCHEMA_UNSUPPORTED"),
        (["0002_atomic_corrections"], "DATABASE_SCHEMA_UPGRADE_REQUIRED"),
        (["0003_payroll_correction_uses"], "DATABASE_SCHEMA_UPGRADE_REQUIRED"),
        (["0004_payroll_dependency_scope"], "DATABASE_SCHEMA_UPGRADE_REQUIRED"),
        (["0005_payroll_provenance"], None),
        (["0001_catalog_baseline_v2"], "DATABASE_SCHEMA_UNSUPPORTED"),
    ],
)
def test_schema_check_never_creates_or_stamps_database(
    revisions, expected_code, tmp_path, monkeypatch
):
    # Resolve the installed migration tree even when the caller uses another cwd.
    monkeypatch.chdir(tmp_path)
    engine = create_engine("sqlite://")
    with engine.begin() as connection:
        if revisions:
            connection.execute(text("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)"))
            for revision in revisions:
                connection.execute(
                    text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": revision}
                )
        before = connection.exec_driver_sql("SELECT name, sql FROM sqlite_master").all()
        if expected_code:
            with pytest.raises(DatabaseSchemaError) as caught:
                require_current_schema(connection)
            result = caught.value.result()
            assert result["errors"] == [expected_code]
            assert result["data"]["failure_kind"] == "deployment_mismatch"
            assert result["data"]["database_schema"]["actual_revisions"] == sorted(revisions)
            assert "missing_information" not in result
        else:
            require_current_schema(connection)
        assert connection.exec_driver_sql("SELECT name, sql FROM sqlite_master").all() == before
    engine.dispose()


def test_schema_cli_rejects_outdated_database_without_login_or_writes(
    tmp_path, monkeypatch, capsys
):
    engine = create_engine(f"sqlite:///{tmp_path / 'deployed.db'}")
    with engine.begin() as connection:
        connection.execute(text("CREATE TABLE alembic_version (version_num TEXT PRIMARY KEY)"))
        connection.execute(text("INSERT INTO alembic_version VALUES ('0001_business_baseline_v4')"))
    monkeypatch.setattr(
        company_cli,
        "get_settings",
        lambda: SimpleNamespace(
            multi_company_enabled=False,
            runtime_database_url=lambda: engine.url,
        ),
    )
    with pytest.raises(SystemExit) as caught:
        company_cli.main(["check-schema"])
    assert caught.value.code == 1
    assert '"upgrade_available": true' in capsys.readouterr().out
    with engine.connect() as connection:
        assert schema_state(connection)["actual_revisions"] == ["0001_business_baseline_v4"]
    engine.dispose()


@pytest.mark.parametrize("sqlstate", ["42703", "42P01"])
def test_missing_database_objects_are_deployment_diagnostics_not_fact_questions(sqlstate, caplog):
    original = Exception("private database error")
    original.sqlstate = sqlstate
    result = mcp_server._invalid(ProgrammingError("private SQL", {"secret": "private"}, original))
    assert result["errors"] == ["DATABASE_SCHEMA_MISMATCH"]
    assert result["data"]["diagnostic"]["sqlstate"] == sqlstate
    assert uuid.UUID(result["data"]["diagnostic_id"])
    assert result["data"]["next_action"] == "inspect_database_deployment"
    assert "private" not in str(result) + caplog.text
    assert "missing_information" not in result
