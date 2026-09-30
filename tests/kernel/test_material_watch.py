"""Closed material reuse is located by authenticated, source-bound changes."""

import hashlib
import json

import pytest
from stage9_book import MixedBook

from ai_accounting.kernel import material_watch, materials
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.types import YearMonth


def test_watch_preserves_unchanged_sources_and_locates_linked_accounting_change(tmp_path):
    book = MixedBook(tmp_path / "watch-linked", employees=1, businesses=26)
    book.add_month(0)
    january = YearMonth("2016-01").ordinal
    with book.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        frozen, affected = material_watch.changed_material_sources(connection, january)
        assert frozen and not affected
        root = material_watch._stored_root(connection, january)
        occupied = {
            row[0]
            for row in connection.execute(
                "SELECT bucket FROM material_watch_bucket WHERE period=?", (january,)
            )
        }
        absent_in_occupied = next(
            key
            for index in range(100000)
            if (key := f"unrelated-subject:{index}") and material_watch._bucket(key) in occupied
        )
        assert material_watch._read_keys(connection, january, root, {absent_in_occupied}) == set()
        material_watch.require_material_watch(book.engine, connection)
        source_id, subject_id = connection.execute(
            "SELECT r.source_id,l.subject_id FROM fact_material_resolution_v2 r "
            "JOIN fact_current c ON c.fact_id=r.revision_id "
            "JOIN fact_material_resolution_v2_links l ON l.revision_id=r.revision_id "
            "WHERE r.period=? LIMIT 1",
            (january,),
        ).fetchone()
        connection.execute("DELETE FROM calculation_current WHERE subject_id=?", (subject_id,))
        _, affected = material_watch.changed_material_sources(connection, january)
        assert source_id in affected
        connection.execute(
            "DELETE FROM material_watch_bucket WHERE period=? AND bucket=?",
            (january, material_watch._bucket(f"business:{subject_id}")),
        )
        with pytest.raises(KernelError, match="资料变化目录"):
            material_watch.changed_material_sources(connection, january)
        connection.rollback()


def test_watch_future_inventory_only_affects_matching_evidence_and_repair_rebuilds(tmp_path):
    book = MixedBook(tmp_path / "watch-inventory", employees=1, businesses=26)
    book.add_month(0)
    january = YearMonth("2016-01").ordinal
    february = YearMonth("2016-02").ordinal
    with book.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        source_id, evidence, category, specification = connection.execute(
            "SELECT f.subject_id,unhex(s.evidence_digest),s.category,s.specification "
            "FROM fact_material_source_v2 s "
            "JOIN fact_revision f ON f.id=s.revision_id "
            "JOIN fact_current c ON c.fact_id=f.id WHERE s.purpose='business' LIMIT 1"
        ).fetchone()
        inventory_id = connection.execute(
            "SELECT coalesce(max(id),0)+1 FROM material_revision"
        ).fetchone()[0]
        connection.execute(
            "INSERT INTO material_revision VALUES(?,?,?,?,?,?,?)",
            (inventory_id, february, "unrelated-future", 1, 1, 0, evidence),
        )
        _, affected = material_watch.changed_material_sources(connection, january)
        assert source_id not in affected
        connection.execute("INSERT INTO material_item VALUES(?,?)", (inventory_id, evidence))
        _, affected = material_watch.changed_material_sources(connection, january)
        assert source_id in affected
        connection.rollback()


    # Sharing a generic supporting citation is not a competing material source.
    book.save(
        [
            book.item(
                "expense",
                "new-subject-sharing-closed-evidence",
                {
                    "period": "2016-02",
                    "counterparty_id": book.supplier,
                    "amount_fen": 12345,
                    "expense_class": "administration",
                    "creditor_kind": "supplier",
                },
                evidence=(evidence.hex(),),
            )
        ]
    )
    with book.engine.store.connection(read_only=True) as connection:
        _, affected = material_watch.changed_material_sources(connection, january)
        assert source_id not in affected

    # A distinct typed material source using the same original *is* a competing
    # source and must locate the frozen source through the typed evidence key.
    book.materials.receive(
        "new-source-sharing-closed-evidence",
        {
            "period": "2016-02",
            "category": category,
            "evidence_digest": evidence.hex(),
            "purpose": "business",
            "specification": json.loads(specification),
        },
        evidence=(evidence.hex(), book.proof),
        expected_revision=0,
        request_id=book.request("shared-source"),
    )
    with book.engine.store.connection(read_only=True) as connection:
        _, affected = material_watch.changed_material_sources(connection, january)
        assert source_id in affected
        full = materials.check_completeness(connection, february, book.engine.store.registry)
        summary = materials.read_completeness_summary(
            connection, february, book.engine.store.registry
        )
        assert list(summary.issues) == full["issues"]

    with book.engine.store.connection() as connection:
        # The repairable directory is never allowed to stand in for its root.
        number = connection.execute(
            "SELECT bucket FROM material_watch_bucket WHERE period=? LIMIT 1", (january,)
        ).fetchone()[0]
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE material_watch_bucket SET content='{}' WHERE period=? AND bucket=?",
            (january, number),
        )
        with pytest.raises(KernelError, match="资料变化目录"):
            material_watch.require_material_watch(book.engine, connection)
        assert material_watch.repair_material_watch(book.engine, connection)
        material_watch.require_material_watch(book.engine, connection)
        connection.rollback()

        # JSON with identical decoded values but changed stored bytes is not
        # the original root committed by the close.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "UPDATE material_watch_root SET content=content||' ' WHERE period=?", (january,)
        )
        with pytest.raises(KernelError, match="资料变化目录"):
            material_watch.changed_material_sources(connection, january)
        connection.rollback()


def test_full_rebuild_advances_one_journal_across_corrected_closes(tmp_path, monkeypatch):
    book = MixedBook(tmp_path / "watch-historic-replay", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1)
    with book.engine.store.connection() as connection:
        closes = tuple(connection.execute("SELECT period FROM period_close ORDER BY period"))
        assert len(closes) == 2
        # The second month corrects a January interest fact and replaces its head.
        corrected = connection.execute(
            "SELECT sequence FROM source_change WHERE source='fact' "
            "AND before_ref IS NOT NULL ORDER BY sequence LIMIT 1"
        ).fetchone()
        assert corrected is not None
        original = material_watch.changes_since
        calls = []

        def counted(connection, after, *, through=None):
            calls.append((after, through))
            return original(connection, after, through=through)

        monkeypatch.setattr(material_watch, "changes_since", counted)
        monkeypatch.setattr(
            material_watch,
            "heads_at",
            lambda *_args: pytest.fail("per-close journal replay returned"),
        )
        connection.execute("BEGIN IMMEDIATE")
        material_watch.require_material_watch(book.engine, connection)
        assert calls == [(0, None)]
        from ai_accounting.kernel import material_watch_v1

        historical_changes = material_watch_v1.changes_since
        historical_calls = []

        def counted_historical(connection, after, *, through=None):
            historical_calls.append((after, through))
            return historical_changes(connection, after, through=through)

        monkeypatch.setattr(material_watch_v1, "changes_since", counted_historical)
        monkeypatch.setattr(
            material_watch_v1,
            "heads_at",
            lambda *_args: pytest.fail("v1 per-close journal replay returned"),
        )
        material_watch_v1.require_material_watch(book.engine, connection)
        assert historical_calls == [(0, None)]
        calls.clear()
        connection.execute(
            "UPDATE material_watch_bucket SET content='[]' WHERE period=? AND bucket=("
            "SELECT min(bucket) FROM material_watch_bucket WHERE period=?)",
            (closes[0][0], closes[0][0]),
        )
        assert material_watch.repair_material_watch(book.engine, connection)
        assert calls == [(0, None)]
        connection.rollback()

        # A well-formed but false before-ref must fail both comparison and repair.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TRIGGER source_change_immutable")
        connection.execute(
            "UPDATE source_change SET before_ref='wrong-before-ref' WHERE sequence=?",
            (corrected[0],),
        )
        with pytest.raises(KernelError, match="资料变化目录"):
            material_watch.require_material_watch(book.engine, connection)
        with pytest.raises(KernelError, match="资料变化目录"):
            material_watch.repair_material_watch(book.engine, connection)
        connection.rollback()

        # Missing even a later journal row cannot be mistaken for an unchanged close.
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("DROP TRIGGER source_change_retained")
        connection.execute(
            "DELETE FROM source_change WHERE sequence=(SELECT max(sequence) FROM source_change)"
        )
        with pytest.raises(KernelError, match="资料变化记录"):
            material_watch.require_material_watch(book.engine, connection)
        with pytest.raises(KernelError, match="资料变化记录"):
            material_watch.repair_material_watch(book.engine, connection)
        connection.rollback()


def test_released_v1_material_watch_reads_nonempty_close_without_current_rules(
    tmp_path, monkeypatch
):
    from test_integrity_content import damage

    from ai_accounting.kernel import close_storage, close_storage_v1, material_watch_v1
    from ai_accounting.kernel.content_history_context import (
        historical_content,
        material_watch_reader,
    )
    from ai_accounting.kernel.types import canonical

    book = MixedBook(tmp_path / "watch-v1", employees=1, businesses=26)
    book.add_month(0)

    def changed_current(*_args, **_kwargs):
        raise AssertionError("released v1 must not use current close or watch decoding")

    monkeypatch.setattr(close_storage, "decode_close", changed_current)
    monkeypatch.setattr(close_storage, "verified_header", changed_current)
    monkeypatch.setattr(material_watch, "_directory", changed_current)
    with book.engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close LIMIT 1").fetchone()
        assert close_storage_v1.verified_header(connection, row).root["source_changes"]
        with historical_content(1):
            assert material_watch_reader() is material_watch_v1
            material_watch_reader().require_material_watch(book.engine, connection)
        assert material_watch_reader() is material_watch
    with book.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE material_watch_root SET content=content||' '")
        with historical_content(1), pytest.raises(KernelError, match="资料变化目录"):
            material_watch_reader().require_material_watch(book.engine, connection)
        connection.rollback()

    with book.engine.store.connection(read_only=True) as connection:
        root = json.loads(connection.execute("SELECT manifest FROM period_close").fetchone()[0])
    root.pop("source_changes")
    changed = canonical(root)
    damage(book.engine, "period_close", "UPDATE period_close SET manifest=?", (changed,))
    damage(
        book.engine,
        "close_storage_root",
        "UPDATE close_storage_root SET storage_digest=?",
        (hashlib.sha256(changed.encode("utf-8")).digest(),),
    )
    with book.engine.store.connection(read_only=True) as connection:
        row = connection.execute("SELECT * FROM period_close").fetchone()
        with pytest.raises(KernelError, match="关账私有存储"):
            close_storage_v1.verified_header(connection, row)


def test_new_business_does_not_probe_frozen_dependencies_but_restored_old_fact_does(
    tmp_path, monkeypatch
):
    book = MixedBook(tmp_path / "watch-new-business", employees=1, businesses=26)
    book.add_month(0)
    january = YearMonth("2016-01").ordinal
    seen = []
    original = material_watch._read_keys

    def record(connection, period, root, keys):
        seen.append(set(keys))
        return original(connection, period, root, keys)

    monkeypatch.setattr(material_watch, "_read_keys", record)
    subject = "new-subject-after-freeze"
    book.save(
        [
            book.item(
                "expense",
                subject,
                {
                    "period": "2016-02",
                    "counterparty_id": book.supplier,
                    "amount_fen": 12345,
                    "expense_class": "administration",
                    "creditor_kind": "supplier",
                },
            )
        ]
    )
    with book.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        _, affected = material_watch.changed_material_sources(connection, january)
        assert not affected
        assert f"business:{subject}" not in seen[-1]
        source_id, old_subject, fact_id = connection.execute(
            "SELECT r.source_id,l.subject_id,l.fact_id FROM fact_material_resolution_v2 r "
            "JOIN fact_current c ON c.fact_id=r.revision_id "
            "JOIN fact_material_resolution_v2_links l ON l.revision_id=r.revision_id "
            "JOIN fact_revision f ON f.id=l.fact_id WHERE f.revision=1 LIMIT 1"
        ).fetchone()
        connection.execute("DELETE FROM fact_current WHERE subject_id=?", (old_subject,))
        connection.execute("INSERT INTO fact_current VALUES(?,?)", (old_subject, fact_id))
        _, affected = material_watch.changed_material_sources(connection, january)
        assert f"business:{old_subject}" in seen[-1]
        assert source_id in affected
        connection.rollback()


def test_closed_source_later_revision_keeps_its_frozen_business_dependency(tmp_path, monkeypatch):
    book = MixedBook(tmp_path / "watch-later-source-revision", employees=1, businesses=26)
    book.add_month(0)
    january = YearMonth("2016-01").ordinal
    with book.engine.store.connection(read_only=True) as connection:
        source_id = connection.execute(
            "SELECT f.subject_id FROM fact_material_source_v2 s "
            "JOIN fact_revision f ON f.id=s.revision_id "
            "JOIN fact_current c ON c.fact_id=f.id WHERE s.purpose='business' LIMIT 1"
        ).fetchone()[0]
        source = book.engine.store.current_fact(connection, source_id)
    seen = []
    original = material_watch._read_keys

    def record(connection, period, root, keys):
        seen.append(set(keys))
        return original(connection, period, root, keys)

    monkeypatch.setattr(material_watch, "_read_keys", record)
    book.materials.receive(
        source_id,
        {
            **source.fact.model_dump(mode="json"),
            "supporting_purpose": "二月追加资料说明",
        },
        evidence=source.evidence,
        expected_revision=source.revision,
        request_id=book.request("later-source-revision"),
    )
    with book.engine.store.connection(read_only=True) as connection:
        assert book.engine.store.current_fact(connection, source_id).revision == 2
        _, affected = material_watch.changed_material_sources(connection, january)
        assert f"business:{source_id}" in seen[-1]
        assert source_id in affected


def test_first_creation_proof_is_shared_only_for_exact_subject_events(tmp_path, monkeypatch):
    from ai_accounting.kernel import change_journal, duplicate_freeze
    from ai_accounting.kernel.query_reads import QueryReads

    book = MixedBook(tmp_path / "watch-created-snapshot", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1, close=False)
    january = YearMonth("2016-01").ordinal
    calls = []
    original = change_journal.first_created_subjects

    def count(connection, events, **kwargs):
        events = tuple(events)
        calls.append(events)
        return original(connection, events, **kwargs)

    monkeypatch.setattr(change_journal, "first_created_subjects", count)
    with QueryReads.snapshot(book.engine) as reads:
        connection = reads.connection
        cache = materials._CompletenessInspectionCache(connection)
        statements = []
        connection.set_trace_callback(statements.append)
        material_watch.changed_material_sources(
            connection, january, _inspection_cache=cache
        )
        connection.set_trace_callback(None)
        assert len(calls) == 1
        assert not any(
            "SELECT f.id,f.subject_id,f.revision,(SELECT sequence" in sql
            for sql in statements
        ), "changed revision headers must not be read again for first creation"
        cached = duplicate_freeze.narrowed_duplicate_candidates(
            book.engine,
            connection,
            YearMonth("2016-02").ordinal,
            inspection_cache=cache,
            query_reads=reads,
        )
        assert len(calls) == 1
        without_cache = duplicate_freeze.narrowed_duplicate_candidates(
            book.engine,
            connection,
            YearMonth("2016-02").ordinal,
            query_reads=reads,
        )
        assert cached == without_cache
        assert len(calls) == 2

        # A restoration by itself is not the complete creation history for
        # its subject, even if a first creation was already proved above.
        events = cache.source_changes_since(
            connection, material_watch._stored_root(connection, january)["highwater"]
        )
        first = next(
            event for event in events
            if event.source == "fact" and event.before_ref is None and event.after_ref is not None
        )
        altered = change_journal.SourceChange(
            first.sequence,
            first.source,
            first.target_id,
            first.before_ref,
            "not-the-verified-reference",
        )
        with pytest.raises(KernelError):
            cache.first_created_subjects(connection, (altered,))
        assert len(calls) == 3


def test_failed_first_creation_proof_is_not_cached(tmp_path, monkeypatch):
    from ai_accounting.kernel import change_journal
    from ai_accounting.kernel.query_reads import QueryReads

    book = MixedBook(tmp_path / "watch-created-failure", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1, close=False)
    january = YearMonth("2016-01").ordinal
    with QueryReads.snapshot(book.engine) as reads:
        connection = reads.connection
        cache = materials._CompletenessInspectionCache(connection)
        events = cache.source_changes_since(
            connection, material_watch._stored_root(connection, january)["highwater"]
        )
        original = change_journal.first_created_subjects
        calls = []

        def fail_once(connection, events, **kwargs):
            calls.append(None)
            if len(calls) == 1:
                raise KernelError("content_integrity_failed", "synthetic failed proof")
            return original(connection, events, **kwargs)

        monkeypatch.setattr(change_journal, "first_created_subjects", fail_once)
        with pytest.raises(KernelError, match="synthetic failed proof"):
            cache.first_created_subjects(connection, events)
        assert cache.first_created_subjects(connection, events) == original(connection, events)
        assert len(calls) == 2


def test_kind_partition_still_rejects_missing_changed_material_row(tmp_path):
    book = MixedBook(tmp_path / "watch-missing-typed-row", employees=1, businesses=26)
    book.add_month(0)
    book.add_month(1, close=False)
    january = YearMonth("2016-01").ordinal
    february = YearMonth("2016-02").ordinal
    with book.engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        revision_id = connection.execute(
            "SELECT r.revision_id FROM fact_material_resolution_v2 r "
            "JOIN fact_current c ON c.fact_id=r.revision_id WHERE r.period=? LIMIT 1",
            (february,),
        ).fetchone()[0]
        triggers = tuple(
            connection.execute(
                "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name IN "
                "('fact_material_resolution_v2_links','fact_material_resolution_v2')"
            )
        )
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            "DELETE FROM fact_material_resolution_v2_links WHERE revision_id=?",
            (revision_id,),
        )
        connection.execute(
            "DELETE FROM fact_material_resolution_v2 WHERE revision_id=?",
            (revision_id,),
        )
        for _, sql in triggers:
            connection.execute(sql)
        with pytest.raises(KernelError) as error:
            material_watch.changed_material_sources(connection, january)
        assert error.value.code == "content_integrity_failed"
        assert error.value.details["reason"] == "changed_material_fact_missing"
        connection.rollback()
