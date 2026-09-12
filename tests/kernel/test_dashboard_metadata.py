"""Lazy presentation metadata keeps original exact-version and supplement semantics."""

import json

from test_banking import book as _bank_book
from test_dashboard_provenance import profile
from test_engine import close, publish, save
from test_engine import engine as _engine

from ai_accounting.kernel.dashboard import _Snapshot
from ai_accounting.kernel.dashboard_metadata import initialize_metadata
from ai_accounting.kernel.dashboard_reads import FrozenFacts
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.exports import Exports
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

engine, bank_book = _engine, _bank_book


def detached_snapshot(engine, connection, period="2026-01"):
    snapshot = _Snapshot.__new__(_Snapshot)
    snapshot.engine, snapshot.store = engine, engine.store
    snapshot.connection, snapshot.period = connection, period
    snapshot.month = YearMonth(period).ordinal
    close = connection.execute(
        "SELECT manifest FROM period_close WHERE period=?", (snapshot.month,)
    ).fetchone()
    snapshot.close = json.loads(close[0]) if close else None
    snapshot.reads = QueryReads(engine, connection)
    snapshot.profile_cache, snapshot.source_metadata = {}, {}
    snapshot.frozen_fact_ids = FrozenFacts(snapshot)
    snapshot.metadata = initialize_metadata(snapshot)
    return snapshot


def test_profile_key_iteration_and_entity_batch_do_not_decode_other_records(engine, monkeypatch):
    for index in range(30):
        profile(engine, "employee", f"employee-{index:03}", display_name=f"姓名 {index}")
        profile(engine, "asset", f"asset-{index:03}", display_name=f"资产 {index}")
    decoded = []
    original = Display._record

    def watched(record):
        decoded.append(record["id"])
        return original(record)

    monkeypatch.setattr(Display, "_record", watched)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        snapshot = detached_snapshot(engine, connection)
        assert decoded == []
        assert len(set(snapshot.profiles["employee"])) == 30
        assert decoded == []
        snapshot.metadata.prime_profiles("employee", {f"employee-{index:03}" for index in range(5)})
        assert len(decoded) == 5
        assert snapshot.profile("employee", "employee-003")["display_name"] == "姓名 3"
        assert len(decoded) == 5
        assert snapshot.profiles["asset"].cache == {}


def test_frozen_metadata_and_missing_fields_keep_original_sources(engine):
    save(engine)
    publish(engine)
    old = profile(engine, "fund_account", "bank", active=False, note="")
    periods = Periods(engine)
    periods.management(
        "charge",
        note="",
        payment_period=None,
        payment_category=None,
        expected_revision=0,
        request_id="metadata-before-close",
    )
    proof = engine.register_evidence(
        b"synthetic payee", "text/plain", "payee", request_id="payee-proof"
    )["digest"]
    export = Exports(engine)
    old_payee = export.save_payee(
        "party",
        name="原名称",
        account="123",
        evidence_digest=proof,
        expected_revision=0,
        request_id="old-payee",
    )
    close(engine)
    newer = profile(engine, "fund_account", "bank", 1, active=True, note="后来补齐")
    periods.management(
        "charge",
        note="后来说明",
        payment_period=None,
        payment_category=None,
        expected_revision=1,
        request_id="metadata-after-close",
    )
    export.save_payee(
        "party",
        name="新名称",
        account="456",
        evidence_digest=proof,
        expected_revision=1,
        request_id="new-payee",
    )
    profile(engine, "employee", "later-person", display_name="后来录入姓名")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        snapshot = detached_snapshot(engine, connection)
        assert (
            dict(snapshot.profiles["fund_account"])
            == Display.profiles(connection, "2026-01")["fund_account"]
        )
        assert "later-person" not in snapshot.profiles["employee"]
        later = snapshot.profile("employee", "later-person")
        assert later["field_sources"]["display_name"]["basis"] == "current_supplement"
        selected = snapshot.profile("fund_account", "bank")
        assert selected["active"] is False
        assert selected["field_sources"]["active"]["id"] == old["id"]
        assert selected["note"] == "后来补齐"
        assert selected["field_sources"]["note"]["id"] == newer["id"]
        management = snapshot.management["charge"]
        assert management["note"] == "后来说明"
        assert management["field_sources"]["note"]["basis"] == "current_supplement"
        assert management["id"] in snapshot.frozen_management_ids
        party = snapshot.party_details("party")
        assert party["name"] == "原名称" and party["id"] == old_payee["payee_revision_id"]
        assert party["field_sources"]["name"]["basis"] == "frozen"


def test_tax_identity_scope_loads_only_requested_employee_candidates(bank_book, monkeypatch):
    engine, save, _, _ = bank_book
    expected = set()
    for index in range(12):
        result = save(
            "tax_import_identity_v2",
            f"tax-{index}",
            {
                "period": "2026-09",
                "employee_id": "target" if index < 2 else f"person-{index}",
                "employee_code": "0007" if index < 2 else str(index),
                "name": f"候选姓名 {index}",
                "document_type": "居民身份证",
                "document_number": "001234567890123456",
            },
        )
        if index < 2:
            expected.add(result["fact_id"])
    loaded = set()
    original = engine.store.facts

    def watched(connection, identifiers):
        identifiers = set(identifiers)
        loaded.update(identifiers)
        return original(connection, identifiers)

    monkeypatch.setattr(engine.store, "facts", watched)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        snapshot = detached_snapshot(engine, connection, "2026-09")
        assert loaded == set()
        party = snapshot.party_details("target")
        assert party["name"] == "候选姓名 0、候选姓名 1（姓名资料待核对）"
        assert set(party["conflicting_ids"]) == expected == loaded
        assert snapshot.party_code("target") == "0007"
        assert loaded == expected
        assert set(snapshot.tax_identity_candidates.cache) == {"target"}
