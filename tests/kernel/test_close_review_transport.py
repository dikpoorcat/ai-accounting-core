"""Real transport keeps owner review tied to one company, month and preview."""

from urllib.parse import urlencode

import pytest
import test_resident_service as resident_cases
from monthly_close_fixture import ready
from test_dashboard_transport import authenticated
from test_resident_service import PASSWORD

from ai_accounting.kernel.close_review import CloseReview
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.read_state import advance_repair_revision
from ai_accounting.kernel.service import LocalService

resident = resident_cases.resident


def prepared_company(resident, *, prepare=True):
    service, _, _, http, _ = resident
    headers, token = authenticated(resident)
    service.security_controller.store.save_session_token(token)
    company = service.catalog.create_company("91310000123456789A", "核对传输合成企业")
    engine = service.engine(company["id"])
    proof = engine.register_evidence(
        b"explicit synthetic monthly owner confirmation",
        "text/plain",
        "合成确认",
        request_id="proof",
    )["digest"]
    ready(engine, proof, first="2026-01", last="2026-01")

    def execute(command, **payload):
        return service.dispatch(
            command, {"company_id": company["id"], **payload}, session_token=token
        )

    def read(**payload):
        return http.request(
            "/api/dashboard/close-review?"
            + urlencode({"company_id": company["id"], "period": "2026-01", **payload}),
            headers=headers,
        )

    preview = (
        execute("preview_close", period="2026-01", owner_confirmation=proof) if prepare else None
    )
    return service, engine, company, proof, token, execute, read, preview


def test_review_uses_exact_preview_and_survives_freeze_and_service_restart(resident, tmp_path):
    service, engine, company, proof, _, execute, read, _ = prepared_company(resident, prepare=False)
    assert read()[3]["state"] == "unprepared"
    preview = execute("preview_close", period="2026-01", owner_confirmation=proof)
    assert preview["review_locator"] == {
        "company_id": company["id"],
        "database_id": company["database_id"],
        "period": "2026-01",
        "preview_digest": preview["digest"],
    }
    status, _, _, shown = read(preview_digest=preview["digest"])
    assert status == 200, shown
    assert shown["state"] == "prepared"
    assert shown["owner_review"]["accounting_summary"]["total_debit_fen"] == "0"
    native = execute("dashboard_close_review", period="2026-01", preview_digest=preview["digest"])
    assert native["owner_review"] == preview["manifest"]["owner_review"]
    assert type(native["owner_review"]["accounting_summary"]["total_debit_fen"]) is int
    detail = read(preview_digest=preview["digest"], section="evidence", limit=1)[3]
    assert detail["preview_digest"] == shown["preview_digest"]
    assert detail["collection"]["page"]["total_count"] >= 1
    assert detail["collection"]["items"][0]["references"][0]["digest"] == proof

    # A new service has no in-memory preview. It must not reconstruct an owner confirmation.
    restarted = LocalService(service.catalog.root)
    restarted_token = restarted.security.login("owner", PASSWORD).session_token
    assert (
        restarted.dispatch(
            "dashboard_close_review",
            {
                "company_id": company["id"],
                "period": "2026-01",
            },
            session_token=restarted_token,
        )["state"]
        == "unprepared"
    )

    request = service.security_controller.request(
        kind="approve_period_close",
        company_id=company["id"],
        database_id=company["database_id"],
        period="2026-01",
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
    )
    status, _, _, approved = resident[3].native(
        "native_execute",
        request_id=request["request_id"],
        password=PASSWORD.get_secret_value(),
    )
    assert status == 200, approved
    closed = execute(
        "close",
        period="2026-01",
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        approval_id=approved["approval_id"],
        request_id="close",
        backup_directory=str(tmp_path / "backup"),
    )
    frozen = read()[3]
    assert (company["id"], company["database_id"], "2026-01") not in service.active_close_previews
    with pytest.raises(KernelError) as retired:
        service.security_controller.request(
            kind="approve_period_close",
            company_id=company["id"],
            database_id=company["database_id"],
            period="2026-01",
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
        )
    assert retired.value.code == "preview_expired"
    with pytest.raises(KernelError) as late_preview:
        service._remember_close_preview(engine, preview, proof)
    assert late_preview.value.code == "preview_expired"
    assert frozen["state"] == "closed" and frozen["close_digest"] == closed["digest"]
    assert frozen["owner_review"] == shown["owner_review"]
    assert frozen["preview_digest"] == preview["digest"]
    outdated = read(preview_digest="0" * 64, section="evidence", limit=1)[3]
    assert outdated["state"] == "stale" and outdated["owner_review"] is None
    assert outdated["collection"] is None
    assert (
        restarted.dispatch(
            "dashboard_close_review",
            {
                "company_id": company["id"],
                "period": "2026-01",
            },
            session_token=restarted_token,
        )["owner_review"]
        == native["owner_review"]
    )
    covered = read(period="2025-12")[3]
    assert covered["state"] == "covered" and covered["owner_review"] is None
    assert covered["covered_by"] == {"period": "2026-01", "digest": closed["digest"]}


def test_replaced_preview_is_never_implicitly_selected_by_old_locator(resident):
    service, engine, company, proof, _, execute, read, first = prepared_company(resident)
    other_proof = engine.register_evidence(
        b"second explicit synthetic owner confirmation",
        "text/plain",
        "第二次确认",
        request_id="proof2",
    )["digest"]
    second = execute("preview_close", period="2026-01", owner_confirmation=other_proof)
    assert first["digest"] != second["digest"]
    status, _, _, stale = read(preview_digest=first["digest"], section="evidence", limit=1)
    assert status == 200 and stale["state"] == "stale"
    assert stale["owner_review"] is None and stale["collection"] is None
    assert read(preview_digest=second["digest"])[3]["state"] == "prepared"
    with pytest.raises(KernelError) as rejected:
        service.require_active_close_preview(
            company["id"], company["database_id"], "2026-01", first["digest"]
        )
    assert rejected.value.code == "preview_expired"
    other = service.catalog.create_company("91310000123456789B", "另一家合成企业")
    assert read(company_id=other["id"], preview_digest=second["digest"])[3]["state"] == "unprepared"
    assert read(period="2026-02", preview_digest=second["digest"])[3]["state"] == "unprepared"


@pytest.mark.parametrize("lane", ["accounting", "material", "management", "read_repair_revision"])
def test_each_revision_invalidates_review_without_exposing_cached_contents(resident, lane):
    _, engine, _, _, _, _, read, preview = prepared_company(resident)
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        if lane == "read_repair_revision":
            advance_repair_revision(connection)
        else:
            connection.execute(f"UPDATE state SET {lane}={lane}+1 WHERE id=1")
        connection.commit()
    response = read(preview_digest=preview["digest"])[3]
    assert response["state"] == "stale" and response["reason"] == "snapshot_changed"
    assert response["owner_review"] is None and response["collection"] is None


def test_review_contract_failure_is_a_server_error_and_has_no_private_value(resident, monkeypatch):
    _, _, _, _, _, _, read, _ = prepared_company(resident)
    original = CloseReview.read

    def malformed(self, *args, **kwargs):
        result = original(self, *args, **kwargs)
        result["owner_review"]["accounting_summary"]["total_debit_fen"] = "private value"
        return result

    monkeypatch.setattr(CloseReview, "read", malformed)
    status, _, _, rejected = read()
    assert status == 500 and rejected["code"] == "response_contract_mismatch"
    assert "private value" not in str(rejected)
