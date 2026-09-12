"""T7 managed read snapshots preserve selections and exact reference checks."""

import sqlite3

import pytest
from test_business_queries import state_review_engine as state_review_fixture
from test_engine import close, evidence, publish, save
from test_engine import engine as engine_fixture

import ai_accounting.kernel.read_indexes as indexes
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.storage import Store

engine = engine_fixture
state_review_engine = state_review_fixture


def voucher_reference(connection):
    return dict(
        connection.execute(
            "SELECT * FROM close_reference WHERE path=? LIMIT 1", (indexes.CLOSE_VOUCHERS,)
        ).fetchone()
    )


def test_selection_options_preserve_shared_snapshot_results(engine):
    save(engine)
    publish(engine)
    close(engine)
    save(engine, "february", amount=250, period="2026-02", request="february")
    publish(engine, ["february"], request="publish-february")
    cases = [
        (None, "2026-02", {}),
        ([], "2026-02", {}),
        ("charge", "2026-02", {}),
        (["february", "charge"], "2026-02", {}),
        (None, "2026-01", {}),
        (None, "2026-02", {"kinds": []}),
        (None, "2026-02", {"kinds": ["test_charge"]}),
        (None, "2026-02", {"kinds": ["unused", "test_charge"]}),
        (None, "2026-02", {"current_heads": True}),
        (None, "2026-02", {"include_vouchers": False}),
        (None, "2026-02", {"include_lines": False}),
        (None, "2026-02", {"posting_period": "2026-01"}),
        (None, "2026-02", {"posting_period": "2026-02"}),
    ]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        queries = BusinessQueries(engine)
        expected = [
            queries._selected_accounting(connection, subjects, period, **options)
            for subjects, period, options in cases
        ]
    with QueryReads.snapshot(engine) as reads:
        queries = BusinessQueries(engine, reads=reads)
        observed = [
            queries._selected_accounting(reads.connection, subjects, period, **options)
            for subjects, period, options in cases
        ]
        assert observed == expected
        assert queries._selected_accounting(reads.connection, {"charge"}, "2026-02") == expected[2]
        assert (
            queries._selected_accounting(
                reads.connection, ["charge", "february", "charge"], "2026-02"
            )
            == expected[3]
        )
        assert (
            queries._selected_accounting(
                reads.connection, None, "2026-02", kinds=["test_charge", "unused", "test_charge"]
            )
            == expected[7]
        )
    assert len(observed[0]["through_period"]["voucher_events"]) == 2
    assert observed[1]["through_period"]["status"] == "not_established"
    assert observed[5]["through_period"]["voucher_events"] == []
    assert observed[9]["through_period"]["voucher_events"] == []
    assert all("lines" not in event for event in observed[10]["through_period"]["voucher_events"])
    assert [
        event["posting_period"] for event in observed[4]["through_period"]["voucher_events"]
    ] == ["2026-01"]
    assert [
        event["posting_period"] for event in observed[11]["through_period"]["voucher_events"]
    ] == ["2026-01"]
    assert [
        event["posting_period"] for event in observed[12]["through_period"]["voucher_events"]
    ] == ["2026-02"]


@pytest.mark.parametrize("fail", [False, True], ids=["normal-exit", "exception-exit"])
def test_snapshot_lifetime_clears_all_memos_and_closes_read_only_connection(engine, fail):
    save(engine)
    publish(engine)
    close(engine)
    escaped = None
    try:
        with QueryReads.snapshot(engine) as reads:
            escaped = reads
            assert reads.connection.in_transaction
            BusinessQueries(engine, reads=reads)._selected_accounting(
                reads.connection, None, "2026-01"
            )
            reads.verify_close_references([voucher_reference(reads.connection)])
            assert reads._verified_close_references
            assert reads._metadata and reads._lines and reads.closed_accounting_contexts
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                reads.connection.execute("DELETE FROM calculation_current")
            if fail:
                raise RuntimeError("synthetic reader failure")
    except RuntimeError as error:
        assert fail and str(error) == "synthetic reader failure"
    assert escaped is not None and not escaped._snapshot_active
    for value in vars(escaped).values():
        if isinstance(value, (dict, set)):
            assert not value
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        escaped.connection.execute("SELECT 1")


def test_snapshot_rejects_transaction_replacement_and_savepoints(engine):
    with QueryReads.snapshot(engine) as reads:
        for statement in ("COMMIT", "ROLLBACK", "BEGIN", "SAVEPOINT replacement"):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                reads.connection.execute(statement)
            assert reads.connection.in_transaction and reads._snapshot_active
        for operation in (reads.connection.commit, reads.connection.rollback):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                operation()
            assert reads.connection.in_transaction
        assert reads.connection.execute("SELECT 1").fetchone()[0] == 1


def test_ordinary_reads_never_memoize_semantics_or_references_across_transactions(
    engine, monkeypatch
):
    save(engine)
    publish(engine)
    close(engine)
    checks = []
    original = indexes.verify_close_references

    def counted(connection, references):
        references = list(references)
        checks.append(references)
        return original(connection, references)

    monkeypatch.setattr(indexes, "verify_close_references", counted)
    with engine.store.connection(read_only=True) as connection:
        reads = QueryReads(engine, connection)
        queries = BusinessQueries(engine, reads=reads)
        # An ordinary connection may acquire or replace transactions; neither
        # connection identity nor in_transaction is a managed snapshot token.
        connection.execute("BEGIN")
        first = queries._selected_accounting(connection, None, "2026-02")
        reference = voucher_reference(connection)
        reads.verify_close_references([reference])
        reads.verify_close_references([reference])
        assert checks[-2:] == [[reference], [reference]]
        assert not reads._verified_close_references
        connection.commit()
        save(engine, "february", amount=250, period="2026-02", request="february")
        publish(engine, ["february"], request="publish-february")
        connection.execute("BEGIN")
        second = queries._selected_accounting(connection, None, "2026-02")
        assert len(first["through_period"]["voucher_events"]) == 1
        assert len(second["through_period"]["voucher_events"]) == 2
        reads.verify_close_references([reference])
        assert checks[-1] == [reference]
        assert not reads._verified_close_references


def test_fresh_snapshots_observe_publication_changes_and_company_isolation(engine, tmp_path):
    save(engine)
    publish(engine)
    with QueryReads.snapshot(engine) as old_reads:
        before = BusinessQueries(engine, reads=old_reads)._selected_accounting(
            old_reads.connection, "charge", "2026-01"
        )
    save(engine, amount=250, revision=1, request="updated-fact")
    publish(engine, request="updated-publication")
    with QueryReads.snapshot(engine) as reads:
        after = BusinessQueries(engine, reads=reads)._selected_accounting(
            reads.connection, "charge", "2026-01"
        )
    assert before["period_events"][0]["lines"][0]["debit"] == 100
    assert after["period_events"][0]["lines"][0]["debit"] == 250
    other = Engine(
        Store.create(
            tmp_path / "other.sqlite",
            engine.store.registry,
            "company-b",
            "91310000123456789B",
            "db-b",
        )
    )
    save(other, amount=400)
    publish(other)
    with QueryReads.snapshot(other) as reads:
        selected = BusinessQueries(other, reads=reads)._selected_accounting(
            reads.connection, "charge", "2026-01"
        )
    assert selected["period_events"][0]["lines"][0]["debit"] == 400


def test_reference_memo_requires_each_exact_field_and_does_not_cache_failures(engine, monkeypatch):
    save(engine)
    publish(engine)
    close(engine)
    checks = []
    original = indexes.verify_close_references

    def counted(connection, references):
        references = list(references)
        checks.append(references)
        return original(connection, references)

    monkeypatch.setattr(indexes, "verify_close_references", counted)
    with QueryReads.snapshot(engine) as reads:
        good = voucher_reference(reads.connection)
        reads.verify_close_references([good, good])
        assert checks[-1] == [good]
        checked_count = len(checks)
        reads.verify_close_references([good])
        assert len(checks) == checked_count
        for field, bad_value in (
            ("close_period", good["close_period"] + 1),
            ("path", indexes.CLOSE_CALCULATIONS),
            ("position", "999"),
            ("reference_type", "fact"),
            ("reference_id", "missing-voucher"),
            ("related_id", None),
        ):
            bad = good | {field: bad_value}
            for _ in range(2):
                with pytest.raises(KernelError, match="精确引用目录"):
                    reads.verify_close_references([good, bad])
                assert checks[-1] == [bad]
            checked_count = len(checks)
            reads.verify_close_references([good])
            assert len(checks) == checked_count
        numeric_position = good | {"position": int(good["position"])}
        with pytest.raises(KernelError, match="精确引用目录"):
            reads.verify_close_references([numeric_position, good | {"reference_id": "missing"}])
        # Even the successful prefix must be checked again after a mixed batch fails.
        reads.verify_close_references([numeric_position])
        assert checks[-1] == [numeric_position]
        boolean_position = good | {"position": False}
        with pytest.raises(KernelError, match="精确引用目录"):
            reads.verify_close_references([boolean_position])
        assert checks[-1] == [boolean_position]


def test_new_snapshot_rechecks_source_marker_for_previously_valid_tuple(engine):
    save(engine)
    publish(engine)
    close(engine)
    with QueryReads.snapshot(engine) as reads:
        reference = voucher_reference(reads.connection)
        reads.verify_close_references([reference])
    # Deliberately damage only this synthetic database, restoring the exact DDL
    # so the next read tests source validation rather than schema validation.
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        trigger = connection.execute(
            "SELECT sql FROM sqlite_schema WHERE name='immutable_read_index_source_UPDATE'"
        ).fetchone()[0]
        connection.execute("DROP TRIGGER immutable_read_index_source_UPDATE")
        connection.execute(
            "UPDATE read_index_source SET source_digest=? WHERE source_kind='close'",
            (bytes(32),),
        )
        connection.execute(trigger)
        connection.commit()
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError, match="精确引用目录"):
                reads.verify_close_references([reference])
            assert not reads._verified_close_references


def test_verified_leaf_never_promotes_dependent_state_to_adopted(state_review_engine):
    engine = state_review_engine
    proof = evidence(engine)
    engine.save_fact(
        "test_charge",
        "charge",
        {"period": "2026-01", "amount": 100, "suppress_posting": True},
        evidence=(proof,),
        expected_revision=0,
        request_id="state",
    )
    _, publication = publish(engine)
    ident = publication["results"][0]["calculation_id"]
    engine.save_fact(
        "test_exact_reference",
        "reference",
        {"period": "2026-01", "old_id": ident},
        evidence=(proof,),
        expected_revision=0,
        request_id="reference",
    )
    publish(engine, ["reference"], request="publish-reference")
    close(engine)
    with QueryReads.snapshot(engine) as reads:
        refs = reads.connection.execute(
            "SELECT * FROM close_reference WHERE path=? AND reference_id=?",
            (indexes.CLOSE_CALCULATIONS, ident),
        ).fetchall()
        assert refs
        reads.verify_close_references(refs)
        reads.verify_close_references(refs)
        queries = BusinessQueries(engine, reads=reads)
        for _ in range(2):
            result = queries._selected_accounting(reads.connection, "charge", "2026-01")
            selected = result["through_period"]
            assert selected["status"] == "unestablished"
            assert selected["state_results"] == []
            candidates = selected["unestablished_state_selections"][0]["candidates"]
            assert [item["calculation_id"] for item in candidates] == [ident]
            assert candidates[0]["trace_only"] is True
        current = queries._selected_accounting(
            reads.connection, "charge", "2026-01", current_heads=True
        )
        assert current["through_period"]["status"] == "established"
        assert current["through_period"]["state_results"][0]["calculation_id"] == ident
