"""Owner review locators share the brief snapshot and defer the review body."""

import hmac
import json
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from hashlib import sha256
from threading import Event

import pytest
from monthly_close_fixture import ready
from pydantic import SecretStr

from ai_accounting.kernel import close_storage, dashboard
from ai_accounting.kernel.close_review import CloseReview
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.service import LocalService
from ai_accounting.kernel.types import canonical

PERIOD = "2026-01"


@pytest.fixture
def owner_book(tmp_path):
    # This fixture has no HTTP server, daemon, native window or real data.
    service = LocalService(tmp_path / "synthetic-owner-review", enable_read_pool=True)
    password = SecretStr("Synthetic-owner-review-123")
    service.security.provision("owner", password)
    token = service.security.login("owner", password).session_token
    company = service.catalog.create_company("91310000123456789A", "合成核对企业")
    engine = service.engine(company["id"])
    proof = engine.register_evidence(
        b"Synthetic explicit owner confirmation", "text/plain", "synthetic", request_id="proof"
    )["digest"]
    ready(engine, proof, first=PERIOD, last=PERIOD)

    def execute(command, **payload):
        return service.dispatch(
            command, {"company_id": company["id"], **payload}, session_token=token
        )

    try:
        yield service, engine, company, proof, token, execute
    finally:
        service.close()


def preview(book, *, period=PERIOD, proof=None):
    return book[-1]("preview_close", period=period, owner_confirmation=proof or book[3])


def brief(book):
    return book[-1]("dashboard_brief", period=PERIOD)


def advance(engine, lane):
    assert lane in {"accounting", "material", "management", "read_repair_revision"}
    with engine.store.connection() as connection:
        connection.execute(f"UPDATE state SET {lane}={lane}+1 WHERE id=1")
        connection.commit()


def test_only_exact_ready_intent_is_requested_and_wire_contract_matches(owner_book, monkeypatch):
    service, _, company, _, token, _ = owner_book
    assert brief(owner_book)["data"]["owner_review_request"] is None
    selected = preview(owner_book)

    def forbidden(*_args, **_kwargs):
        pytest.fail("brief requested a full owner review")

    monkeypatch.setattr(CloseReview, "read", forbidden)
    response = brief(owner_book)
    assert response["schema_version"] == 15
    assert response["data"]["month_state"] == "open"
    assert response["data"]["owner_review_request"] == {"preview_digest": selected["digest"]}
    for response_format in ("http", "http_json"):
        wire = service.dispatch(
            "dashboard_brief", {"company_id": company["id"], "period": PERIOD},
            session_token=token, response_format=response_format,
        )
        if response_format == "http_json":
            wire = json.loads(wire)
        assert wire["data"]["owner_review_request"] == response["data"]["owner_review_request"]
        assert wire["data"]["month_state"] == "open"
    malformed = {
        **response,
        "data": {**response["data"], "owner_review_request": {"preview_digest": "short"}},
    }
    with pytest.raises(KernelError) as rejected:
        validate_response("dashboard_brief", malformed)
    assert rejected.value.code == "response_contract_mismatch"
    properties = service.command_models["dashboard_brief"].json_schema()["properties"]
    assert "owner_review_request" not in properties


@pytest.mark.parametrize("lane", ["accounting", "material", "management", "read_repair_revision"])
def test_changed_read_versions_remove_request(owner_book, lane):
    preview(owner_book)
    advance(owner_book[1], lane)
    assert brief(owner_book)["data"]["owner_review_request"] is None


@pytest.mark.parametrize("binding", ["company_id", "database_id", "period", "preview_digest"])
def test_invalid_intent_binding_is_a_content_error_even_when_stale(owner_book, binding):
    service, engine, company, _, _, _ = owner_book
    selected = preview(owner_book)
    key = (company["id"], company["database_id"], selected["digest"])
    service.close_previews[key] = replace(service.close_previews[key], **{binding: "wrong-binding"})
    advance(engine, "management")
    with pytest.raises(KernelError) as rejected:
        brief(owner_book)
    assert rejected.value.code == "content_integrity_failed"


def test_invalid_summary_signature_remains_a_content_error(owner_book):
    service, _, company, _, _, _ = owner_book
    selected = preview(owner_book)
    key = (company["id"], company["database_id"], selected["digest"])
    service.close_previews[key] = replace(service.close_previews[key], signature=b"bad signature")
    with pytest.raises(KernelError) as rejected:
        brief(owner_book)
    assert rejected.value.code == "content_integrity_failed"


def test_authenticated_ai_reviewing_summary_does_not_request_owner_action(owner_book):
    service, _, company, _, _, _ = owner_book
    selected = preview(owner_book)
    key = (company["id"], company["database_id"], selected["digest"])
    intent = service.close_previews[key]
    summary = json.loads(intent.summary_bytes)
    summary["status"] = "ai_reviewing"
    encoded = canonical(summary).encode("utf-8")
    intent = replace(intent, summary_bytes=encoded, summary_sha256=sha256(encoded).digest())
    service.close_previews[key] = replace(
        intent, signature=hmac.digest(service._close_preview_key, intent._signed_bytes(), "sha256")
    )
    assert brief(owner_book)["data"]["owner_review_request"] is None


def test_replaced_preview_keeps_the_old_locator_stale(owner_book):
    _, engine, _, _, _, execute = owner_book
    first = preview(owner_book)
    locator = brief(owner_book)["data"]["owner_review_request"]
    proof = engine.register_evidence(
        b"Synthetic replacement owner confirmation", "text/plain", "replacement",
        request_id="replacement",
    )["digest"]
    second = preview(owner_book, proof=proof)
    assert second["digest"] != first["digest"]
    selected = execute("dashboard_close_review", period=PERIOD, **locator)
    assert selected["state"] == "stale" and selected["owner_review"] is None
    assert selected["preview_digest"] == first["digest"]
    assert brief(owner_book)["data"]["owner_review_request"] == {"preview_digest": second["digest"]}


@pytest.mark.parametrize("state", ["closed", "covered"])
def test_closed_and_covered_briefs_do_not_decode_owner_review(owner_book, state, monkeypatch):
    _, engine, _, proof, _, _ = owner_book
    period = PERIOD if state == "closed" else "2026-02"
    if state == "covered":
        ready(engine, proof, first=period, last=period)
    prepared = Periods(engine).preview_close(period, owner_confirmation=proof)
    Periods(engine).close(
        period, owner_confirmation=proof, preview_digest=prepared["digest"],
        epochs=prepared["epochs"], request_id="synthetic-close",
    )
    original = close_storage.read_section

    def bounded(connection, header, name, **kwargs):
        assert name != "owner_review", "brief decoded a dedicated frozen review directory"
        return original(connection, header, name, **kwargs)

    monkeypatch.setattr(close_storage, "read_section", bounded)
    monkeypatch.setattr(
        CloseReview, "read", lambda *_a, **_k: pytest.fail("unexpected full review")
    )
    response = brief(owner_book)
    assert response["data"]["month_state"] == state
    assert response["data"]["owner_review_request"] is None
    assert response["data"]["owner_tasks"] == []


def test_concurrent_change_cannot_attach_new_intent_to_old_money(owner_book, monkeypatch):
    selected = preview(owner_book)
    expected = brief(owner_book)
    reached, changed = Event(), Event()
    original = dashboard._brief_amounts

    def pause(snapshot):
        result = original(snapshot)
        reached.set()
        assert changed.wait(10)
        return result

    with monkeypatch.context() as patch:
        patch.setattr(dashboard, "_brief_amounts", pause)
        with ThreadPoolExecutor(max_workers=1) as executor:
            pending = executor.submit(brief, owner_book)
            try:
                assert reached.wait(10)
                advance(owner_book[1], "material")
            finally:
                changed.set()
            response = pending.result(timeout=10)
    assert response["read_context"] == expected["read_context"]
    assert response["data"]["owner_review_request"] == {"preview_digest": selected["digest"]}
    expired = owner_book[-1](
        "dashboard_close_review", period=PERIOD, preview_digest=selected["digest"]
    )
    assert expired["state"] == "stale" and expired["owner_review"] is None
    assert brief(owner_book)["data"]["owner_review_request"] is None


def test_empty_brief_keeps_none_data_and_context_version(owner_book):
    service, _, _, _, token, _ = owner_book
    empty = service.catalog.create_company("91310000123456789B", "合成空企业")
    response = service.dispatch("dashboard_brief", {"company_id": empty["id"]}, session_token=token)
    assert response["schema_version"] == 15 and response["data"] is None
    context = service.dispatch(
        "dashboard_context", {"company_id": empty["id"]}, session_token=token
    )
    assert context["schema_version"] == 3
