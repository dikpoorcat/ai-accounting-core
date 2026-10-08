"""Working documents cross public adapters without becoming business facts."""

from __future__ import annotations

import copy
import http.client
import json
import os
import threading
import zipfile
from functools import partial
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator

from ai_accounting.kernel.backup import create_portable, verify_portable
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.response_contracts import validate_response
from ai_accounting.kernel.service import LocalService


@pytest.fixture
def service(tmp_path):
    apps = []

    def start():
        app = LocalService(tmp_path)
        if not apps:
            app.security.provision("synthetic-owner", "Synthetic-test-only-2026")
        token = app.security.login("synthetic-owner", "Synthetic-test-only-2026").session_token
        app.dispatch = partial(app.dispatch, session_token=token)
        apps.append(app)
        return app, token

    app, token = start()
    company = app.dispatch(
        "create_company", {"taxpayer_id": "91310000123456789A", "name": "合成工作稿公司"}
    )["id"]
    scope = {"company_id": company, "period": "2026-02", "work_area": "transactions"}
    yield app, token, scope, start
    for item in apps:
        item.close()


def candidate(scope):
    return {
        "id": "expense",
        "command": "save_fact",
        "payload": {
            "company_id": scope["company_id"],
            "kind": "expense",
            "subject_id": "expense",
            "data": {
                "period": "2026-02",
                "amount_fen": 10101,
                "counterparty_id": "supplier",
                "expense_class": "administration",
            },
            "expected_revision": 0,
            "request_id": "submit-expense",
        },
    }


def business_state(app, company):
    engine = app.engine(company)
    with engine.store.connection(read_only=True) as connection:
        tables = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
        ]
        counts = {
            table: connection.execute(f'SELECT count(*) FROM "{table}"').fetchone()[0]
            for table in tables
        }
        return engine.store.epochs(connection), counts


def save(app, scope, draft, revision=None):
    return app.dispatch("save_work_draft", {**scope, "expected_revision": revision, "draft": draft})


def test_partial_candidates_answers_cross_month_refs_roundtrip_without_business_writes(service):
    app, _, scope, _ = service
    before = business_state(app, scope["company_id"])
    draft = {
        "candidates": [candidate(scope)],
        "questions": [{"id": "creditor", "question": "债权人类别？", "answer": "负责人尚未回答"}],
        "source_refs": [
            {
                "source_id": "received-in-march",
                "evidence_digest": "a" * 64,
                "location": "CSV!B3",
                "note": "接收2026-03，核算2026-02",
            }
        ],
        "resume_note": "尚未登记，继续核对已有来源。",
    }
    original = copy.deepcopy(draft)
    result = save(app, scope, draft)
    assert result["status"] == "saved" and "draft" not in result
    assert draft == original
    restored = app.dispatch("read_work_draft", scope)
    assert restored["draft"] == original and restored["warnings"] == []
    assert restored["revision"] == result["revision"]
    assert set(restored["draft"]) == set(original)
    assert "creditor_kind" not in restored["draft"]["candidates"][0]["payload"]["data"]
    assert business_state(app, scope["company_id"]) == before
    assert app.dispatch("work_context", scope)["items"] == []
    with pytest.raises(KernelError) as failure:
        app.dispatch("save_fact", draft["candidates"][0]["payload"])
    assert failure.value.response()["status"] == "needs_information"
    assert any(
        "creditor_kind" in issue["field"] for issue in failure.value.response()["fact_issues"]
    )
    assert business_state(app, scope["company_id"]) == before


def test_dedicated_candidates_retain_entry_and_generic_route_is_rejected(service):
    app, _, scope, _ = service
    dedicated = {
        "candidates": [
            {
                "id": "activation",
                "command": "prepare_asset_activation_batch",
                "payload": {"company_id": scope["company_id"], "kind": "asset_activation"},
            }
        ]
    }
    asset_scope = {**scope, "work_area": "assets"}
    result = save(app, asset_scope, dedicated)
    assert app.dispatch("read_work_draft", asset_scope)["draft"] == dedicated
    generic = copy.deepcopy(dedicated)
    generic["candidates"][0]["command"] = "save_fact"
    with pytest.raises(KernelError) as failure:
        save(app, asset_scope, generic, result["revision"])
    assert failure.value.code == "work_draft_target_invalid"
    assert app.dispatch("read_work_draft", asset_scope)["draft"] == dedicated


def test_restart_company_month_area_isolation_and_company_zip_excludes_drafts(service, tmp_path):
    app, _, scope, start = service
    draft = {"candidates": [candidate(scope)], "next_step": "核对债权人类别"}
    result = save(app, scope, draft)
    other_company = app.dispatch(
        "create_company", {"taxpayer_id": "91310000123456789B", "name": "另一合成公司"}
    )["id"]
    for other_scope in (
        {**scope, "company_id": other_company},
        {**scope, "period": "2026-03"},
        {**scope, "work_area": "payroll"},
    ):
        assert app.dispatch("read_work_draft", other_scope)["status"] == "absent"
    assert app.dispatch("list_work_drafts", {"company_id": other_company})["items"] == []
    engine = app.engine(scope["company_id"])
    portable = Path(create_portable(engine.store.path, tmp_path / "synthetic-backup")["path"])
    verify_portable(portable)
    with zipfile.ZipFile(portable) as archive:
        assert set(archive.namelist()) == {"manifest.json", "company.sqlite"}
    app.close()
    restarted, _ = start()
    restored = restarted.dispatch("read_work_draft", scope)
    assert restored["draft"] == draft and restored["revision"] == result["revision"]
    assert restarted.dispatch("list_work_drafts", {"company_id": scope["company_id"]})["items"] == [
        {"period": "2026-02", "work_area": "transactions"}
    ]


def test_pending_original_request_survives_commit_without_draft_update_and_replays_once(service):
    app, _, scope, _ = service
    engine = app.engine(scope["company_id"])
    proof = engine.register_evidence(
        b"synthetic expense confirmed", "text/plain", "proof", request_id="proof"
    )["digest"]
    from ai_accounting.kernel.entities import Entities

    supplier = Entities(engine).register_entity(
        "organization",
        {"display_name": "Supplier"},
        source="synthetic owner",
        request_id="supplier",
        evidence_digest=proof,
    )["entity_id"]
    original = candidate(scope)["payload"]
    original["data"].update(counterparty_id=supplier, creditor_kind="supplier")
    original["evidence"] = [proof]
    draft = {
        "pending_requests": [
            {"command": "save_fact", "payload": original, "request_id": original["request_id"]}
        ],
        "resume_note": "正式请求尚未判明",
    }
    saved = save(app, scope, draft)
    with pytest.raises(KernelError) as failure:
        save(
            app,
            scope,
            {"result_refs": [{"request_id": original["request_id"], "command": "save_fact"}]},
            saved["revision"],
        )
    assert failure.value.code == "work_draft_pending_request_required"
    assert app.dispatch("read_work_draft", scope)["draft"] == draft
    committed = app.dispatch("save_fact", original)
    restored = app.dispatch("read_work_draft", scope)
    assert restored["draft"] == draft and restored["revision"] == saved["revision"]
    receipt = app.dispatch(
        "request_result",
        {"company_id": scope["company_id"], "submitted_request_id": original["request_id"]},
    )
    assert receipt["status"] == "committed" and receipt["result"] == committed
    before = business_state(app, scope["company_id"])
    assert app.dispatch("save_fact", original) == committed
    assert business_state(app, scope["company_id"]) == before
    assert (
        app.dispatch(
            "request_result",
            {"company_id": scope["company_id"], "submitted_request_id": "never-sent"},
        )["status"]
        == "unknown"
    )
    for edited in (
        {"resume_note": "candidate editing"},
        {
            **draft,
            "pending_requests": [
                {**draft["pending_requests"][0], "payload": {**original, "subject_id": "different"}}
            ],
        },
    ):
        with pytest.raises(KernelError) as failure:
            save(app, scope, edited, saved["revision"])
        assert failure.value.code in {
            "work_draft_pending_request_required",
            "work_draft_request_conflict",
        }
    finished = {
        "result_refs": [
            {
                "request_id": original["request_id"],
                "command": "save_fact",
                "fact_id": committed["fact_id"],
            }
        ]
    }
    save(app, scope, finished, saved["revision"])
    assert business_state(app, scope["company_id"]) == before


@pytest.mark.parametrize(
    "command", ["list_work_drafts", "read_work_draft", "save_work_draft", "delete_work_draft"]
)
def test_selected_schema_and_native_responses_are_strict(service, command):
    app, _, scope, _ = service
    schema = app.dispatch(
        "schema", {"view": "selected", "commands": [command], "response_types": [command]}
    )
    for contract in (*schema["command_schemas"].values(), *schema["response_schemas"].values()):
        Draft202012Validator.check_schema(contract)
    if command == "list_work_drafts":
        payload = {"company_id": scope["company_id"]}
    elif command == "save_work_draft":
        payload = {**scope, "expected_revision": None, "draft": {}}
    elif command == "delete_work_draft":
        payload = {**scope, "expected_revision": save(app, scope, {})["revision"]}
    else:
        payload = scope
    Draft202012Validator(schema["command_schemas"][command]).validate(payload)
    result = app.dispatch(command, payload)
    assert validate_response(command, result) == result
    for invalid in ({**result, "schema_version": 1.0}, {**result, "undeclared": "data"}):
        with pytest.raises(KernelError) as failure:
            validate_response(command, invalid)
        assert failure.value.code == "response_contract_mismatch"


def test_http_local_capability_auth_scope_conflict_and_cli_use_public_entry(
    service, monkeypatch, tmp_path, capsys
):
    from ai_accounting.kernel import cli
    from ai_accounting.kernel.daemon import ServiceClient
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.security.credentials import InMemoryCredentialStore

    app, token, scope, _ = service
    server, capability = create_server(app, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def request(command, payload, *, authenticated=True, allowed=True):
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        headers = {"Content-Type": "application/json"}
        if allowed:
            headers["X-Local-Capability"] = capability
        if authenticated:
            headers["Authorization"] = "Bearer " + token.get_secret_value()
        connection.request(
            "POST", "/api/command", json.dumps({"command": command, "payload": payload}), headers
        )
        response = connection.getresponse()
        result = response.status, json.loads(response.read())
        connection.close()
        return result

    try:
        assert request("read_work_draft", scope, allowed=False)[0] == 403
        assert request("read_work_draft", scope, authenticated=False)[1]["status"] == "rejected"
        status, saved = request(
            "save_work_draft",
            {**scope, "expected_revision": None, "draft": {"candidates": [candidate(scope)]}},
        )
        assert status == 200 and saved["status"] == "saved"
        status, conflict = request(
            "save_work_draft", {**scope, "expected_revision": None, "draft": {}}
        )
        assert status == 409 and conflict["code"] == "work_draft_revision_conflict"
        wrong = candidate(scope)
        wrong["payload"]["company_id"] = "different-company"
        assert request(
            "save_work_draft",
            {**scope, "expected_revision": saved["revision"], "draft": {"candidates": [wrong]}},
        )[1]["code"] == ("work_draft_scope_mismatch")
        credentials = InMemoryCredentialStore()
        credentials.save_session_token(token)
        client = ServiceClient(
            app.catalog.root,
            metadata={
                "protocol": 2,
                "pid": os.getpid(),
                "database_format": app.catalog.database_format(),
                "port": server.server_port,
                "capability": capability,
                "catalog_id": app.security.catalog_instance_id,
                "build_id": server.build_id,
            },
            credential_store=credentials,
        )
        monkeypatch.setattr(cli, "ServiceClient", lambda root: client)
        input_file = tmp_path / "synthetic-cli-input.json"
        input_file.write_text(json.dumps(scope), encoding="utf-8")
        monkeypatch.setattr(
            "sys.argv",
            [
                "finance-local",
                "--root",
                str(tmp_path),
                "call",
                "read_work_draft",
                "--input",
                str(input_file),
            ],
        )
        cli.main()
        result = json.loads(capsys.readouterr().out)
        assert result["revision"] == saved["revision"]
        assert result["draft"]["candidates"][0] == candidate(scope)
        from ai_accounting.kernel import work_drafts

        monkeypatch.setattr(work_drafts, "MAX_WORK_DRAFT_BYTES", 4096)
        status, too_large = request(
            "save_work_draft",
            {**scope, "expected_revision": saved["revision"], "draft": {"resume_note": "x" * 5000}},
        )
        assert status == 413 and too_large["code"] == "work_draft_too_large"
        assert request("read_work_draft", scope)[1]["revision"] == saved["revision"]
        status, missing = request(
            "delete_work_draft",
            {**scope, "work_area": "financing", "expected_revision": saved["revision"]},
        )
        assert status == 404 and missing["code"] == "work_draft_absent"
        original_file = next((tmp_path / ".work-drafts").glob("*/*.json"))
        original_file.write_bytes(b"{corrupt")
        status, corrupt = request("read_work_draft", scope)
        assert status == 500 and corrupt["code"] == "work_draft_corrupt"
        assert original_file.read_bytes() == b"{corrupt"
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()
