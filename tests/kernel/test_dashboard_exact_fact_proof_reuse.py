"""Exact fact content is proven once, without borrowing another proof scope."""

import pytest
from test_banking import book as book
from test_dashboard_open_groups import expenses, snapshot
from test_dashboard_open_item_classification import _native_obligations
from test_integrity_content import damage

from ai_accounting.kernel import query_reads, storage
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads


def headers(connection):
    identifiers = {row[0] for row in connection.execute(
        "SELECT id FROM calculation WHERE kind='expense' ORDER BY id",
    )}
    return tuple(query_reads._read_exact_source_headers(connection, identifiers).values())


def observe_hashes(monkeypatch):
    actual = []
    original = storage._scalar_fact_hashes

    def measured(*args, **kwargs):
        result = original(*args, **kwargs)
        actual.append(set(result))
        return result

    monkeypatch.setattr(storage, "_scalar_fact_hashes", measured)
    return actual


@pytest.mark.parametrize("count", [3, 40])
def test_repeated_exact_scope_and_subset_do_not_hash_unrelated_facts_again(
    book, count, monkeypatch, record_property,
):
    engine, *_ = book
    expenses(book, count)
    actual = observe_hashes(monkeypatch)
    with snapshot(engine, "2026-09") as snap:
        selected = headers(snap.connection)
        snap.reads._verify_selected_fact_bodies(selected[:2])
        first = {item["fact_id"] for item in selected[:2]}
        assert actual == [first]
        snap.reads._verify_selected_fact_bodies(selected[:1])
        snap.reads._verify_selected_fact_bodies(selected[:2])
        assert actual == [first]
        snap.reads._verify_selected_fact_bodies(selected[1:3])
        assert actual == [first, {selected[2]["fact_id"]}]
        assert sum(map(len, actual)) == 3
        # Identity/seal callers still validate independently; changing an
        # expected content digest cannot borrow the previous fact proof.
        changed = dict(selected[0]) | {"fact_digest": bytes(32)}
        with pytest.raises(KernelError, match="事实内容封签不一致"):
            snap.reads._verify_selected_fact_bodies([changed])
        snap.reads._verify_selected_fact_bodies(selected[:1])
        record_property("actual_facts_hashed_before_bad_input", 3)
        record_property("unrelated_facts", count - 3)
    with snapshot(engine, "2026-09") as snap:
        actual.clear()
        snap.reads._verify_selected_fact_bodies(headers(snap.connection)[:1])
        assert len(actual) == 1 and len(actual[0]) == 1


def test_failure_of_one_fact_does_not_publish_half_batch_success(book, monkeypatch):
    engine, *_ = book
    expenses(book, 3)
    damage(engine, "fact_expense", "UPDATE fact_expense SET amount_fen=amount_fen+1 "
           "WHERE revision_id=(SELECT max(revision_id) FROM fact_expense)")
    actual = observe_hashes(monkeypatch)
    with snapshot(engine, "2026-09") as snap:
        selected = headers(snap.connection)
        for _ in range(2):
            with pytest.raises(KernelError, match="事实内容封签不一致"):
                snap.reads._verify_selected_fact_bodies(selected)
            assert not snap.reads._report_snapshot_cache.get("exact_fact_content_proofs")
        assert len(actual) == 2 and actual[0] == actual[1]


def test_conflicting_same_fact_inputs_cannot_publish_an_unchecked_digest(book):
    engine, *_ = book
    expenses(book, 1)
    with snapshot(engine, "2026-09") as snap:
        original = headers(snap.connection)[0]
        changed = dict(original) | {"fact_digest": bytes(32)}
        for batch in ((changed, original), (original, changed)):
            with pytest.raises(KernelError, match="同批事实内容依据互相冲突"):
                snap.reads._verify_selected_fact_bodies(batch)
            assert not snap.reads._report_snapshot_cache.get("exact_fact_content_proofs")
        snap.reads._verify_selected_fact_bodies((original,))
        for conflicting in (
            changed,
            dict(original) | {"fact_kind": "asset_consumption_month"},
            dict(original) | {"fact_period": original["fact_period"] + 1},
        ):
            for batch in ((conflicting, original), (original, conflicting)):
                with pytest.raises(KernelError, match="同批事实内容依据互相冲突"):
                    snap.reads._verify_selected_fact_bodies(batch)


def test_fixed_and_unowned_reads_do_not_use_current_success(book, monkeypatch):
    engine, *_ = book
    expenses(book, 1)
    actual = observe_hashes(monkeypatch)
    with snapshot(engine, "2026-09") as snap:
        selected = headers(snap.connection)
        snap.reads._verify_selected_fact_bodies(selected)
        with historical_content(1):
            snap.reads._verify_selected_fact_bodies(selected)
            snap.reads._verify_selected_fact_bodies(selected)
        snap.reads._verify_selected_fact_bodies(selected)
        assert len(actual) == 3
    actual.clear()
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        selected = headers(connection)
        reads._verify_selected_fact_bodies(selected)
        reads._verify_selected_fact_bodies(selected)
        assert len(actual) == 2


@pytest.mark.parametrize("field,value", [("amount_fen", 999), ("period", 24288)])
def test_damaged_fact_body_or_physical_period_never_caches_partial_success(book, field, value):
    engine, *_ = book
    expenses(book, 3)
    damage(engine, "fact_expense", f'UPDATE fact_expense SET "{field}"=? '
           "WHERE revision_id=(SELECT max(revision_id) FROM fact_expense)", (value,))
    with snapshot(engine, "2026-09") as snap:
        selected = headers(snap.connection)
        for _ in range(2):
            with pytest.raises(KernelError):
                snap.reads._verify_selected_fact_bodies(selected)
            assert not snap.reads._report_snapshot_cache.get("exact_fact_content_proofs")
            assert snap.reads._raw_fact_data == {}


def test_child_order_failure_never_caches_success_for_other_fact_bodies(book):
    engine, *_ = book
    _native_obligations(book)
    damage(engine, "fact_employee_advance_sources",
           "UPDATE fact_employee_advance_sources SET item_no=1 "
           "WHERE revision_id=(SELECT min(revision_id) FROM fact_employee_advance_sources)")
    with snapshot(engine, "2026-09") as snap:
        identifiers = {row[0] for row in snap.connection.execute("SELECT id FROM calculation")}
        selected = query_reads._read_exact_source_headers(snap.connection, identifiers)
        with pytest.raises(KernelError):
            snap.reads._verify_selected_fact_bodies(selected.values())
        assert not snap.reads._report_snapshot_cache.get("exact_fact_content_proofs")
        assert snap.reads._raw_fact_data == {}


def test_nonmatching_sql_hash_uses_exact_original_fact_decoder(book, monkeypatch):
    engine, *_ = book
    expenses(book, 2)
    original_hashes = storage._scalar_fact_hashes
    original_decoder = engine.store.fact_data_many
    decoded = []
    with snapshot(engine, "2026-09") as snap:
        selected = headers(snap.connection)
        forced = selected[0]["fact_id"]

        def mismatch(store, connection, kinds):
            result = original_hashes(store, connection, kinds)
            result[forced] = bytes(32)
            return result

        def decode(connection, identifiers):
            identifiers = set(identifiers)
            decoded.append(identifiers)
            return original_decoder(connection, identifiers)

        monkeypatch.setattr(storage, "_scalar_fact_hashes", mismatch)
        monkeypatch.setattr(engine.store, "fact_data_many", decode)
        snap.reads._verify_selected_fact_bodies(selected)
        assert decoded == [{forced}]
        assert len(snap.reads._report_snapshot_cache["exact_fact_content_proofs"]) == 2


def test_loaded_raw_fact_still_requires_independent_physical_period_proof_and_expires(book):
    engine, *_ = book
    expenses(book, 1)
    with snapshot(engine, "2026-09") as snap:
        reads = snap.reads
        selected = headers(snap.connection)
        snap.reads.fact_versions({selected[0]["fact_id"]})
        assert snap.reads._raw_fact_data
        assert not snap.reads._report_snapshot_cache.get("exact_fact_content_proofs")
        statements = []
        snap.connection.set_trace_callback(statements.append)
        try:
            snap.reads._verify_selected_fact_bodies(selected)
        finally:
            snap.connection.set_trace_callback(None)
        assert any(sql.startswith("SELECT f.revision_id,f.period FROM") for sql in statements)
        assert snap.reads._report_snapshot_cache["exact_fact_content_proofs"]
    assert reads._raw_fact_data == {}


def test_exact_body_success_does_not_replace_independent_complete_fact_verification(
    book, monkeypatch,
):
    engine, *_ = book
    expenses(book, 2)
    original = engine.store.fact_data_many
    decoded = []

    def decode(connection, identifiers):
        identifiers = set(identifiers)
        decoded.append(identifiers)
        return original(connection, identifiers)

    monkeypatch.setattr(engine.store, "fact_data_many", decode)
    with snapshot(engine, "2026-09") as snap:
        selected = headers(snap.connection)
        snap.reads._verify_selected_fact_bodies(selected)
        identifiers = {row["fact_id"] for row in selected}
        assert decoded == []
        snap.reads.verify_fact_versions(identifiers)
        verified = snap.reads.fact_versions(identifiers)
        assert decoded == [identifiers]
        assert set(verified) == identifiers
        assert sorted(version.fact.amount_fen for version in verified.values()) == [100, 101]
        assert all(version.evidence for version in verified.values())
