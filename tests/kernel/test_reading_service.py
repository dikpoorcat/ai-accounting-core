"""The service exposes compact discovery and owns only the parsing read cache."""

import pytest
from test_local_service import records
from test_local_service import service as service

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.work_context import WorkContext
from ai_accounting.kernel.work_context_contract import WORK_CONTEXT_ADAPTER


def test_service_overview_selected_context_and_full_share_runtime_contracts(service):
    app, company = service
    overview = app.dispatch("schema", {})
    assert overview["view"] == "overview"
    assert "work_context" in overview["commands"]
    assert "work_context" in overview["response_types"]
    assert "command_schemas" not in overview
    selected = app.dispatch("schema", {
        "view": "selected", "commands": ["work_context"], "response_types": ["work_context"],
    })
    assert set(selected["command_schemas"]) == {"work_context"}
    assert selected["response_schemas"] == {"work_context": WORK_CONTEXT_ADAPTER.json_schema()}
    assert "security_request_schema" not in selected
    model = selected["command_schemas"]["work_context"]
    assert model["properties"]["limit"]["default"] == 100
    assert model["properties"]["limit"]["maximum"] == 500
    assert model["properties"]["subject_ids"]["anyOf"][0]["maxItems"] == 500
    engine = app.engine(company)
    engine.save_facts(records(engine)[:1], request_id="reading-seed")
    result = app.dispatch("work_context", {
        "company_id": company, "period": "2026-01", "work_area": "transactions",
    })
    assert result == WORK_CONTEXT_ADAPTER.validate_python(result)
    assert [item["subject_id"] for item in result["items"]] == ["expense-0"]
    assert result["items"][0]["pending"] is True


def test_service_context_fails_closed_without_echoing_private_response(service, monkeypatch):
    app, company = service
    monkeypatch.setattr(WorkContext, "query", lambda *args, **kwargs: {"private-company": "secret"})
    with pytest.raises(KernelError) as failure:
        app.dispatch("work_context", {
            "company_id": company, "period": "2026-01", "work_area": "transactions",
        })
    assert failure.value.code == "response_contract_mismatch"
    assert "secret" not in str(failure.value.response())
    assert "private-company" not in str(failure.value.response())


def test_compact_service_views_do_not_generate_unrelated_security_contracts(service, monkeypatch):
    from ai_accounting.kernel.replay_close import ReplayScopeConfig
    from ai_accounting.kernel.security.native import NativeRequest

    def unexpected(*args, **kwargs):
        raise AssertionError("compact discovery must not generate unrelated full contracts")

    app, _ = service
    monkeypatch.setattr(NativeRequest, "model_json_schema", unexpected)
    monkeypatch.setattr(ReplayScopeConfig, "model_json_schema", unexpected)
    assert app.dispatch("schema", {})["view"] == "overview"
    assert app.dispatch("schema", {
        "view": "selected", "commands": ["work_context"],
    })["commands"] == ["work_context"]


def test_resident_service_reuses_parse_between_commands_but_receipt_rechecks(service, monkeypatch):
    from ai_accounting.kernel import materials

    app, company = service
    evidence = app.engine(company).register_evidence(
        b"name,amount\nfirst,1.00\nsecond,2.00\n", "text/csv", "reading.csv", request_id="raw"
    )["digest"]
    specification = {"format": "csv", "columns": [
        {"column": "A", "role": "context"}, {"column": "B", "role": "amount"},
    ]}
    calls = []
    parse = materials.inspect_bytes

    def inspected(*args, **kwargs):
        calls.append(1)
        return parse(*args, **kwargs)

    monkeypatch.setattr(materials, "inspect_bytes", inspected)
    payload = {
        "company_id": company, "evidence_digest": evidence,
        "specification": specification, "limit": 1,
    }
    first = app.dispatch("inspect_material", payload)
    second = app.dispatch("inspect_material", {**payload, "after": first["next_cursor"]})
    assert len(first["items"]) == len(second["items"]) == 1
    assert second["next_cursor"] is None
    assert len(calls) == 1
    app.dispatch("receive_material", {
        "company_id": company, "subject_id": "reading-source", "data": {
            "period": "2026-01", "evidence_digest": evidence, "category": "transactions",
            "purpose": "business", "specification": specification,
        }, "evidence": [evidence], "expected_revision": 0, "request_id": "receive",
    })
    assert len(calls) == 2
