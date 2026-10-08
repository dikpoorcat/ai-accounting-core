"""Draft responses preserve partial JSON while enforcing their own contract."""

import json

import pytest
from test_work_draft_service import service as service

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.response_contracts import RESPONSE_ADAPTERS, validate_response


def response(draft):
    return {
        "schema_version": 1,
        "company_id": "synthetic-company",
        "database_id": "synthetic-database",
        "period": "2026-01",
        "work_area": "transactions",
        "status": "present",
        "revision": "unique-version",
        "draft": draft,
        "warnings": [],
    }


def test_read_contract_preserves_every_omission_and_json_value():
    value = response({
        "candidates": [{
            "id": "candidate", "command": "save_fact",
            "payload": {"kind": "expense", "data": {"amount": 9007199254740993}},
        }],
        "questions": [{"id": "missing", "question": "待确认所属期"}],
    })
    checked = validate_response("read_work_draft", value)
    assert checked == value
    assert isinstance(checked["draft"], dict)
    adapter = RESPONSE_ADAPTERS["read_work_draft"]
    assert adapter.dump_python(checked, mode="json") == value
    assert json.loads(adapter.dump_json(checked)) == value


def test_absent_contract_cannot_carry_a_draft_or_revision():
    value = {**response({}), "status": "absent", "revision": None, "draft": None}
    assert validate_response("read_work_draft", value) == value
    with pytest.raises(KernelError, match="读取结果不符合接口合同"):
        validate_response("read_work_draft", {**value, "draft": {"next_step": "private"}})


def test_contract_failure_does_not_echo_private_draft_keys_or_values():
    value = response({"private-field": "private-material"})
    with pytest.raises(KernelError) as failure:
        validate_response("read_work_draft", value)
    assert failure.value.code == "response_contract_mismatch"
    error = str(failure.value.response())
    assert "private-field" not in error
    assert "private-material" not in error


def test_list_contract_contains_only_scope_without_document_or_revision():
    value = {
        "schema_version": 1, "company_id": "synthetic-company",
        "database_id": "synthetic-database", "limit": 100, "next_cursor": None,
        "items": [{"period": "2026-01", "work_area": "transactions"}],
    }
    assert validate_response("list_work_drafts", value) == value
    with pytest.raises(KernelError):
        validate_response("list_work_drafts", {
            **value, "items": [{**value["items"][0], "revision": "not-read"}],
        })


@pytest.mark.parametrize("command", ["save_work_draft", "delete_work_draft"])
def test_successful_file_change_with_failed_response_requires_revision_reread(
    service, monkeypatch, command
):
    from ai_accounting.kernel import response_contracts

    app, _, scope, _ = service
    if command == "save_work_draft":
        payload = {**scope, "expected_revision": None, "draft": {"next_step": "resume"}}
    else:
        saved = app.dispatch("save_work_draft", {
            **scope, "expected_revision": None, "draft": {"next_step": "resume"},
        })
        payload = {**scope, "expected_revision": saved["revision"]}
    original = response_contracts.validate_response

    def failed_response(name, value):
        if name == command:
            raise KernelError("response_contract_mismatch", "synthetic response failure")
        return original(name, value)

    monkeypatch.setattr(response_contracts, "validate_response", failed_response)
    with pytest.raises(KernelError) as failure:
        app.dispatch(command, payload)
    assert failure.value.code == "work_draft_result_unconfirmed"
    assert failure.value.response()["action"] == "read_work_draft"
    restored = app.dispatch("read_work_draft", scope)
    if command == "save_work_draft":
        assert restored["status"] == "present"
        assert restored["draft"] == payload["draft"]
        assert restored["revision"]
    else:
        assert restored["status"] == "absent"
