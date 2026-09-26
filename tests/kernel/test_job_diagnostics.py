"""Public job state never returns local exception text."""

from __future__ import annotations

import errno
import sqlite3

import pytest
from schema_fixture import test_bundle
from test_engine import engine as engine  # noqa: F401

from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError, NeedsInformation, Registry
from ai_accounting.kernel.diagnostics import error_response, job_error_code, job_error_message
from ai_accounting.kernel.resolution import validate_resolution


def test_job_error_classification_has_stable_safe_text():
    assert job_error_code(OSError("secret path C:/private/owner")) == "storage_unavailable"
    assert job_error_code(ValueError("secret submitted content")) == "job_failed"
    assert job_error_code(sqlite3.OperationalError("secret database detail")) == "job_failed"
    assert "secret" not in job_error_message("job_failed")
    assert job_error_message("interrupted_retry_exhausted")
    for exception in (ValueError("secret"), TypeError("secret"), KeyError("secret")):
        assert error_response(exception)["code"] == "internal_error"
    assert error_response(KernelError("invalid_command", "无效命令"))["code"] == "invalid_command"


def test_disk_full_keeps_its_actionable_code_without_exposing_the_path():
    failure = OSError(errno.ENOSPC, "private file contents", "C:/private/owner")
    assert error_response(failure)["code"] == "storage_full"
    assert job_error_code(failure) == "storage_full"
    assert "存储空间不足" in job_error_message(job_error_code(failure))
    assert "private" not in str(error_response(failure))


def test_job_listing_and_explicit_retry_expose_only_safe_diagnostic(engine):
    with engine.store.connection() as connection:
        connection.execute(
            "INSERT INTO jobs(id,kind,payload,status,attempts,last_error,error_code) "
            "VALUES('failed','portable_backup','{}','failed',3,?,?)",
            ("C:/private/owner secret", "storage_unavailable"),
        )
        connection.commit()
    failed = engine.jobs(job_id="failed")[0]
    assert failed["error_code"] == "storage_unavailable"
    assert failed["error_message"] == job_error_message("storage_unavailable")
    assert "last_error" not in failed
    retried = engine.retry_job("failed", request_id="retry")
    assert retried["previous_error_code"] == "storage_unavailable"
    assert retried["previous_error_message"] == failed["error_message"]
    assert "C:/private" not in str(retried)
    assert engine.jobs(job_id="failed")[0]["error_code"] is None


def test_needs_information_normalizes_multiple_issues_and_explicit_resolution(engine):
    issues = [
        {
            "field": "recognition_period", "message": "需要月份",
            "semantics": "accounting", "allowed_precision": ["month"],
            "reusable_sources": ["source-a"],
        },
        {
            "field": "actual_date", "message": "需要实际日期",
            "semantics": "accounting", "allowed_precision": ["day"],
            "reusable_sources": [],
        },
    ]
    expected = {"status": "needs_information", "fact_issues": issues}
    assert NeedsInformation(issues).response() == expected
    assert KernelError("needs_information", "待补", fact_issues=issues).response() == expected
    with pytest.raises(ValueError, match="fact_issues"):
        KernelError("needs_information", "待补", field="amount")
    assert NeedsInformation(
        issues, resolution={"fact_kind": "test_charge"}, registry=engine.store.registry
    ).response()["resolution"] == {"fact_kind": "test_charge"}
    assert validate_resolution({"candidates": ["a", "b"]}) == {"candidates": ["a", "b"]}


def test_registration_reports_all_missing_fields_with_registered_fact_target(engine):
    from test_engine import evidence

    with pytest.raises(NeedsInformation) as failure:
        engine.save_fact(
            "test_charge", "charge", {}, evidence=(evidence(engine),),
            expected_revision=0, request_id="missing",
        )
    response = failure.value.response()
    assert response["status"] == "needs_information"
    assert {issue["field"] for issue in response["fact_issues"]} == {"period", "amount"}
    assert response["resolution"] == {"fact_kind": "test_charge"}
    assert engine.request_result("missing")["status"] == "unknown"


def test_command_validation_reports_all_missing_registered_fields(engine):
    models = command_models(engine.store.registry)
    with pytest.raises(NeedsInformation) as failure:
        validate_command(
            models, "save_fact",
            {"company_id": "company-a", "request_id": "missing", "kind": "test_charge",
             "subject_id": "charge", "data": {}, "evidence": ["source"], "expected_revision": 0},
            registry=engine.store.registry,
        )
    response = failure.value.response()
    assert {issue["field"] for issue in response["fact_issues"]} == {"period", "amount"}
    assert response["resolution"] == {"fact_kind": "test_charge"}
    period_issue = next(item for item in response["fact_issues"] if item["field"] == "period")
    assert period_issue["allowed_precision"] == ["month"]


def test_management_fact_missing_fields_keep_management_semantics():
    from ai_accounting.kernel.workflow import ExternalCompletion

    registry = Registry()
    registry.register(ExternalCompletion)
    with pytest.raises(NeedsInformation) as failure:
        validate_command(
            command_models(registry), "save_fact",
            {
                "company_id": "company-a", "request_id": "missing-management",
                "kind": "external_completion", "subject_id": "completion",
                "data": {"period": "2026-02"}, "evidence": ["source"],
                "expected_revision": 0,
            },
            registry=registry,
        )
    issues = failure.value.response()["fact_issues"]
    assert {issue["field"] for issue in issues} >= {
        "obligation_id", "completion_status", "date_status"
    }
    assert all(issue["semantics"] == "management" for issue in issues)


def test_catalog_operations_publish_safe_code_without_changing_catalog_schema(engine, tmp_path):
    catalog = Catalog(tmp_path / "catalog", test_bundle(engine.store.registry))
    with catalog.connection() as connection:
        connection.execute(
            "INSERT INTO company_operation(id,taxpayer_id,kind,payload,status,attempts,last_error) "
            "VALUES('op','91310000123456789A','create','{}','failed',1,?)",
            ("C:/private/secret",),
        )
        connection.commit()
    operation = catalog.operations()[0]
    assert operation["error_code"] == "operation_failed"
    assert operation["error_message"]
    assert "last_error" not in operation
    assert "private" not in str(operation)
