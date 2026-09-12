"""Source time needs a uniquely identifiable confirmation, not an incidental ID."""

import json

import pytest
from test_workflow import confirmation_clock, obligation, setup_company

from ai_accounting.kernel.display import Display
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.provenance import recorded_times
from ai_accounting.kernel.read_indexes import sync_audit

RECORDED = "2026-09-11T10:00:00.000Z"


@pytest.mark.parametrize("actor", [False, True])
def test_batch_uses_exact_profile_payee_and_fact_versions_and_keeps_management_unknown(
    tmp_path, monkeypatch, actor
):
    company = setup_company(tmp_path)
    engine = company.engine
    if actor:
        engine.audit_actor = {"kind": "test", "id": "owner"}
    confirmation_clock(monkeypatch, engine, RECORDED)
    fact = company.save(obligation(), "obligation")
    profile = Display(engine).save_display_profile(
        {
            "kind": "employee",
            "entity_id": "person",
            "display_name": "确认的姓名",
            "source": "负责人提供",
        },
        expected_revision=0,
        request_id=company.request(),
    )
    payee = Exports(engine).save_payee(
        "person",
        name="确认的姓名",
        account="001234567890",
        evidence_digest=company.owner_confirmation,
        expected_revision=0,
        request_id=company.request(),
    )
    Periods(engine).management(
        "obligation",
        note="补充管理说明",
        payment_period=None,
        payment_category=None,
        expected_revision=0,
        request_id=company.request(),
    )
    with engine.store.connection(read_only=True) as connection:
        management = connection.execute("SELECT id FROM management_revision").fetchone()[0]
        expected = {
            ("fact", fact["fact_id"]): RECORDED,
            ("display_profile", profile["id"]): RECORDED,
            ("payee", payee["payee_revision_id"]): RECORDED,
        }
        references = [
            *expected,
            ("management", str(management)),
            ("fact", profile["id"]),
            ("fact", "retained-without-confirmation"),
        ]
        queries = []
        connection.set_trace_callback(queries.append)
        assert recorded_times(connection, references) == expected
        assert sum("FROM audit " in query for query in queries) == 1


@pytest.mark.parametrize("timestamp", [RECORDED, "2026-09-20T10:00:00.000Z", "invalid"])
def test_duplicate_confirmations_remain_unknown_even_when_timestamps_agree(
    tmp_path, monkeypatch, timestamp
):
    company = setup_company(tmp_path)
    confirmation_clock(monkeypatch, company.engine, RECORDED)
    saved = company.save(obligation(), "obligation")
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN")
        audit = connection.execute(
            "INSERT INTO audit(request_id,action,payload,created_at) VALUES(?,?,?,?)",
            ("retained-duplicate", "confirm_fact", json.dumps(saved), timestamp),
        )
        sync_audit(connection, audit.lastrowid)
        assert recorded_times(connection, [("fact", saved["fact_id"])]) == {}


@pytest.mark.parametrize(
    "action,transform",
    [
        ("publication", lambda saved: saved),
        ("confirm_fact", lambda saved: {"nested": saved}),
        ("confirm_fact", lambda saved: {"result": saved}),
        ("confirm_fact", lambda saved: {"result": saved, "actor": "unrecognized"}),
        ("confirm_fact", lambda saved: {**saved, "status": "preview"}),
        ("confirm_fact", lambda saved: {**saved, "revision": True}),
        ("confirm_facts", lambda saved: {"status": "preview", "results": [saved]}),
        ("confirm_facts", lambda saved: {"status": "confirmed", "results": {"0": saved}}),
    ],
)
def test_incidental_or_malformed_audit_references_cannot_establish_recording_time(
    tmp_path, action, transform
):
    company = setup_company(tmp_path)
    saved = {
        "status": "confirmed",
        "subject_id": "retained-subject",
        "fact_id": "retained-fact",
        "revision": 1,
        "pending": [],
    }
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN")
        audit = connection.execute(
            "INSERT INTO audit(request_id,action,payload,created_at) VALUES(?,?,?,?)",
            ("untrusted", action, json.dumps(transform(saved)), RECORDED),
        )
        sync_audit(connection, audit.lastrowid)
        assert recorded_times(connection, [("fact", saved["fact_id"])]) == {}


@pytest.mark.parametrize("timestamp", ["invalid", "2026-09-11T10:00:00", "2026-09-11"])
def test_unzoned_or_invalid_confirmation_timestamp_remains_unknown(
    tmp_path, monkeypatch, timestamp
):
    company = setup_company(tmp_path)
    confirmation_clock(monkeypatch, company.engine, timestamp)
    saved = company.save(obligation(), "obligation")
    with company.engine.store.connection(read_only=True) as connection:
        assert recorded_times(connection, [("fact", saved["fact_id"])]) == {}


def test_empty_and_unidentifiable_only_batches_do_not_read_audit(tmp_path):
    company = setup_company(tmp_path)
    with company.engine.store.connection(read_only=True) as connection:
        queries = []
        connection.set_trace_callback(queries.append)
        assert recorded_times(connection, []) == {}
        assert recorded_times(connection, [("management", "1")]) == {}
        assert not queries


def test_malformed_profile_metadata_does_not_break_other_source_queries(tmp_path):
    company = setup_company(tmp_path)
    saved = company.save(obligation(), "obligation")
    with company.engine.store.connection() as connection:
        connection.execute("BEGIN")
        original = recorded_times(connection, [("fact", saved["fact_id"])])
        audit = connection.execute(
            "INSERT INTO audit(request_id,action,payload,created_at) VALUES(?,?,?,?)",
            (
                "unrecognized-profile",
                "save_display_profile",
                json.dumps({"status": "saved", "id": "retained-profile", "kind": []}),
                RECORDED,
            ),
        )
        sync_audit(connection, audit.lastrowid)
        assert (
            recorded_times(
                connection,
                [("fact", saved["fact_id"]), ("display_profile", "retained-profile")],
            )
            == original
        )
