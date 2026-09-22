"""The actual browser boundaries validate typed job and native-window states."""

import pytest
import test_resident_service as resident_cases
from test_dashboard_transport import authenticated

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.response_contracts import http_response, validate_response

resident = resident_cases.resident


def test_browser_jobs_are_projected_without_changing_native_results(
    resident, monkeypatch, tmp_path
):
    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    company = service.catalog.create_company("91310000123456789A", "任务响应合成企业")
    engine = service.engine(company["id"])
    queued = engine.queue_backup(directory=str(tmp_path / "backups"), request_id="backup")

    def forbidden(*_args, **_kwargs):
        raise AssertionError("browser response must use its declared money contract")

    monkeypatch.setattr("ai_accounting.kernel.http.wire_money", forbidden)
    status, _, _, response = http.request(
        f"/api/local/jobs?company_id={company['id']}&job_id={queued['job_id']}", headers=headers
    )
    assert status == 200, response
    assert response["schema_version"] == 1
    assert response["company_id"] == company["id"]
    assert response["database_id"] == company["database_id"]
    assert len(response["items"]) == 1
    assert "result" not in response["items"][0]
    assert response["items"][0]["attempts"] == 0
    assert response == http_response("browser_jobs", response)

    native = service.dispatch("jobs", {"company_id": company["id"]}, session_token=token)
    assert isinstance(native, list) and native[0]["result"] is None

    original = Reports.browser_job_results

    def malformed(self, jobs):
        rows = original(self, jobs)
        rows[0]["attempts"] = "private input must never leak"
        return rows

    monkeypatch.setattr(Reports, "browser_job_results", malformed)
    status, _, _, rejected = http.request(
        f"/api/local/jobs?company_id={company['id']}", headers=headers
    )
    assert status == 500 and rejected["code"] == "response_contract_mismatch"
    assert "private input" not in str(rejected)


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
    schema = service.dispatch("schema", {})
    assert command not in schema["command_schemas"]
    assert "approve_close_batches" not in str(schema["security_request_schema"])
    assert "closing_batches" not in schema["agent_operating_protocol"]
    with pytest.raises(KernelError) as rejected:
        service.dispatch(command, {})
    assert getattr(rejected.value, "code", None) == "unknown_command"
