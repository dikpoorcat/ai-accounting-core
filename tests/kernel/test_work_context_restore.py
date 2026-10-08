"""Combined restoration uses one company binding and authenticates both sources."""

import copy
import json

import pytest
from pydantic import ValidationError
from test_work_draft_service import candidate, save
from test_work_draft_service import service as service

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.work_context import WorkContext
from ai_accounting.kernel.work_context_contract import WORK_CONTEXT_ADAPTER
from ai_accounting.kernel.work_drafts import WorkDraftStore


def test_combined_read_matches_independent_reads_and_preserves_omitted_draft_fields(service):
    app, _, scope, _ = service
    draft = {
        "candidates": [candidate(scope)],
        "questions": [{"id": "creditor", "question": "欠供应商还是员工？", "answer": "供应商"}],
    }
    saved = save(app, scope, draft)
    separate_context = app.dispatch("work_context", scope)
    separate_draft = app.dispatch("read_work_draft", scope)
    combined = app.dispatch("work_context", {**scope, "include_work_draft": True})
    assert combined["schema_version"] == 2
    assert combined["work_draft"] == separate_draft
    assert combined["work_draft"]["revision"] == saved["revision"]
    assert combined["work_draft"]["draft"] == draft
    assert "next_step" not in combined["work_draft"]["draft"]
    for key in separate_context.keys() - {"page_semantics"}:
        assert combined[key] == separate_context[key]
    assert "不属于同一事务" in combined["page_semantics"]
    assert WORK_CONTEXT_ADAPTER.validate_python(combined) == combined


def test_combined_restore_reuses_one_bind_and_no_public_draft_dispatch(service, monkeypatch):
    app, _, scope, _ = service
    bound = []
    original_bind = app.catalog.bind
    original_dispatch = app._dispatch

    def bind(*args, **kwargs):
        bound.append(args[0])
        return original_bind(*args, **kwargs)

    def dispatch(command, *args, **kwargs):
        assert command != "read_work_draft"
        return original_dispatch(command, *args, **kwargs)

    monkeypatch.setattr(app.catalog, "bind", bind)
    monkeypatch.setattr(app, "_dispatch", dispatch)
    result = app.dispatch("work_context", {**scope, "include_work_draft": True})
    assert result["work_draft"]["status"] == "absent"
    assert bound == [scope["company_id"]]


def test_plain_and_later_pages_never_read_draft_file(service, monkeypatch):
    app, _, scope, _ = service
    save(app, scope, {"next_step": "continue"})

    def forbidden(*args, **kwargs):
        raise AssertionError("regular material reads must not open work drafts")

    monkeypatch.setattr(WorkDraftStore, "read", forbidden)
    for payload in (scope, {**scope, "include_work_draft": False}):
        assert "work_draft" not in app.dispatch("work_context", payload)
    with pytest.raises(KernelError) as error:
        app.dispatch("work_context", {**scope, "include_work_draft": True, "cursor": "unused"})
    assert error.value.code == "work_context_draft_first_page_only"


def test_combined_first_page_cursor_continues_without_work_draft(service, monkeypatch):
    from test_work_context import expense

    from ai_accounting.kernel.entities import Entities

    app, _, scope, _ = service
    engine = app.engine(scope["company_id"])
    engine.proof = engine.register_evidence(
        b"synthetic", "text/plain", "proof", request_id="proof"
    )["digest"]
    engine.vendor = Entities(engine).register_entity(
        "organization", {"display_name": "Synthetic vendor"}, source="owner", request_id="vendor"
    )["entity_id"]
    expense(engine, "one")
    expense(engine, "two")
    save(app, scope, {"next_step": "continue"})
    first = app.dispatch("work_context", {**scope, "include_work_draft": True, "limit": 1})
    assert first["work_draft"]["status"] == "present"

    def forbidden(*args, **kwargs):
        raise AssertionError("subsequent page repeated draft read")

    monkeypatch.setattr(WorkDraftStore, "read", forbidden)
    second = app.dispatch("work_context", {**scope, "limit": 1, "cursor": first["next_cursor"]})
    assert second["items"] and "work_draft" not in second
    assert first["items"][0]["fact_id"] != second["items"][0]["fact_id"]


def test_combined_restore_scope_and_restart(service):
    app, _, scope, start = service
    draft = {"next_step": "reuse saved owner answer"}
    saved = save(app, scope, draft)
    restarted, _ = start()
    result = restarted.dispatch("work_context", {**scope, "include_work_draft": True})
    assert result["work_draft"]["revision"] == saved["revision"]
    assert result["work_draft"]["draft"] == draft
    other_company = app.dispatch(
        "create_company", {"taxpayer_id": "91310000123456789B", "name": "Synthetic second company"}
    )["id"]
    for changes in ({"period": "2026-03"}, {"work_area": "tax"}, {"company_id": other_company}):
        other = restarted.dispatch("work_context", {**scope, **changes, "include_work_draft": True})
        assert other["work_draft"]["status"] == "absent"


@pytest.mark.parametrize("damage", ["corrupt", "identity", "format"])
def test_combined_restore_preserves_bad_draft_and_plain_read_remains_available(service, damage):
    app, _, scope, _ = service
    save(app, scope, {"next_step": "saved"})
    path = next((app.catalog.root / ".work-drafts").rglob("*.json"))
    envelope = json.loads(path.read_bytes())
    if damage == "corrupt":
        path.write_bytes(b"invalid JSON")
    else:
        if damage == "identity":
            envelope["identity"]["database_id"] = "wrong-database"
        else:
            envelope["format"] = "unsupported-format"
        path.write_text(json.dumps(envelope), encoding="utf-8")
    damaged_bytes = path.read_bytes()
    with pytest.raises(KernelError) as error:
        app.dispatch("work_context", {**scope, "include_work_draft": True})
    expected = {
        "corrupt": "work_draft_corrupt", "identity": "work_draft_identity_mismatch",
        "format": "work_draft_format_unsupported",
    }
    assert error.value.code == expected[damage]
    assert path.read_bytes() == damaged_bytes
    assert "work_draft" not in app.dispatch("work_context", scope)


def test_combined_draft_response_keeps_nested_validation(service, monkeypatch):
    app, _, scope, _ = service
    original = WorkContext.query

    def damaged(self, **kwargs):
        result = original(self, **kwargs)
        result["work_draft"]["revision"] = "incorrect-for-absent-status"
        return result

    result = app.dispatch("work_context", {**scope, "include_work_draft": True})
    damaged_copy = copy.deepcopy(result)
    damaged_copy["work_draft"]["undeclared"] = "reject"
    with pytest.raises(ValidationError):
        WORK_CONTEXT_ADAPTER.validate_python(damaged_copy)
    monkeypatch.setattr(WorkContext, "query", damaged)
    with pytest.raises(KernelError) as error:
        app.dispatch("work_context", {**scope, "include_work_draft": True})
    assert error.value.code == "response_contract_mismatch"
