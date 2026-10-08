"""The actual browser boundaries validate typed job and native-window states."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest
import test_resident_service as resident_cases
from test_dashboard_transport import authenticated

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.response_contracts import http_response, validate_response

resident = resident_cases.resident


def test_browser_export_status_contract_preserves_native_jobs(resident, monkeypatch, tmp_path):
    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "任务响应合成企业")
    engine = service.engine(company["id"])
    backup = engine.queue_backup(directory=str(tmp_path / "backups"), request_id="backup")
    with engine.store.connection() as connection:
        connection.execute(
            "INSERT INTO jobs(id,kind,payload,status) "
            "VALUES('export','report_export','{}','pending')"
        )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("browser response must use its declared contract")

    monkeypatch.setattr("ai_accounting.kernel.http.wire_money", forbidden)
    path = f"/api/local/report-export/export/status?company_id={company['id']}"
    status, _, _, response = http.request(path, headers=headers)
    assert status == 200, response
    assert response == {
        "schema_version": 1,
        "company_id": company["id"],
        "database_id": company["database_id"],
        "job_id": "export",
        "status": "pending",
        "attempts": 0,
        "error_code": None,
        "error_message": None,
    }
    assert response == http_response("report_export_status", response)
    # Send this real HTTP response through the generated browser validator.
    sample = tmp_path / "report-status.json"
    sample.write_text(json.dumps({"current_report_status": {
        "command": "report_export_status", "response": response,
    }}), encoding="utf-8")
    subprocess.run(
        [shutil.which("node"), "scripts/check-contract-samples.mjs", str(sample)],
        cwd=Path(__file__).resolve().parents[2] / "frontend",
        check=True, capture_output=True, text=True,
    )
    native = service.dispatch("jobs", {"company_id": company["id"]}, session_token=token)
    assert isinstance(native, list) and all(row["result"] is None for row in native)
    assert {row["id"] for row in native} == {"export", backup["job_id"]}
    assert http.request(f"/api/local/jobs?company_id={company['id']}", headers=headers)[0] == 404

    original = Reports.browser_export_status

    def malformed(self, job_id):
        result = original(self, job_id)
        result["attempts"] = "private input must never leak"
        return result

    monkeypatch.setattr(Reports, "browser_export_status", malformed)
    status, _, _, rejected = http.request(path, headers=headers)
    assert status == 500 and rejected["code"] == "response_contract_mismatch"
    assert "private input" not in str(rejected)


def test_export_status_authentication_scope_and_query_boundary(resident, monkeypatch):
    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "任务状态甲合成企业")["id"]
    other = service.catalog.create_company("91310000123456789B", "任务状态乙合成企业")["id"]
    engine = service.engine(company)
    with engine.store.connection() as connection:
        connection.executemany(
            "INSERT INTO jobs(id,kind,payload,status) VALUES(?,?,'{}','pending')",
            [("export", "report_export"), ("backup", "backup")],
        )
    path = f"/api/local/report-export/export/status?company_id={company}"
    assert http.request(path)[0] == 401
    assert http.request(
        path, headers={"Authorization": "Bearer " + token.get_secret_value()}
    )[0] == 200
    assert http.request(path, headers={"Authorization": "Bearer invalid"})[0] == 401
    for query in (
        "",
        "?company_id=",
        "?company_id=missing",
        f"?company_id={company}&company_id={other}",
        f"?company_id={company}&job_id=export",
        f"?company_id={company}&limit=1",
        f"?company_id={company}&path=C:/Windows",
        f"?company_id={company}&carry_forward_fact_id=source",
    ):
        assert http.request(
            "/api/local/report-export/export/status" + query, headers=headers
        )[0] == 400
    for job_id in ("", "nested/job", "a" * 201):
        assert http.request(
            f"/api/local/report-export/{job_id}/status?company_id={company}", headers=headers
        )[0] == 400
    for job_id, selected in (("missing", company), ("backup", company), ("export", other)):
        status, _, _, rejected = http.request(
            f"/api/local/report-export/{job_id}/status?company_id={selected}", headers=headers
        )
        assert status == 404 and rejected["code"] == "unknown_report_job"
    assert http.request(path, headers=headers)[0] == 200
    original = Reports.browser_export_status

    def revoked_during_read(self, job_id):
        result = original(self, job_id)
        service.security.logout(token)
        return result

    monkeypatch.setattr(Reports, "browser_export_status", revoked_during_read)
    assert http.request(path, headers=headers)[0] == 401
    assert http.request(path, headers=headers)[0] == 401


def test_public_security_state_covers_session_request_and_cancel(resident):
    _, _, _, http, _ = resident
    cookies, _ = http.surface()
    status, _, _, session = http.browser("session_status", {}, cookies)
    assert status == 200, session
    assert session["schema_version"] == 1
    assert session["authenticated"] is False and session["provisioned"] is True
    assert validate_response("browser_security_status", session) == session

    status, _, _, request = http.browser("request", {"kind": "login"}, cookies)
    assert status == 200, request
    assert request["schema_version"] == 1 and request["kind"] == "login"
    assert request["status"] in {"starting", "waiting_for_user"}
    assert "password" not in request and "session_token" not in request
    status, _, _, cancelled = http.browser("cancel", {"request_id": request["request_id"]}, cookies)
    assert status == 200, cancelled
    assert cancelled["status"] == "cancelled"
    assert validate_response("browser_security_status", cancelled) == cancelled


@pytest.mark.parametrize("command", ["preview_close_range", "close_range"])
def test_retired_batch_commands_have_no_public_schema_or_dispatch(resident, command):
    service, *_ = resident
    schema = service.dispatch("schema", {"view": "full"})
    assert command not in schema["command_schemas"]
    assert "approve_close_batches" not in str(schema["security_request_schema"])
    assert "closing_batches" not in schema["agent_operating_protocol"]
    with pytest.raises(KernelError) as rejected:
        service.dispatch(command, {})
    assert getattr(rejected.value, "code", None) == "unknown_command"
