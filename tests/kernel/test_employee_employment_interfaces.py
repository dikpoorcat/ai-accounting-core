"""Employee events cross the existing service, MCP, draft and backup boundaries."""

import json
from types import SimpleNamespace

import pytest
from jsonschema import Draft202012Validator
from test_work_draft_service import service as service  # noqa: F401

from ai_accounting.kernel.backup import BackupError, create_portable, verify_file, verify_portable
from ai_accounting.kernel.content_history_context import source_canonical, source_digest
from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.work_context_contract import WORK_CONTEXT_ADAPTER

COMMANDS = ["register_employee", "change_employee_employment", "employee_employment_history"]


def register_payload(company, **changes):
    return {
        "company_id": company,
        "data": {"effective_date": "2026-01"},
        "source": "合成入职确认",
        "request_id": "hire",
        **changes,
    }


def test_selected_contracts_and_missing_dates_use_dedicated_management_input(service):
    app, _, scope, _ = service
    selected = app.dispatch("schema", {"view": "selected", "commands": COMMANDS})
    assert set(selected["command_schemas"]) == set(COMMANDS)
    for schema in selected["command_schemas"].values():
        Draft202012Validator.check_schema(schema)
    payload = register_payload(scope["company_id"])
    Draft202012Validator(selected["command_schemas"]["register_employee"]).validate(payload)
    with pytest.raises(NeedsInformation) as missing:
        app.dispatch("register_employee", payload | {"data": {}})
    assert missing.value.issues == [
        {
            "field": "data.effective_date",
            "message": "缺少必需任职管理事实",
            "semantics": "management",
            "reusable_sources": ["owner_confirmation", "employment_document"],
            "allowed_precision": ["month", "day"],
        }
    ]
    for command, data in (
        ("register_entity", {"employment_start": "2026-01"}),
        ("register_entity", {"employment_state": "regular"}),
    ):
        with pytest.raises(KernelError) as blocked:
            app.dispatch(
                command,
                {
                    "company_id": scope["company_id"],
                    "kind": "person",
                    "data": data,
                    "source": "合成资料",
                    "request_id": "blocked",
                },
            )
        assert blocked.value.code == "invalid_command"


def test_dynamic_mcp_hire_reuses_identity_without_clearing_omitted_metadata(
    service, monkeypatch, tmp_path
):
    from ai_accounting.kernel import mcp

    app, _, scope, _ = service
    company = scope["company_id"]
    original = app.dispatch(
        "register_entity",
        {
            "company_id": company,
            "kind": "person",
            "data": {"display_name": "合成员工", "display_number": "001", "note": "原管理备注"},
            "source": "合成人员资料",
            "request_id": "person",
        },
    )
    tools = {}

    class InProcessMCP:
        def __init__(self, *args, **kwargs):
            pass

        def tool(self):
            def keep(function):
                tools[function.__name__] = function
                return function

            return keep

        def run(self, **kwargs):
            assert kwargs == {"transport": "stdio"}

    monkeypatch.setattr(mcp, "FastMCP", InProcessMCP)
    monkeypatch.setattr(mcp, "ServiceClient", lambda root: SimpleNamespace(dispatch=app.dispatch))
    mcp.serve(tmp_path)
    schema = tools["finance_local_schema"](view="selected", commands=COMMANDS)
    assert set(schema["command_schemas"]) == set(COMMANDS)
    payload = register_payload(company, entity_id=original["entity_id"], expected_revision=1)
    saved = tools["finance_local_command"]("register_employee", payload)
    assert saved["entity_id"] == original["entity_id"] and saved["revision"] == 2
    assert set(saved) == {"status", "entity_id", "profile_id", "revision"}
    assert saved == tools["finance_local_command"]("register_employee", payload)
    current = app.dispatch("find_entities", {"company_id": company})["items"][0]["profile"]
    assert (current["display_name"], current["display_number"], current["note"]) == (
        "合成员工",
        "001",
        "原管理备注",
    )
    history = tools["finance_local_command"](
        "employee_employment_history",
        {
            "company_id": company,
            "entity_id": original["entity_id"],
        },
    )
    assert history["items"][0]["action"] == "hire"
    assert history["items"][0]["recorded_at"]


def test_draft_pending_receipt_history_provenance_and_context_share_original_sources(service):
    app, _, scope, _ = service
    company = scope["company_id"]
    engine = app.engine(company)
    proof = engine.register_evidence(
        b"Synthetic source confirmation",
        "text/plain",
        "合成原始确认",
        request_id="proof",
    )["digest"]
    payload = register_payload(company, evidence_digest=proof)
    pending = {
        "pending_requests": [
            {
                "command": "register_employee",
                "payload": payload,
                "request_id": "hire",
            }
        ]
    }
    stored = app.dispatch("save_work_draft", scope | {"draft": pending, "expected_revision": None})
    finished = {"result_refs": [{"request_id": "hire", "command": "register_employee"}]}
    with pytest.raises(KernelError) as unresolved:
        app.dispatch(
            "save_work_draft", scope | {"draft": finished, "expected_revision": stored["revision"]}
        )
    assert unresolved.value.code == "work_draft_pending_request_required"
    hired = app.dispatch("register_employee", payload)
    app.dispatch(
        "save_work_draft", scope | {"draft": finished, "expected_revision": stored["revision"]}
    )
    leave = app.dispatch(
        "change_employee_employment",
        {
            "company_id": company,
            "entity_id": hired["entity_id"],
            "expected_revision": 1,
            "data": {"action": "unpaid_leave", "effective_date": "2026-02"},
            "source": "合成停薪留职确认",
            "evidence_digest": proof,
            "request_id": "leave",
        },
    )
    history = app.dispatch(
        "employee_employment_history",
        {
            "company_id": company,
            "entity_id": hired["entity_id"],
            "limit": 1,
        },
    )
    assert history["items"][0]["profile_id"] == leave["profile_id"]
    assert history["items"][0]["source"] == "合成停薪留职确认"
    assert history["items"][0]["evidence_digest"] == proof and history["page"]["has_more"]
    older = app.dispatch(
        "employee_employment_history",
        {
            "company_id": company,
            "entity_id": hired["entity_id"],
            "limit": 1,
            "cursor": history["page"]["next_cursor"],
        },
    )
    with engine.store.connection(read_only=True) as connection:
        stamps = {
            row["request_id"]: row["created_at"]
            for row in connection.execute(
                "SELECT request_id,created_at FROM audit WHERE request_id IN ('hire','leave')"
            )
        }
    assert history["items"][0]["recorded_at"] == stamps["leave"]
    assert older["items"][0]["recorded_at"] == stamps["hire"]
    assert older["items"][0]["source"] == payload["source"]
    engine.save_fact(
        "expense",
        "expense",
        {
            "period": "2026-02",
            "counterparty_id": hired["entity_id"],
            "amount_fen": 100,
            "expense_class": "administration",
            "creditor_kind": "employee",
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="expense",
    )
    context = app.dispatch("work_context", scope)
    WORK_CONTEXT_ADAPTER.validate_python(context)
    assert len(context["entities"]) == 1
    profile = context["entities"][0]["profile"]
    assert profile["employment_state"] == "unpaid_leave"
    assert not {"employment_event", "employment_baseline", "employment_intervals"} & profile.keys()


def test_backup_retains_event_history_and_rehashed_invalid_projection_is_rejected(
    service, tmp_path
):
    app, _, scope, _ = service
    company = scope["company_id"]
    hired = app.dispatch("register_employee", register_payload(company))
    engine = app.engine(company)
    before = app.dispatch(
        "employee_employment_history",
        {
            "company_id": company,
            "entity_id": hired["entity_id"],
        },
    )
    archive = create_portable(engine.store.path, tmp_path / "verified-backups")
    assert verify_portable(archive["path"])["verification"]["status"] == "verified"
    assert (
        app.dispatch(
            "employee_employment_history",
            {
                "company_id": company,
                "entity_id": hired["entity_id"],
            },
        )
        == before
    )
    with engine.store.connection() as connection:
        triggers = list(
            connection.execute(
                "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
                "AND tbl_name='entity_profile_revision'"
            )
        )
        for trigger in triggers:
            connection.execute('DROP TRIGGER "' + trigger["name"] + '"')
        row = connection.execute(
            "SELECT * FROM entity_profile_revision WHERE id=?", (hired["profile_id"],)
        ).fetchone()
        content = json.loads(row["content"])
        content["employment_state"] = "departed"
        # Deliberately match the outer digest, preserving SQL and all identities.
        # Semantic verification must still reject a state with no departure event.
        digest = source_digest([row["entity_id"], row["revision"], content, row["source"], None])
        connection.execute(
            "UPDATE entity_profile_revision SET content=?,digest=? WHERE id=?",
            (
                source_canonical(content),
                digest,
                row["id"],
            ),
        )
        for trigger in triggers:
            connection.execute(trigger["sql"])
        connection.commit()
    with pytest.raises(BackupError) as corrupt:
        verify_file(engine.store.path)
    assert corrupt.value.code == "backup_content_invalid"
    assert verify_portable(archive["path"])["verification"]["status"] == "verified"
