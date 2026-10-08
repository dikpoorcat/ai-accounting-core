"""Retire original requests only after consistent, shared-snapshot receipts."""

import json
from contextlib import contextmanager

import pytest
from test_engine import engine as engine  # noqa: F401
from test_engine import evidence

from ai_accounting.kernel import runtime
from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.work_drafts import WorkDraftStore


def work_store(engine, tmp_path, **options):
    return WorkDraftStore(
        tmp_path,
        "synthetic-catalog",
        engine.store.company_id,
        engine.store.database_id,
        registry=engine.store.registry,
        commands={"save_fact"},
        request_result=engine.request_result,
        request_result_snapshot=options.get(
            "request_result_snapshot", engine._request_result_snapshot
        ),
    )


def pending_requests(engine, count):
    return [
        {
            "command": "save_fact",
            "request_id": f"receipt-{index}",
            "payload": {
                "company_id": engine.store.company_id,
                "request_id": f"receipt-{index}",
                "kind": "test_source",
                "subject_id": f"source-{index}",
                "data": {"period": "2026-02", "amount": index + 1},
                "expected_revision": 0,
            },
        }
        for index in range(count)
    ]


def commit(engine, requests):
    proof = evidence(engine)
    for item in requests:
        payload = item["payload"]
        engine.save_fact(
            payload["kind"],
            payload["subject_id"],
            payload["data"],
            evidence=(proof,),
            expected_revision=0,
            request_id=item["request_id"],
        )


def count_reads(monkeypatch):
    counts = {"connections": 0, "requests": 0, "audits": 0, "sql": 0}
    original_init = runtime._PrivateConnection.__init__

    def trace(sql):
        counts["sql"] += 1
        counts["requests"] += sql.startswith("SELECT digest,result FROM request WHERE id=")
        counts["audits"] += sql.startswith("SELECT action,payload FROM audit WHERE request_id=")

    def connection_init(connection, *args, **kwargs):
        original_init(connection, *args, **kwargs)
        counts["connections"] += 1
        connection.set_trace_callback(trace)

    monkeypatch.setattr(runtime._PrivateConnection, "__init__", connection_init)
    return counts


@pytest.mark.parametrize("count", [1, 2, 4])
def test_multiple_request_retirements_share_one_verified_snapshot(
    engine, tmp_path, monkeypatch, count
):
    store = work_store(engine, tmp_path)
    requests = pending_requests(engine, count)
    saved = store.save("2026-02", "transactions", None, {"pending_requests": requests})
    commit(engine, requests)
    finished = {"result_refs": [{"request_id": item["request_id"]} for item in requests]}
    counts = count_reads(monkeypatch)
    updated = store.save("2026-02", "transactions", saved["revision"], finished)
    assert counts["connections"] == 1
    assert counts["requests"] == counts["audits"] == count
    assert updated["revision"] != saved["revision"]
    assert store.read("2026-02", "transactions")["draft"] == finished


def test_candidate_edit_retaining_original_requests_does_not_open_receipt_snapshot(
    engine, tmp_path, monkeypatch
):
    store = work_store(engine, tmp_path)
    original = {"pending_requests": pending_requests(engine, 2)}
    saved = store.save("2026-02", "transactions", None, original)
    counts = count_reads(monkeypatch)
    edited = {**original, "resume_note": "负责人已回答，下次继续核对。"}
    store.save("2026-02", "transactions", saved["revision"], edited)
    assert counts["connections"] == 0
    assert store.read("2026-02", "transactions")["draft"] == edited


@pytest.mark.parametrize("failure", ["unknown", "corrupt", "identity"])
def test_one_bad_receipt_preserves_old_document_after_an_earlier_success(
    engine, tmp_path, monkeypatch, failure
):
    @contextmanager
    def wrong_identity():
        with engine._request_result_snapshot() as read_result:
            yield (
                lambda ident: (
                    read_result(ident) | {"database_id": "other-database"}
                    if ident == "receipt-1"
                    else read_result(ident)
                )
            )

    store = work_store(
        engine,
        tmp_path,
        request_result_snapshot=wrong_identity
        if failure == "identity"
        else engine._request_result_snapshot,
    )
    requests = pending_requests(engine, 2)
    original = {"pending_requests": requests, "resume_note": "原请求尚未全部判明。"}
    saved = store.save("2026-02", "transactions", None, original)
    commit(engine, requests[:1] if failure == "unknown" else requests)
    if failure == "corrupt":
        with engine.store.connection() as connection:
            triggers = connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='request'"
            ).fetchall()
            for name, _ in triggers:
                connection.execute(f'DROP TRIGGER "{name}"')
            connection.execute("UPDATE request SET result='{}' WHERE id='receipt-1'")
            for _, sql in triggers:
                connection.execute(sql)
            connection.commit()
    path = store.directory / "2026-02--transactions.json"
    before = path.read_bytes()
    counts = count_reads(monkeypatch)
    with pytest.raises(KernelError) as caught:
        store.save(
            "2026-02",
            "transactions",
            saved["revision"],
            {"result_refs": [{"request_id": item["request_id"]} for item in requests]},
        )
    assert caught.value.code == (
        "request_content_invalid" if failure == "corrupt" else "work_draft_pending_request_required"
    )
    assert counts["connections"] == 1
    assert counts["requests"] == counts["audits"] == 2
    assert path.read_bytes() == before
    restored = store.read("2026-02", "transactions")
    assert restored["revision"] == saved["revision"] and restored["draft"] == original


def test_shared_receipt_snapshot_does_not_see_later_commits(engine):
    engine.register_evidence(b"before", "text/plain", "before", request_id="before")
    with engine._request_result_snapshot() as read_result:
        assert read_result("before")["status"] == "committed"
        engine.register_evidence(b"after", "text/plain", "after", request_id="after")
        assert read_result("after")["status"] == "unknown"
    assert engine.request_result("after")["status"] == "committed"


@pytest.mark.parametrize("damage", ["duplicate_request", "duplicate_audit", "boolean", "null"])
def test_ambiguous_or_unstructured_receipt_cannot_retire_original_payload(engine, tmp_path, damage):
    store = work_store(engine, tmp_path)
    requests = pending_requests(engine, 1)
    original = {"pending_requests": requests}
    saved = store.save("2026-02", "transactions", None, original)
    commit(engine, requests)
    with engine.store.connection() as connection:
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' "
            "AND tbl_name IN ('request','audit')"
        ).fetchall()
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        request = connection.execute("SELECT result FROM request WHERE id='receipt-0'").fetchone()[
            0
        ]
        if damage == "duplicate_request":
            broken = request.replace('"revision":1', '"revision":0,"revision":1')
            assert broken != request
            connection.execute("UPDATE request SET result=? WHERE id='receipt-0'", (broken,))
        elif damage == "duplicate_audit":
            broken = request.replace('"revision":1', '"revision":0,"revision":1')
            assert broken != request
            connection.execute("UPDATE audit SET payload=? WHERE request_id='receipt-0'", (broken,))
        elif damage == "boolean":
            broken = json.loads(request) | {"revision": True}
            connection.execute(
                "UPDATE audit SET payload=? WHERE request_id='receipt-0'", (json.dumps(broken),)
            )
        else:
            connection.execute("UPDATE request SET result='null' WHERE id='receipt-0'")
            connection.execute("UPDATE audit SET payload='null' WHERE request_id='receipt-0'")
        for _, sql in triggers:
            connection.execute(sql)
        connection.commit()
    path = store.directory / "2026-02--transactions.json"
    before = path.read_bytes()
    with pytest.raises(KernelError) as caught:
        store.save(
            "2026-02",
            "transactions",
            saved["revision"],
            {"result_refs": [{"request_id": "receipt-0"}]},
        )
    assert caught.value.code == "request_content_invalid"
    assert path.read_bytes() == before
    with pytest.raises(KernelError) as caught:
        engine.request_result("receipt-0")
    assert caught.value.code == "request_content_invalid"


def test_command_validation_preserves_supplied_working_values_and_omissions():
    draft = {
        "candidates": [
            {
                "id": "partial-expense",
                "command": "save_fact",
                "payload": {
                    "kind": "expense",
                    "data": {
                        "amount_fen": 9007199254740993,
                        "supplied_number": 1.0,
                        "supplied_flag": True,
                        "supplied_empty": None,
                    },
                },
            }
        ],
        "questions": [{"id": "creditor", "question": "债权人是谁？"}],
    }
    payload = {
        "company_id": "synthetic-company",
        "period": "2026-02",
        "work_area": "transactions",
        "expected_revision": None,
        "draft": draft,
    }
    result = validate_command(command_models(default_registry()), "save_work_draft", payload)
    assert result["draft"] == draft
    data = result["draft"]["candidates"][0]["payload"]["data"]
    assert type(data["amount_fen"]) is int and data["amount_fen"] == 9007199254740993
    assert type(data["supplied_number"]) is float
    assert type(data["supplied_flag"]) is bool
    assert "period" not in data and "answer" not in result["draft"]["questions"][0]
    assert "pending_requests" not in result["draft"]
