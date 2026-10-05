"""Only exact successful content proofs share result syntax/digest verification."""

import json

import pytest
from test_engine import engine as _engine
from test_engine import publish, save
from test_integrity_content import damage

from ai_accounting.kernel import stored_json
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads

engine = _engine


def prepared(engine):
    for subject, amount in (("good", 100), ("bad", 200)):
        save(engine, subject=subject, amount=amount, request=subject)
    publish(engine, ["good", "bad"])
    with engine.store.connection(read_only=True) as connection:
        return {
            row["subject_id"]: dict(row)
            for row in connection.execute(
                "SELECT c.id,c.subject_id,c.outcome FROM calculation_current h "
                "JOIN calculation c ON c.id=h.calculation_id"
            )
        }


def probe(monkeypatch):
    decoded = []
    original = stored_json.loads_unique

    def observed(raw):
        decoded.append(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
        return original(raw)

    monkeypatch.setattr(stored_json, "loads_unique", observed)
    return decoded


def test_successful_content_then_sql_checks_decode_exact_result_once(engine, monkeypatch):
    rows = prepared(engine)
    ident, raw = rows["good"]["id"], rows["good"]["outcome"]
    decoded = probe(monkeypatch)
    with QueryReads.snapshot(engine) as reads:
        outcome = reads.verify_selected_content((ident,))[ident]
        assert outcome == json.loads(raw)
        assert decoded.count(raw) == 1
        sql = []
        reads.connection.set_trace_callback(sql.append)
        reads.verify_sql_outcomes((ident,))
        reads.connection.set_trace_callback(None)
        assert decoded.count(raw) == 1
        assert sql == []
        # A decoded shape or metadata hit alone must not prove another ID.
        reads.metadata((rows["bad"]["id"],), state=False)
        reads.calculations((rows["bad"]["id"],))
        assert rows["bad"]["id"] not in reads._verified_source_contents
        before = decoded.count(rows["bad"]["outcome"])
        reads.verify_sql_outcomes((rows["bad"]["id"],))
        assert decoded.count(rows["bad"]["outcome"]) == before + 1


@pytest.mark.parametrize("change", ["duplicate", "digest"])
def test_failed_content_batch_cannot_skip_sql_verification(engine, monkeypatch, change):
    rows = prepared(engine)
    good, bad = rows["good"], rows["bad"]
    if change == "duplicate":
        raw = bad["outcome"][:-1] + ',"lines":[]}'
        damage(
            engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?", (raw, bad["id"])
        )
    else:
        damage(
            engine,
            "calculation",
            "UPDATE calculation SET digest=? WHERE id=?",
            (bytes(32), bad["id"]),
        )
    decoded = probe(monkeypatch)
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_content((good["id"], bad["id"]))
        assert failure.value.code == "content_integrity_failed"
        assert reads._verified_source_contents == {}
        assert reads._verified_sql_outcomes == set()
        with pytest.raises(KernelError) as failed_batch:
            reads.verify_sql_outcomes((good["id"], bad["id"]))
        assert failed_batch.value.code == "content_integrity_failed"
        assert reads._verified_sql_outcomes == set()
        before = decoded.count(good["outcome"])
        reads.verify_sql_outcomes((good["id"],))
        assert decoded.count(good["outcome"]) == before + 1
        with pytest.raises(KernelError) as sql_failure:
            reads.verify_sql_outcomes((bad["id"],))
        assert sql_failure.value.code == "content_integrity_failed"
        assert bad["id"] not in reads._verified_sql_outcomes


def test_content_proof_expires_with_owned_snapshot_and_plain_reads_recheck(engine, monkeypatch):
    rows = prepared(engine)
    ident, raw = rows["good"]["id"], rows["good"]["outcome"]
    decoded = probe(monkeypatch)
    with QueryReads.snapshot(engine) as reads:
        reads.verify_selected_content((ident,))
        reads.verify_sql_outcomes((ident,))
        assert decoded.count(raw) == 1
    assert not reads._snapshot_active
    assert reads._verified_source_contents == {}
    with QueryReads.snapshot(engine) as fresh:
        fresh.verify_sql_outcomes((ident,))
        assert decoded.count(raw) == 2
    with engine.store.connection(read_only=True) as connection:
        plain = QueryReads(engine, connection)
        plain.verify_selected_content((ident,))
        before = decoded.count(raw)
        plain.verify_sql_outcomes((ident,))
        plain.verify_sql_outcomes((ident,))
        assert decoded.count(raw) == before + 2


def test_sql_reuse_does_not_scan_unrelated_content_cache(engine):
    rows = prepared(engine)
    ident = rows["good"]["id"]

    class ExactMembership(dict):
        def keys(self):
            raise AssertionError("exact SQL result proof scanned unrelated content cache")

        def __iter__(self):
            raise AssertionError("exact SQL result proof scanned unrelated content cache")

    with QueryReads.snapshot(engine) as reads:
        reads.verify_selected_content((ident,))
        reads._verified_source_contents = ExactMembership(reads._verified_source_contents)
        reads.verify_sql_outcomes((ident,))
