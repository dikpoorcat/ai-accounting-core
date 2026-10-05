"""One stored-contract check and one public response check per actual read."""

import copy
import json
from dataclasses import replace

import pytest
from monthly_close_fixture import ready
from test_close_review_transport import prepared_company
from test_close_review_transport import resident as resident_fixture
from test_resident_service import PASSWORD

from ai_accounting.kernel import close_review as reviews
from ai_accounting.kernel.close_preview import _PUBLIC_OWNER_REVIEW_ADAPTER
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.response_contracts import http_response
from ai_accounting.kernel.security.window import _owner_review_text

resident = resident_fixture


def freeze(resident, prepared, tmp_path, *, period="2026-01"):
    service, _, company, proof, _, execute, _, preview = prepared
    request = service.security_controller.request(
        kind="approve_period_close",
        company_id=company["id"],
        database_id=company["database_id"],
        period=period,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
    )
    status, _, _, approved = resident[3].native(
        "native_execute", request_id=request["request_id"], password=PASSWORD.get_secret_value()
    )
    assert status == 200, approved
    execute(
        "close",
        period=period,
        owner_confirmation=proof,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        approval_id=approved["approval_id"],
        request_id="close",
        backup_directory=str(tmp_path / "backup"),
    )


@pytest.mark.parametrize("state", ["prepared", "closed", "covered", "unprepared", "stale"])
def test_actual_review_states_validate_source_and_public_contract_once(
    resident, tmp_path, monkeypatch, state
):
    prepared = prepared_company(resident, prepare=state != "unprepared")
    service, engine, company, proof, _, execute, read, preview = prepared
    if state == "closed":
        freeze(resident, prepared, tmp_path)
    elif state == "covered":
        ready(engine, proof, first="2026-02", last="2026-02")
        second = execute("preview_close", period="2026-02", owner_confirmation=proof)
        freeze(resident, (*prepared[:-1], second), tmp_path, period="2026-02")
    elif state == "stale":
        with engine.store.connection() as connection:
            connection.execute("UPDATE state SET management=management+1 WHERE id=1")
            connection.commit()
    adapters = {
        "source": reviews._OWNER_REVIEW_ADAPTER,
        "public": reviews.DASHBOARD_CLOSE_REVIEW_ADAPTER,
        "summary": _PUBLIC_OWNER_REVIEW_ADAPTER,
    }
    original = type(adapters["source"]).validate_python
    counts = {key: 0 for key in adapters}

    def counted(self, *args, **kwargs):
        for name, adapter in adapters.items():
            if self is adapter:
                counts[name] += 1
        return original(self, *args, **kwargs)

    monkeypatch.setattr(type(adapters["source"]), "validate_python", counted)

    expected = {
        "source": int(state == "closed"),
        "public": 1,
        "summary": int(state in {"prepared", "stale"}),
    }
    native = execute("dashboard_close_review", period="2026-01")
    assert native["state"] == state and counts == expected
    counts.update(source=0, public=0, summary=0)
    status, _, _, wire = read()
    assert status == 200 and wire["state"] == state and counts == expected
    assert (wire["owner_review"] is None) == (native["owner_review"] is None)
    if native["owner_review"] is not None:
        assert wire["owner_review"]["amounts"]["month_expense_fen"] == "0"
        assert type(native["owner_review"]["amounts"]["month_expense_fen"]) is int
        assert wire["preview_digest"] == native["preview_digest"] == preview["digest"]


def test_public_builder_error_reaches_common_safe_response_boundary(resident, monkeypatch):
    service, engine, _, _, _, execute, read, _ = prepared_company(resident)
    original = reviews.CloseReview._response

    def malformed(self, **fields):
        result = original(self, **fields)
        result["owner_review"]["amounts"]["month_expense_fen"] = "private builder value"
        result["owner_review"]["private builder field"] = "private extra value"
        return result

    monkeypatch.setattr(reviews.CloseReview, "_response", malformed)
    with pytest.raises(KernelError) as failure:
        execute("dashboard_close_review", period="2026-01")
    assert failure.value.code == "response_contract_mismatch"
    status, _, _, result = read()
    assert status == 500 and result["code"] == "response_contract_mismatch"
    assert set(result["paths"]) == {
        "owner_review.amounts.month_expense_fen",
        "owner_review.<extra>",
    }
    assert "private" not in json.dumps(result)
    with pytest.raises(KernelError) as independent_http:
        http_response(
            "dashboard_close_review", reviews.CloseReview(service, engine).read("2026-01")
        )
    assert independent_http.value.code == "response_contract_mismatch"


def test_stored_review_damage_is_not_hidden_by_stale_or_public_projection(resident):
    service, engine, company, _, _, execute, read, preview = prepared_company(resident)
    stored, _ = service.require_active_close_preview(
        company["id"], company["database_id"], "2026-01", preview["digest"]
    )
    service.close_previews[(company["id"], company["database_id"], preview["digest"])] = replace(
        stored, summary_bytes=b"private invalid source"
    )
    with engine.store.connection() as connection:
        connection.execute("UPDATE state SET management=management+1 WHERE id=1")
        connection.commit()
    with pytest.raises(KernelError) as failure:
        execute("dashboard_close_review", period="2026-01")
    assert failure.value.code == "content_integrity_failed"
    status, _, _, result = read()
    assert status == 500 and result["code"] == "content_integrity_failed"
    assert "private invalid source" not in json.dumps(result)


def test_independent_public_projection_and_native_window_still_validate_source_once(
    resident, monkeypatch
):
    _, _, _, _, _, _, _, preview = prepared_company(resident)
    stored = preview["manifest"]["owner_review"]
    original = reviews.require_owner_review
    calls = []

    def counted(value):
        calls.append(value)
        return original(value)

    monkeypatch.setattr(reviews, "require_owner_review", counted)
    public = reviews.public_owner_review(stored)
    assert len(calls) == 1 and public["amounts"]["month_expense_fen"] == 0
    calls.clear()
    assert "本月费用：¥0.00" in _owner_review_text(stored)
    assert len(calls) == 1
    broken = copy.deepcopy(stored)
    broken["presentation_contract"] = "invalid"
    for project in (reviews.public_owner_review, _owner_review_text):
        with pytest.raises(KernelError) as failure:
            project(broken)
        assert failure.value.code == "content_integrity_failed"


@pytest.mark.parametrize("amount", [None, 2**53 + 1, 2**63 - 1])
def test_public_money_precision_is_identical_across_native_and_browser_boundaries(
    resident, monkeypatch, amount
):
    service, _, company, proof, token, execute, read, _ = prepared_company(resident, prepare=False)
    original = reviews._public_owner_review

    def with_money(review):
        result = original(review)
        result["amounts"]["funds_total_fen"] = amount
        return result

    # A narrow serialization-contract probe; this does not claim the saved
    # source review itself contains a naturally produced extreme amount.
    monkeypatch.setattr(reviews, "_public_owner_review", with_money)
    preview = execute("preview_close", period="2026-01", owner_confirmation=proof)
    native = execute("dashboard_close_review", period="2026-01", preview_digest=preview["digest"])
    status, _, _, private = resident[3].request(
        "/api/command",
        {
            "command": "dashboard_close_review",
            "payload": {
                "company_id": company["id"],
                "period": "2026-01",
                "preview_digest": preview["digest"],
            },
        },
        headers={
            "X-Local-Capability": resident[2],
            "Authorization": "Bearer " + token.get_secret_value(),
        },
    )
    assert status == 200 and private == native
    status, _, _, browser = read(preview_digest=preview["digest"])
    assert status == 200 and browser["preview_digest"] == private["preview_digest"]
    assert native["owner_review"]["amounts"]["funds_total_fen"] == amount
    assert amount is None or type(private["owner_review"]["amounts"]["funds_total_fen"]) is int
    expected = None if amount is None else str(amount)
    assert browser["owner_review"]["amounts"]["funds_total_fen"] == expected
    wire = service.dispatch(
        "dashboard_close_review",
        {"company_id": company["id"], "period": "2026-01"},
        session_token=token,
        response_format="http_json",
    )
    assert json.loads(wire) == browser
