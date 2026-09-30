"""Saved-JSON selection work stays within exact selected or latest sources."""

import json

import pytest
import test_banking as banking

from ai_accounting.kernel import entities, stored_json
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.entities import Entities, employee_entities
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import digest

bank_book = banking.book


def test_sql_outcome_proof_reuses_success_only_within_one_snapshot(bank_book, monkeypatch):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    with engine.store.connection(read_only=True) as connection:
        identifier = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='funding'"
        ).fetchone()[0]
    checked = []
    original = stored_json.verify_outcome_bytes

    def counted(raw, expected_digest, ident, *, source_digest=None):
        checked.append(ident)
        return original(raw, expected_digest, ident, source_digest=source_digest)

    monkeypatch.setattr(stored_json, "verify_outcome_bytes", counted)
    with QueryReads.snapshot(engine) as reads:
        reads.verify_sql_outcomes({identifier})
        reads.verify_sql_outcomes({identifier})
    assert checked == [identifier]
    with QueryReads.snapshot(engine) as reads:
        reads.verify_sql_outcomes({identifier})
    assert checked == [identifier, identifier]


def test_failed_sql_outcome_batch_does_not_cache_successful_prefix(bank_book, monkeypatch):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish, subject="funding-a")
    banking.funding(save, publish, subject="funding-b", amount=2400, day="2026-09-02")
    with engine.store.connection() as connection:
        identifiers = sorted(
            row[0]
            for row in connection.execute(
                "SELECT calculation_id FROM calculation_current "
                "WHERE subject_id IN('funding-a','funding-b')"
            )
        )
        good, bad = identifiers
        raw = connection.execute("SELECT outcome FROM calculation WHERE id=?", (bad,)).fetchone()[0]
        changed = '{"values":{},' + raw[1:]
        assert json.loads(changed) == json.loads(raw)
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_calculation_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_calculation_UPDATE")
        connection.execute("UPDATE calculation SET outcome=? WHERE id=?", (changed, bad))
        connection.execute(trigger)
    checked = []
    original = stored_json.verify_outcome_bytes

    def counted(raw, expected_digest, ident, *, source_digest=None):
        checked.append(ident)
        return original(raw, expected_digest, ident, source_digest=source_digest)

    monkeypatch.setattr(stored_json, "verify_outcome_bytes", counted)
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError) as damaged:
            reads.verify_sql_outcomes(identifiers)
        assert damaged.value.code == "content_integrity_failed"
        reads.verify_sql_outcomes({good})
    assert checked == [good, bad, good]


def test_employee_membership_decodes_latest_profile_not_unrelated_revisions(bank_book, monkeypatch):
    engine, *_ = bank_book
    person = Entities(engine).register_entity(
        "person",
        {"display_name": "合成员工", "employment_status": "active"},
        source="synthetic profile",
        request_id="person",
    )["entity_id"]
    # Build valid older revisions through the real storage shape. Only the
    # newest record is relevant to the public membership predicate.
    with engine.store.connection() as connection:
        profile = connection.execute(
            "SELECT * FROM entity_profile_revision WHERE entity_id=?", (person,)
        ).fetchone()
        content = json.loads(profile["content"])
        for revision in range(2, 102):
            connection.execute(
                "INSERT INTO entity_profile_revision VALUES(?,?,?,?,?,?,?)",
                (
                    f"synthetic-{revision}",
                    person,
                    revision,
                    profile["content"],
                    profile["source"],
                    profile["evidence_digest"],
                    digest([person, revision, content, profile["source"], None]),
                ),
            )
    decoded = []
    original = entities._profile_record

    def counted(row):
        decoded.append(row["revision"])
        return original(row)

    monkeypatch.setattr(entities, "_profile_record", counted)
    with engine.store.connection(read_only=True) as connection:
        assert person in employee_entities(connection, "2026-09", registry=engine.store.registry)
    assert decoded == [101]
