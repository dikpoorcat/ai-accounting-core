"""Source time needs a uniquely identifiable confirmation, not an incidental ID."""

import json

import pytest
from entity_fixture import seed_entities
from test_workflow import confirmation_clock, obligation, setup_company

from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.provenance import recorded_times
from ai_accounting.kernel.read_indexes import sync_audit

RECORDED = "2026-09-11T10:00:00.000Z"


def test_fund_identity_receipt_keeps_original_profile_registration_time(tmp_path, monkeypatch):
    company = setup_company(tmp_path)
    entities = Entities(company.engine)
    data = {
        "bank_name": "合成银行",
        "bank_account_number": "0001234567",
        "account_holder_name": "合成公司",
    }
    confirmation_clock(monkeypatch, company.engine, RECORDED)
    saved = entities.register_entity(
        "fund_account",
        data,
        account_type="bank",
        source="明确的合成开户资料",
        evidence_digest=company.owner_confirmation,
        request_id=company.request(),
    )
    reference = ("display_profile", saved["profile_id"])
    identity_reference = ("fund_account_identity", saved["fund_account"]["digest"])
    with company.engine.store.connection(read_only=True) as connection:
        assert recorded_times(connection, [reference, identity_reference]) == {
            reference: RECORDED,
            identity_reference: RECORDED,
        }
    confirmation_clock(monkeypatch, company.engine, "2026-09-12T10:00:00.000Z")
    repeated = entities.register_entity(
        "fund_account",
        data,
        account_type="bank",
        source="复用已核实账户",
        evidence_digest=company.owner_confirmation,
        request_id=company.request(),
    )
    assert repeated["entity_id"] == saved["entity_id"]
    assert repeated["status"] == "existing"
    with company.engine.store.connection(read_only=True) as connection:
        assert recorded_times(connection, [reference, identity_reference]) == {
            reference: RECORDED,
            identity_reference: RECORDED,
        }


def test_fund_identity_correction_records_only_new_identity_version(tmp_path, monkeypatch):
    company = setup_company(tmp_path)
    entities = Entities(company.engine)
    data = {
        "bank_name": "合成银行",
        "bank_account_number": "0001234567",
        "account_holder_name": "合成公司",
    }
    confirmation_clock(monkeypatch, company.engine, RECORDED)
    saved = entities.register_entity(
        "fund_account",
        data,
        account_type="bank",
        source="明确合成开户资料",
        evidence_digest=company.owner_confirmation,
        request_id=company.request(),
    )
    corrected_time = "2026-09-12T10:00:00.000Z"
    confirmation_clock(monkeypatch, company.engine, corrected_time)
    corrected_data = data | {"bank_account_number": "0007654321"}
    corrected = entities.correct_fund_account_identity(
        saved["entity_id"],
        account_type="bank",
        data=corrected_data,
        expected_identity_digest=saved["fund_account"]["digest"],
        recording_error_confirmed=True,
        source="合成有据账号更正",
        evidence_digest=company.owner_confirmation,
        request_id=company.request(),
    )
    identity = corrected["fund_account"]
    expected = {
        ("display_profile", saved["profile_id"]): RECORDED,
        ("fund_account_identity", saved["fund_account"]["digest"]): RECORDED,
        ("fund_account_identity", identity["digest"]): corrected_time,
    }
    with company.engine.store.connection(read_only=True) as connection:
        assert recorded_times(connection, expected) == expected
    confirmation_clock(monkeypatch, company.engine, "2026-09-13T10:00:00.000Z")
    unchanged = entities.correct_fund_account_identity(
        saved["entity_id"],
        account_type="bank",
        data=corrected_data,
        expected_identity_digest=identity["digest"],
        recording_error_confirmed=True,
        source="合成复核未改变身份",
        evidence_digest=company.owner_confirmation,
        request_id=company.request(),
    )
    assert unchanged["fund_account"] == identity
    with company.engine.store.connection(read_only=True) as connection:
        assert recorded_times(connection, expected) == expected


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
    seed_entities(engine, [("person", "person", None)])
    profile = Entities(engine).update_entity_profile(
        "person",
        {
            "display_name": "确认的姓名",
        },
        source="负责人提供",
        expected_revision=1,
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
        payment_period=None,
        payment_category=None,
        expected_revision=0,
        request_id=company.request(),
    )
    with engine.store.connection(read_only=True) as connection:
        management = connection.execute("SELECT id FROM management_revision").fetchone()[0]
        expected = {
            ("fact", fact["fact_id"]): RECORDED,
            ("display_profile", profile["profile_id"]): RECORDED,
            ("payee", payee["payee_revision_id"]): RECORDED,
        }
        references = [
            *expected,
            ("management", str(management)),
            ("fact", profile["profile_id"]),
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
