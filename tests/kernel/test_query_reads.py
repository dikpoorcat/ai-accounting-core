"""Exact batch loading and the shared page-summary boundaries."""

import hashlib
import json
from types import SimpleNamespace

import pytest
from test_business_queries import state_review_engine as state_review_engine_fixture
from test_deletion_boundaries import book as domain_book_fixture
from test_deletion_boundaries import expense, payment, prepare_payment
from test_engine import close, evidence, publish, save
from test_engine import engine as engine_fixture

import ai_accounting.kernel.business_queries as business_query_module
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads, selected_voucher_sql
from ai_accounting.kernel.query_semantics import (
    project_settlement_followup,
    resolve_calculation_relations,
)
from ai_accounting.kernel.read_indexes import (
    CLOSE_CALCULATIONS,
    CLOSE_VOUCHERS,
    close_rows,
    sync_close,
    sync_job,
)
from ai_accounting.kernel.settlement_projection import (
    _COLUMNS,
    _sealed,
    settlement_dashboard_open,
    settlement_followup_summary,
    settlement_position_rows,
)
from ai_accounting.kernel.types import YearMonth, canonical

engine = engine_fixture
domain_book = domain_book_fixture
state_review_engine = state_review_engine_fixture


def test_frozen_voucher_selection_batches_metadata_and_keeps_state_when_loading_results(engine):
    subjects = [f"charge-{index}" for index in range(8)]
    for subject in subjects:
        save(engine, subject=subject, request=subject)
    publish(engine, subjects)
    close(engine)
    with QueryReads.snapshot(engine) as reads:
        statements = []
        reads.connection.set_trace_callback(statements.append)
        selected = BusinessQueries(engine, reads=reads)._selected_accounting(
            reads.connection, subjects, "2026-01"
        )
        identifiers = {
            item["calculation_id"] for item in selected["through_period"]["voucher_events"]
        }
        assert len(identifiers) == 8
        metadata_queries = [
            sql for sql in statements if sql.startswith("SELECT c.id,c.subject_id,c.kind")
        ]
        assert len(metadata_queries) == 1
        state = reads.metadata(identifiers)
        assert all(item["line_count"] == 2 for item in state.values())
        reads.calculations(identifiers)
        before = len(statements)
        assert reads.metadata(identifiers) == state
        assert len(statements) == before
        reads.connection.set_trace_callback(None)


def test_batch_facts_preserve_typed_versions_without_per_revision_queries(engine):
    identifiers = [
        save(engine, subject=f"charge-{i}", request=f"save-{i}")["fact_id"] for i in range(8)
    ]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        expected = {ident: engine.store.fact(connection, ident) for ident in identifiers}
        statements = []
        connection.set_trace_callback(statements.append)
        actual = engine.store.facts(connection, identifiers)
        connection.set_trace_callback(None)
    assert actual == expected
    assert len(statements) == 3


def test_accounting_and_profiles_do_not_expand_unrelated_frozen_materials(engine, monkeypatch):
    import ai_accounting.kernel.close_storage as close_storage

    save(engine, subject="bounded-accounting", request="bounded-accounting")
    publish(engine, ["bounded-accounting"])
    close(engine)

    def no_full_manifest(*_args, **_kwargs):
        raise AssertionError("accounting selection expanded the complete frozen manifest")

    monkeypatch.setattr(close_storage, "decode_close", no_full_manifest)
    with QueryReads.snapshot(engine) as reads:
        queries = BusinessQueries(engine, reads=reads)
        result = queries._selected_accounting(reads.connection, None, "2026-01")
        assert len(result["through_period"]["voucher_events"]) == 1
        assert queries._profiles(reads.connection, "bounded-accounting", "2026-01") == {}


def test_close_leaf_batches_share_verified_blocks_and_do_not_cache_failure(engine, monkeypatch):
    from collections import Counter

    from ai_accounting.kernel import close_storage

    for index in range(8):
        save(engine, subject=f"leaf-{index}", request=f"leaf-{index}")
    publish(engine, [f"leaf-{index}" for index in range(8)])
    close(engine)
    checked = Counter()
    original = close_storage._bucket_rows

    def counted(connection, header, subroot, field, bucket, **kwargs):
        checked[header.period, field, bucket] += 1
        return original(connection, header, subroot, field, bucket, **kwargs)

    monkeypatch.setattr(close_storage, "_bucket_rows", counted)
    with QueryReads.snapshot(engine) as reads:
        references = [
            dict(row)
            for row in reads.connection.execute(
                "SELECT * FROM close_reference WHERE path IN "
                "('adopted_results[*].calculation_id','adopted_results[*].fact_id') "
                "ORDER BY path,position"
            )
        ]
        assert len(references) == 16
        statements = []
        reads.connection.set_trace_callback(statements.append)
        reads.verify_close_references(references[:8])
        reads.verify_close_references(references[8:])
        reads.connection.set_trace_callback(None)
        assert checked and set(checked.values()) == {1}
        # Eight separate subject buckets share two physical queries; later
        # fact references reuse the already checked blocks in this snapshot.
        assert sum("JOIN close_storage_directory" in sql for sql in statements) == 1
        assert sum("JOIN close_storage_block" in sql for sql in statements) == 1
        known_parts = dict(reads._verified_close_storage_parts)
        with pytest.raises(KernelError):
            reads.verify_close_references([{**references[0], "position": "999999"}])
        assert reads._verified_close_storage_parts == known_parts
        zero = next(item for item in references if str(item["position"]) == "0")
        reads.verify_close_references([{**zero, "position": 0}])
        with pytest.raises(KernelError):
            reads.verify_close_references([{**zero, "position": False}])
        assert reads._verified_close_storage_parts == known_parts


def test_accounting_slice_does_not_publish_partly_verified_blocks(engine, monkeypatch):
    from ai_accounting.kernel import close_storage

    save(engine, subject="accounting-slice", request="accounting-slice")
    publish(engine, ["accounting-slice"])
    close(engine)
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[YearMonth("2026-01").ordinal])[0]
        before = dict(reads._verified_close_storage_parts)
        original = close_storage._buckets_rows

        def fail_after_adoption(connection, header, subroot, field, buckets):
            if field == "vouchers":
                raise RuntimeError("synthetic voucher block failure")
            return original(connection, header, subroot, field, buckets)

        monkeypatch.setattr(close_storage, "_buckets_rows", fail_after_adoption)
        with pytest.raises(RuntimeError, match="synthetic voucher block failure"):
            reads.close_accounting(row, subjects={"accounting-slice"})
        assert reads._verified_close_storage_parts == before
        assert not reads._close_accounting_slices


@pytest.mark.parametrize("damage_kind", ["publication", "voucher_current"])
def test_scoped_accounting_rejects_missing_authoritative_source(engine, damage_kind):
    from test_integrity_content import damage

    save(engine, subject="accounting-source", request="accounting-source")
    publish(engine, ["accounting-source"])
    close(engine)
    if damage_kind == "publication":
        damage(
            engine,
            "calculation_publication",
            "DELETE FROM calculation_publication WHERE subject_id='accounting-source'",
            foreign_keys=False,
        )
        mismatch = "storage_adoption_publication_mismatch"
    else:
        damage(
            engine,
            "voucher_current",
            "DELETE FROM voucher_current WHERE version_id IN ("
            "SELECT v.id FROM voucher_version v JOIN calculation c "
            "ON c.id=v.calculation_id WHERE c.subject_id='accounting-source')",
        )
        mismatch = "storage_voucher_source_mismatch"
    with QueryReads.snapshot(engine) as reads:
        row = reads.authoritative_close_rows(periods=[YearMonth("2026-01").ordinal])[0]
        with pytest.raises(KernelError) as error:
            reads.close_accounting(row, subjects={"accounting-source"})
        assert error.value.details["reason"] == mismatch


@pytest.mark.parametrize("damage_kind", ["directory", "missing", "extra", "bytes"])
def test_batched_close_blocks_reject_missing_extra_and_damaged_parts(engine, damage_kind):
    from test_integrity_content import damage

    from ai_accounting.kernel.close_storage import _bucket

    save(engine, subject="batch-damage", request="batch-damage")
    publish(engine, ["batch-damage"])
    close(engine)
    bucket = _bucket("batch-damage")
    where = " WHERE field='adopted_results' AND bucket=?"
    if damage_kind == "directory":
        damage(
            engine,
            "close_storage_directory",
            "DELETE FROM close_storage_directory" + where,
            (bucket,),
        )
    elif damage_kind == "missing":
        damage(engine, "close_storage_block", "DELETE FROM close_storage_block" + where, (bucket,))
    elif damage_kind == "extra":
        damage(
            engine,
            "close_storage_block",
            "INSERT INTO close_storage_block SELECT period,field,bucket,part+100,content,digest "
            "FROM close_storage_block" + where,
            (bucket,),
        )
    else:
        damage(
            engine,
            "close_storage_block",
            "UPDATE close_storage_block SET content='[]'" + where,
            (bucket,),
        )
    with QueryReads.snapshot(engine) as reads:
        references = reads.connection.execute(
            "SELECT * FROM close_reference WHERE path='adopted_results[*].calculation_id'"
        ).fetchall()
        before = dict(reads._verified_close_storage_parts)
        with pytest.raises(KernelError, match="关账"):
            reads.verify_close_references(references)
        assert reads._verified_close_storage_parts == before


def test_primed_calculation_scalar_reads_do_not_reload_batch(engine, monkeypatch):
    subjects = [f"primed-charge-{index}" for index in range(8)]
    for subject in subjects:
        save(engine, subject=subject, request=subject)
    publish(engine, subjects)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        identifiers = [
            row[0]
            for row in connection.execute(
                "SELECT calculation_id FROM calculation_current "
                "WHERE subject_id LIKE 'primed-charge-%' ORDER BY subject_id"
            )
        ]
        reads = QueryReads(engine, connection)
        statements = []
        connection.set_trace_callback(statements.append)
        expected = reads.prime_calculations(identifiers)
        connection.set_trace_callback(None)
        calculation_rows = [
            statement
            for statement in statements
            if "JOIN calculation c ON c.id=ids.value" in statement
            and "c.outcome AS outcome" in statement
        ]
        assert len(calculation_rows) == 1
        assert not any(
            "SELECT c.id,c.outcome FROM json_each" in statement for statement in statements
        )

        def no_scalar_reload(_self, _identifiers):
            raise AssertionError("primed calculation reloaded through batch reader")

        monkeypatch.setattr(QueryReads, "calculations", no_scalar_reload)
        assert {ident: reads.calculation(ident) for ident in identifiers} == expected


def test_batch_calculation_still_requires_exact_publication(engine):
    from test_integrity_content import damage

    save(engine, subject="publication-proof", request="publication-proof")
    publish(engine, ["publication-proof"])
    with engine.store.connection(read_only=True) as connection:
        calculation_id = connection.execute(
            "SELECT calculation_id FROM calculation_current WHERE subject_id='publication-proof'"
        ).fetchone()[0]
    damage(
        engine,
        "calculation_publication",
        "DELETE FROM calculation_publication WHERE calculation_id=?",
        (calculation_id,),
        foreign_keys=False,
    )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as missing:
            QueryReads(engine, connection).calculations((calculation_id,))
    assert missing.value.code == "unknown_calculation"


@pytest.mark.parametrize("owned_snapshot", [False, True])
def test_batch_calculation_still_requires_exact_fact_version(engine, owned_snapshot):
    from test_integrity_content import damage

    save(engine, subject="fact-proof", request="fact-proof")
    publish(engine, ["fact-proof"])
    with engine.store.connection(read_only=True) as connection:
        calculation_id, fact_id = connection.execute(
            "SELECT id,fact_id FROM calculation WHERE subject_id='fact-proof'"
        ).fetchone()
    damage(
        engine,
        "fact_revision",
        "DELETE FROM fact_revision WHERE id=?",
        (fact_id,),
        foreign_keys=False,
    )
    def assert_missing_source(reads):
        with pytest.raises(KernelError) as missing:
            reads.calculations((calculation_id,))
        assert missing.value.code == "content_integrity_failed"
        assert str(missing.value) == "核算元数据与来源事实身份不一致"
        assert not reads._metadata
        assert not reads._calculations

    if owned_snapshot:
        with QueryReads.snapshot(engine) as reads:
            assert_missing_source(reads)
    else:
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            assert_missing_source(QueryReads(engine, connection))


def test_summary_does_not_hydrate_history_without_obligation_declarations(engine):
    subjects = [f"charge-{i}" for i in range(8)]
    for subject in subjects:
        save(engine, subject=subject, request=subject)
    publish(engine, subjects)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        summary = BusinessQueries(engine, reads=reads).settlement_summary(connection, "2026-01")
    assert summary["obligations"] == []
    assert summary["complete"] is True
    assert summary["business_count"] == 0
    assert summary["movement_count"] == 0
    assert summary["line_relation_count"] == 0
    assert not reads._calculations
    assert not reads._fact_versions


def test_scoped_summary_includes_related_payments_and_keeps_month_amounts(domain_book):
    engine, _save, publish_businesses, *_ = domain_book
    prepare_payment(domain_book)
    publish_businesses("payment")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        queries = BusinessQueries(engine)
        summary = queries.settlement_summary(connection, "2026-01", subject_ids={"expense"})
        full = queries.settlements(connection, "2026-01", subject_ids={"expense"})
    item = summary["obligations"][0]
    assert item["paid_fen"] == item["period_paid_fen"] == 100
    assert item["remaining_fen"] == 0
    assert item["settlement_status"] == "settled"
    assert summary["movements"] == []
    assert summary["movement_count"] == len(full["movements"]) == 1
    assert item["source_events"] == []
    assert item["source_event_count"] == 1


def test_dashboard_settlements_aggregate_and_hydrate_only_open_page(domain_book):
    engine, save_business, publish_businesses, *_ = domain_book
    prepare_payment(domain_book)
    publish_businesses("payment")
    save_business("expense", "other-expense", expense(amount=200))
    publish_businesses("other-expense")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        full = BusinessQueries(engine, reads=reads).settlement_summary(connection, "2026-01")
        compact = settlement_dashboard_open(connection, "2026-01", limit=1, reads=reads)
        compact_summary = settlement_dashboard_open(
            connection, "2026-01", limit=1, summary_only=True, reads=reads
        )
        position = settlement_position_rows(connection, "2026-01", {"2202"}, reads=reads)
        current = settlement_dashboard_open(
            connection,
            "2026-01",
            current=True,
            page_keys={row["key"] for row in full["obligations"]},
            include_settled_page=True,
            reads=reads,
        )
        for current_scope in (False, True):
            expected = project_settlement_followup(
                BusinessQueries(engine, reads=reads).settlement_summary(
                    connection, "2026-01", current=current_scope
                )
            )
            assert (
                settlement_followup_summary(
                    connection, "2026-01", current=current_scope, reads=reads
                )
                == expected
            )
    open_rows = [row for row in full["obligations"] if row["remaining_fen"]]
    assert compact["page"]["total_count"] == len(open_rows) == 1
    assert [row["category_key"] for row in compact["obligations"]] == ["supplier_payables"]
    assert [
        {key: value for key, value in row.items() if key != "category_key"}
        for row in compact["obligations"]
    ] == open_rows
    assert compact["categories"]["supplier_payables"] == {"count": 1, "amount": 200}
    assert compact_summary["obligations"] == []
    assert compact_summary["page"] == compact["page"]
    assert sum(row["remaining"] for row in position) == 200
    assert {row["key"] for row in current["obligations"]} == {
        row["key"] for row in full["obligations"]
    }


def test_settlement_summary_rejects_damaged_projection_and_repairs_explicitly(domain_book):
    engine, _save, publish_businesses, *_ = domain_book
    prepare_payment(domain_book)
    publish_businesses("payment")
    with engine.store.connection() as connection:
        connection.execute(
            "UPDATE settlement_change SET amount=amount+1 "
            "WHERE rowid=(SELECT min(rowid) FROM settlement_change)"
        )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as damaged:
            BusinessQueries(engine).settlement_summary(
                connection, "2026-01", subject_ids={"expense"}
            )
        with pytest.raises(KernelError) as damaged_followup:
            settlement_followup_summary(connection, "2026-01", current=True)
    assert damaged.value.code == "content_integrity_failed"
    assert damaged_followup.value.code == "content_integrity_failed"
    assert engine.rebuild_projections(request_id="repair-settlement-projection")["changed"]
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        repaired = BusinessQueries(engine).settlement_summary(
            connection, "2026-01", subject_ids={"expense"}
        )
    assert repaired["obligations"][0]["remaining_fen"] == 0


def test_settlement_summary_requires_zero_row_period_seal(engine):
    save(engine)
    publish(engine)
    with engine.store.connection() as connection:
        connection.execute("DELETE FROM settlement_projection_seal")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as damaged:
            BusinessQueries(engine).settlement_summary(connection, "2026-01")
    assert damaged.value.code == "content_integrity_failed"


@pytest.mark.parametrize(
    "damage",
    (
        "UPDATE calculation_publication SET mode='open_replace'",
        "UPDATE calculation_publication SET posting_period=posting_period+1",
    ),
)
def test_settlement_summary_rejects_damaged_publication_identity(engine, damage):
    save(engine)
    publish(engine)
    with engine.store.connection() as connection:
        trigger = connection.execute(
            "SELECT name,sql FROM sqlite_master WHERE type='trigger' "
            "AND name='immutable_calculation_publication_UPDATE'"
        ).fetchone()
        connection.execute('DROP TRIGGER "' + trigger[0] + '"')
        connection.execute(damage)
        connection.execute(trigger[1])
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as damaged:
            BusinessQueries(engine).settlement_summary(connection, "2026-01")
    assert damaged.value.code == "content_integrity_failed"


@pytest.mark.parametrize("source_amount", [None, 0], ids=["unknown", "zero"])
def test_settlement_summary_preserves_unknown_and_zero_sources(domain_book, source_amount):
    engine, _save, publish_businesses, *_ = domain_book
    prepare_payment(domain_book)
    publish_businesses("payment")
    month = YearMonth("2026-01").ordinal
    with engine.store.connection() as connection:
        connection.execute(
            "UPDATE settlement_change SET amount=?,state=? WHERE change_kind='source'",
            (source_amount, "unresolved" if source_amount is None else "resolved"),
        )
        rows = [
            tuple(row)
            for row in connection.execute(
                f"SELECT {','.join(_COLUMNS)} FROM settlement_change "
                "ORDER BY posting_period,publication_id,item_no"
            )
        ]
        count, checksum = _sealed(connection, rows, {month})[month]
        connection.execute(
            "UPDATE settlement_projection_seal SET row_count=?,digest=? WHERE posting_period=?",
            (count, checksum, month),
        )
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        summary = BusinessQueries(engine).settlement_summary(
            connection, "2026-01", subject_ids={"expense"}
        )
        dashboard = settlement_dashboard_open(connection, "2026-01")
    obligation = summary["obligations"][0]
    assert obligation["source_amount_fen"] == source_amount
    expected_remaining = None if source_amount is None else -100
    assert obligation["remaining_fen"] == expected_remaining
    assert obligation["settlement_status"] == (
        "unestablished" if source_amount is None else "over_settled"
    )
    assert dashboard["obligations"][0]["remaining_fen"] == expected_remaining
    assert dashboard["categories"]["supplier_payables"]["amount"] == expected_remaining
    assert dashboard["complete"] is (source_amount is not None)


def test_unknown_source_amount_is_not_a_zero_or_settled_state(state_review_engine, monkeypatch):
    engine = state_review_engine
    save(engine)
    publish(engine)
    original = business_query_module.resolve_calculation_relations

    def unknown(calculation, **loaders):
        result = original(calculation, **loaders)
        return {
            **result,
            "obligations": [dict(item, amount_fen=None) for item in result["obligations"]],
        }

    monkeypatch.setattr(business_query_module, "resolve_calculation_relations", unknown)
    result = BusinessQueries(engine).business_status("charge", "2026-01")
    item = result["settlements"]["obligations"][0]
    assert item["source_amount_fen"] is None
    assert item["remaining_fen"] is None
    assert item["settlement_status"] == "unestablished"
    assert result["settlements"]["status"] == "partially_established"


def test_relation_cache_reuses_long_shared_ancestry_without_recursion():
    records = {
        str(i): {
            "id": str(i),
            "subject_id": str(i),
            "kind": "test",
            "fact_id": str(i),
            "outcome": {"lines": [], "values": {}},
        }
        for i in range(1500)
    }
    records["0"]["outcome"]["values"]["obligations"] = [
        {
            "key": "origin",
            "name": "primary",
            "amount_fen": 100,
            "account": "2202",
            "normal": "credit",
            "category": "payable",
            "counterparty_id": "supplier",
        }
    ]
    records["branch"] = {"id": "branch", "kind": "test", "outcome": {"lines": [], "values": {}}}
    calls = []

    def parents(ident):
        calls.append(ident)
        return ("1499",) if ident == "branch" else (str(int(ident) - 1),) if int(ident) else ()

    cache = {}
    first = resolve_calculation_relations(
        records["1499"],
        load_calculation=records.__getitem__,
        load_parents=parents,
        source_cache=cache,
    )
    calls.clear()
    second = resolve_calculation_relations(
        records["branch"],
        load_calculation=records.__getitem__,
        load_parents=parents,
        source_cache=cache,
    )
    assert first["obligations"] == second["obligations"]
    assert calls == ["branch"]


def test_event_page_only_loads_selected_voucher_lines(engine):
    subjects = {f"charge-{i}" for i in range(5)}
    for subject in subjects:
        save(engine, subject=subject, request=subject)
    publish(engine, list(subjects))
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        queries = BusinessQueries(engine, reads=reads)
        first = queries.business_collection(
            connection, subjects, "2026-01", section="events", limit=2
        )
        assert first["page"]["total_count"] == 5
        assert first["page"]["returned_count"] == 2
        assert first["page"]["has_more"] is True
        assert len(reads._lines) == 2
        assert not reads._calculations
        assert not reads._fact_versions
        second = queries.business_collection(
            connection,
            subjects,
            "2026-01",
            section="events",
            limit=2,
            after=first["page"]["next_cursor"],
        )
    assert {item["id"] for item in first["items"]}.isdisjoint(
        item["id"] for item in second["items"]
    )


def test_source_history_pages_revision_keys_before_typed_facts(engine):
    ids = []
    for revision in range(6):
        ids.append(save(engine, revision=revision, request=f"revision-{revision}")["fact_id"])
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        first = BusinessQueries(engine, reads=reads).business_collection(
            connection, "charge", "2026-01", section="source_history", limit=2
        )
    assert first["page"]["total_count"] == 6
    assert [item["id"] for item in first["items"]] == ids[:2]
    assert set(reads._fact_versions) == set(ids[:2])
    assert all(item["recorded_at"] for item in first["items"])


def test_source_history_keyset_keeps_full_scope_count_and_rejects_missing_after(engine):
    identifiers = [
        save(engine, revision=index, request=f"source-{index}")["fact_id"]
        for index in range(5)
    ]
    unrelated = save(engine, subject="unrelated", request="unrelated")["fact_id"]
    with QueryReads.snapshot(engine) as reads:
        queries = BusinessQueries(engine, reads=reads)
        pages = []
        after = None
        for _ in range(3):
            page = queries.business_collection(
                reads.connection,
                "charge",
                "2026-01",
                section="source_history",
                after=after,
                limit=2,
            )
            pages.append(page)
            after = page["page"]["next_cursor"]
        assert [item["id"] for page in pages for item in page["items"]] == identifiers
        assert [page["page"]["total_count"] for page in pages] == [5, 5, 5]
        assert [page["page"]["has_more"] for page in pages] == [True, True, False]
        assert set(reads._fact_versions) == set(identifiers)
        unscoped = queries.business_collection(
            reads.connection,
            None,
            "2026-01",
            section="source_history",
            after=identifiers[-1],
            limit=2,
        )
        assert [item["id"] for item in unscoped["items"]] == [unrelated]
        assert unscoped["page"]["total_count"] == 6
        for wrong_after in (unrelated, "missing"):
            with pytest.raises(KernelError) as failure:
                queries.business_collection(
                    reads.connection,
                    "charge",
                    "2026-01",
                    section="source_history",
                    after=wrong_after,
                    limit=2,
                )
            assert failure.value.code == "dashboard_snapshot_changed"


def test_external_followup_keyset_keeps_full_count_and_rejects_missing_after(domain_book):
    engine, save_fact, *_ = domain_book
    for name in ("external-a", "external-b", "external-c"):
        save_fact(
            "external_obligation",
            name,
            {
                "period": "2026-01",
                "obligation_kind": "quarterly_financial_report",
                "start_period": "2026-01",
                "end_period": "2026-01",
                "due_date": "2026-02-20",
                "applicability_confirmed": True,
                "applicability": "required",
            },
        )
    with QueryReads.snapshot(engine) as reads:
        snap = SimpleNamespace(
            connection=reads.connection,
            reads=reads,
            period="2026-01",
            as_of="2026-02-01",
        )
        dashboard = Dashboard(engine)
        first = dashboard._external_collection(snap, None, 2)
        second = dashboard._external_collection(snap, first["page"]["next_cursor"], 2)
        assert [item["obligation_id"] for item in first["items"]] == [
            "external-a", "external-b"
        ]
        assert [item["obligation_id"] for item in second["items"]] == ["external-c"]
        assert first["page"] == {
            "total_count": 3,
            "filtered_count": 3,
            "returned_count": 2,
            "has_more": True,
            "next_cursor": "external-b",
        }
        assert second["page"]["total_count"] == 3
        assert second["page"]["has_more"] is False
        with pytest.raises(KernelError) as failure:
            dashboard._external_collection(snap, "missing", 2)
        assert failure.value.code == "dashboard_snapshot_changed"


def test_current_settlement_page_preserves_original_scope_and_slot_identity(domain_book):
    engine, save_fact, publish_businesses, *_ = domain_book
    prepare_payment(domain_book)
    save_fact("expense", "later-expense", expense(period="2026-02"))
    publish_businesses("later-expense")
    later_payment = payment() | {
        "period": "2026-02",
        "actual_date": "2026-02-10",
        "amount_fen": 200,
        "allocations": [
            payment()["allocations"][0],
            payment("later-expense")["allocations"][0],
        ],
    }
    save_fact("cash_payment", "payment", later_payment, revision=1, amend=True)
    publish_businesses("payment")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        queries = BusinessQueries(engine)
        historical = queries.business_collection(
            connection, None, "2026-01", section="settlement_events"
        )
        current = queries.business_collection(
            connection, None, "2026-01", section="settlement_events", current=True
        )
        summary = queries._business_status(connection, "expense", "2026-01", summary=True)
    assert historical["items"] == []
    assert current["current_cutoff_period"] == "2026-02"
    assert current["page"]["total_count"] == 1
    assert current["items"][0]["source_business"]["subject_id"] == "expense"
    assert current["items"][0]["signed_amount_fen"] == 100
    assert summary["settlements"]["obligations"][0]["remaining_fen"] == 100
    assert summary["current_followups"]["settlements"]["obligations"][0]["remaining_fen"] == 0


def test_file_page_versions_worker_status_without_changing_business_scope(domain_book):
    engine, save_fact, publish_businesses, *_ = domain_book
    saved = save_fact("expense", "expense", expense())
    publish_businesses("expense")
    payload = json.dumps(
        {
            "plan": {
                "report_fact_ids": [saved["fact_id"]],
                "source_closes": [],
                "period": {"quarter_start": "2026-01-01", "quarter_end": "2026-03-31"},
            }
        }
    )
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        for ident in ("one", "two", "three"):
            connection.execute(
                "INSERT INTO jobs(id,kind,payload,status) VALUES(?,'report_export',?,'pending')",
                (ident, payload),
            )
            sync_job(connection, ident)
        connection.commit()

    def query():
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return BusinessQueries(engine).business_collection(
                connection, {"expense"}, "2026-01", section="file_jobs", limit=1
            )

    first = query()
    assert first["page"]["total_count"] == 3
    assert len(first["items"]) == 1
    with engine.store.connection() as connection:
        connection.execute("UPDATE jobs SET status='running',attempts=1 WHERE id='two'")
    second = query()
    assert second["page"]["collection_version"] != first["page"]["collection_version"]


def test_global_event_page_bounds_month_before_loading_old_manifests(engine):
    save(engine)
    publish(engine)
    close(engine)
    save(engine, subject="february", period="2026-02", request="february")
    publish(engine, ["february"], request="publish-february")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        result = BusinessQueries(engine, reads=reads).business_collection(
            connection, None, "2026-02", section="events", limit=1
        )
    assert result["page"]["total_count"] == 1
    assert result["items"][0]["posting_period"] == "2026-02"
    assert not reads._close_manifests
    assert {item["subject_id"] for item in reads._metadata.values()} == {"february"}


def test_one_response_reuses_direct_manifest_across_subjects(engine, monkeypatch):
    for subject in ("first", "second"):
        save(engine, subject=subject, request=subject)
    publish(engine, ["first", "second"])
    close(engine)
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        queries = BusinessQueries(engine, reads=reads)
        queries._selected_accounting(connection, "first", "2026-01", include_lines=False)

        def unexpected_second_graph_walk(_ident):
            raise AssertionError("direct adoption must not walk dependency ancestry")

        monkeypatch.setattr(reads, "parents", unexpected_second_graph_walk)
        second = queries._selected_accounting(connection, "second", "2026-01", include_lines=False)
        assert len(second["through_period"]["voucher_events"]) == 1
        assert len(reads._close_headers) == 1
        assert not reads._close_manifests  # Scoped reads never decode unrelated material history.


def test_publication_period_finds_state_adoption_even_if_reverse_index_omits_it(
    state_review_engine,
):
    from test_integrity_content import damage

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
    calculation_id = publication["results"][0]["calculation_id"]
    close(engine)
    month = YearMonth("2026-01").ordinal

    def selected():
        with QueryReads.snapshot(engine) as reads:
            result = BusinessQueries(engine, reads=reads)._selected_accounting(
                reads.connection, "charge", "2026-01", include_vouchers=False
            )
            assert month in reads._authoritative_closes
            assert month not in reads._closes
            return result

    baseline = selected()
    assert baseline["through_period"]["state_results"][0]["calculation_id"] == calculation_id
    damage(
        engine,
        "close_reference",
        "DELETE FROM close_reference WHERE close_period=? AND path=? AND reference_id=?",
        (month, CLOSE_CALCULATIONS, calculation_id),
    )
    assert canonical(selected()) == canonical(baseline)
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            close_rows(connection, periods=[month])
    assert failure.value.details["reason"] == "reference_multiset_mismatch"

    with engine.store.connection(read_only=True) as connection:
        block = next(
            row
            for row in connection.execute(
                "SELECT bucket,part,content FROM close_storage_block "
                "WHERE period=? AND field='adopted_results'",
                (month,),
            )
            if any(
                item[1]["calculation_id"] == calculation_id for item in json.loads(row["content"])
            )
        )
        entries = json.loads(block["content"])
        for _, item in entries:
            if item["calculation_id"] == calculation_id:
                item["result_digest"] = "0" * 64
        directory = json.loads(
            connection.execute(
                "SELECT content FROM close_storage_directory "
                "WHERE period=? AND field='adopted_results' AND bucket=?",
                (month, block["bucket"]),
            ).fetchone()[0]
        )
        subroot = json.loads(
            connection.execute(
                "SELECT content FROM close_storage_subroot WHERE period=? AND family='accounting'",
                (month,),
            ).fetchone()[0]
        )
        root = json.loads(
            connection.execute(
                "SELECT manifest FROM period_close WHERE period=?", (month,)
            ).fetchone()[0]
        )

    def hashed(value):
        return hashlib.sha256(canonical(value).encode("utf-8")).digest()

    block_digest = hashed(entries)
    damage(
        engine,
        "close_storage_block",
        "UPDATE close_storage_block SET content=?,digest=? "
        "WHERE period=? AND field='adopted_results' AND bucket=? AND part=?",
        (canonical(entries), block_digest, month, block["bucket"], block["part"]),
    )
    directory[block["part"]][1] = block_digest.hex()
    directory_digest = hashed(directory)
    damage(
        engine,
        "close_storage_directory",
        "UPDATE close_storage_directory SET content=?,digest=? "
        "WHERE period=? AND field='adopted_results' AND bucket=?",
        (canonical(directory), directory_digest, month, block["bucket"]),
    )
    for descriptor in subroot["directories"]["adopted_results"]:
        if descriptor[0] == block["bucket"]:
            descriptor[1] = directory_digest.hex()
    subroot_digest = hashed(subroot)
    damage(
        engine,
        "close_storage_subroot",
        "UPDATE close_storage_subroot SET content=?,digest=? "
        "WHERE period=? AND family='accounting'",
        (canonical(subroot), subroot_digest, month),
    )
    root["subroots"]["accounting"] = subroot_digest.hex()
    damage(
        engine,
        "period_close",
        "UPDATE period_close SET manifest=? WHERE period=?",
        (canonical(root), month),
    )
    damage(
        engine,
        "close_storage_root",
        "UPDATE close_storage_root SET storage_digest=? WHERE period=?",
        (hashed(root), month),
    )
    with pytest.raises(KernelError) as failure:
        selected()
    assert failure.value.details["reason"] == "direct_adoption_mismatch"


def test_missing_voucher_reverse_index_is_rejected_after_independent_root_discovery(engine):
    from test_integrity_content import damage

    from ai_accounting.kernel.maintenance import Maintenance

    save(engine)
    publish(engine)
    close(engine)
    month = YearMonth("2026-01").ordinal

    def selected():
        with QueryReads.snapshot(engine) as reads:
            result = BusinessQueries(engine, reads=reads)._selected_accounting(
                reads.connection, "charge", "2026-01", include_lines=False
            )
            assert month in reads._authoritative_closes
            assert month not in reads._closes
            return result

    baseline = selected()
    voucher_id = baseline["through_period"]["voucher_events"][0]["voucher_version_id"]
    damage(
        engine,
        "close_reference",
        "DELETE FROM close_reference WHERE close_period=? AND path=? AND reference_id=?",
        (month, CLOSE_VOUCHERS, voucher_id),
    )
    # The independent root still locates this voucher. The missing derived
    # mirror must neither hide it nor supply an unverified successful read.
    with QueryReads.snapshot(engine) as reads:
        close_row = reads.authoritative_close_rows(periods=[month])[0]
        accounting = reads.close_accounting(close_row, subjects={"charge"})
        assert {item["id"] for item in accounting.vouchers} == {voucher_id}
    with pytest.raises(KernelError) as selected_failure:
        selected()
    assert selected_failure.value.code == "content_integrity_failed"
    with engine.store.connection(read_only=True) as connection:
        with pytest.raises(KernelError) as failure:
            close_rows(connection, periods=[month])
    assert failure.value.details["reason"] == "reference_multiset_mismatch"
    repaired = Maintenance(engine).repair_read_indexes(request_id="repair-missing-voucher-mirror")
    assert repaired["changed"]
    assert canonical(selected()) == canonical(baseline)


def test_old_transitive_close_manifest_is_rejected(engine):
    save(engine)
    _, published = publish(engine)
    close(engine)
    ident = published["results"][0]["calculation_id"]
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        raw = json.dumps({"period": "2026-02", "calculations": [ident], "vouchers": []})
        connection.execute(
            "INSERT INTO period_close(period,manifest,digest) VALUES(?,?,zeroblob(32))",
            (YearMonth("2026-02").ordinal, raw),
        )
        with pytest.raises(KernelError) as invalid:
            sync_close(connection, YearMonth("2026-02").ordinal)
        assert invalid.value.code == "content_integrity_failed"
        connection.rollback()


def test_source_history_publication_lookup_uses_page_subject_index(engine):
    first = save(engine)
    _, original = publish(engine)
    save(engine, revision=1, request="second")
    publish(engine, request="second-publication")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        statements = []
        connection.set_trace_callback(statements.append)
        result = BusinessQueries(engine).business_collection(
            connection, "charge", "2026-01", section="source_history", limit=1
        )
        connection.set_trace_callback(None)
        lookup = next(sql for sql in statements if sql.startswith("SELECT c.id,c.fact_id"))
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + lookup)]
    assert result["items"][0]["id"] == first["fact_id"]
    assert result["items"][0]["trace_targets"] == [
        {
            "calculation_id": original["results"][0]["calculation_id"],
            "voucher_version_id": None,
        }
    ]
    assert any("SEARCH c USING INDEX calculation_subject (subject_id=?)" in row for row in plan)
    assert not any("SCAN c " in row for row in plan)


def test_subject_close_lookup_drives_complete_reference_key_and_period(engine):
    save(engine)
    publish(engine)
    close(engine)
    month = YearMonth("2026-01").ordinal
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        statements = []
        connection.set_trace_callback(statements.append)
        result = close_rows(
            connection, subject_ids={"charge"}, periods=(month,), through_period=month
        )
        connection.set_trace_callback(None)
        lookup = next(sql for sql in statements if sql.startswith("SELECT p.* FROM period_close"))
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + lookup)]
    assert [row["period"] for row in result] == [month]
    assert any("calculation_subject (subject_id=?)" in row for row in plan)
    assert any(
        "close_reference_lookup (reference_type=? AND reference_id=? AND close_period=?)" in row
        for row in plan
    )
    assert not any("close_reference_lookup (reference_type=?)" in row for row in plan)


def test_selected_voucher_adoption_lookup_drives_ids_and_rejects_missing_reference(engine):
    from test_integrity_content import damage

    subjects = [f"adoption-{index}" for index in range(8)]
    for subject in subjects:
        save(engine, subject=subject, request=subject)
    publish(engine, subjects)
    close(engine)
    month = YearMonth("2026-01").ordinal
    with QueryReads.snapshot(engine) as reads:
        rows = [
            {"id": row["reference_id"], "close_period": row["close_period"]}
            for row in reads.connection.execute(
                "SELECT reference_id,close_period FROM close_reference "
                "WHERE close_period=? AND path=? ORDER BY position",
                (month, CLOSE_VOUCHERS),
            )
        ]
        assert len(rows) == len(subjects)
        statements = []
        reads.connection.set_trace_callback(statements.append)
        reads.verify_selected_voucher_adoptions(rows, through_period=month)
        reads.connection.set_trace_callback(None)
        lookup = next(
            sql for sql in statements if sql.startswith("SELECT r.* FROM json_each(")
        )
        plan = [
            row[3]
            for row in reads.connection.execute("EXPLAIN QUERY PLAN " + lookup)
        ]
        assert any("SCAN ids VIRTUAL TABLE" in row for row in plan)
        assert any(
            "close_reference_lookup "
            "(reference_type=? AND reference_id=? AND close_period<?)" in row
            for row in plan
        )
        assert not any("close_reference_lookup (reference_type=?)" in row for row in plan)
    victim = rows[0]["id"]
    damage(
        engine,
        "close_reference",
        "DELETE FROM close_reference WHERE close_period=? AND path=? AND reference_id=?",
        (month, CLOSE_VOUCHERS, victim),
    )
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError) as failure:
            reads.verify_selected_voucher_adoptions(rows, through_period=month)
    assert failure.value.code == "content_integrity_failed"


def test_scoped_voucher_candidates_use_identity_indexes_and_keep_cross_year_reversal(engine):
    save(engine, period="2025-12")
    save(engine, subject="unrelated", request="unrelated", period="2025-12")
    publish(engine, ["charge", "unrelated"])
    close(engine, "2025-12")
    save(engine, amount=150, revision=1, request="changed", period="2025-12")
    publish(engine, request="correct-next-year", posting_period="2026-01")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        source, parameters = selected_voucher_sql("2026-01", subject_ids={"charge"})
        selected = [dict(row) for row in connection.execute(source, parameters)]
        plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + source, parameters)]
        assert len(selected) == 3
        assert {row["basis_subject_id"] for row in selected} == {"charge"}
        original = next(row for row in selected if row["selection_source"] == "close_manifest")
        reversal = next(row for row in selected if row["reverses_id"] is not None)
        assert reversal["reverses_id"] == original["id"]
        assert reversal["basis_calculation_id"] == original["basis_calculation_id"]
        assert any("calculation_subject (subject_id=?)" in row for row in plan)
        assert any("voucher_version_reverses (reverses_id=?)" in row for row in plan)
        assert not any("SEARCH v USING INDEX voucher_period" in row for row in plan)
        for filters, expected in (
            ({"kinds": {"test_charge"}}, 4),
            ({"subject_ids": {"charge"}, "posting_period": "2026-01"}, 2),
            ({"subject_ids": {"charge"}, "posting_start": "2026-01"}, 2),
            ({"voucher_ids": {original["id"]}, "subject_ids": {"charge"}}, 1),
            ({"subject_ids": set()}, 0),
        ):
            scoped, args = selected_voucher_sql("2026-01", **filters)
            assert len(connection.execute(scoped, args).fetchall()) == expected
            scoped_plan = [
                row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + scoped, args)
            ]
            assert not any("SEARCH v USING INDEX voucher_period" in row for row in scoped_plan)
            if "kinds" in filters:
                assert any("subject_kind (kind=?)" in row for row in scoped_plan)
                assert any("calculation_subject (subject_id=?)" in row for row in scoped_plan)


def test_joined_current_basis_preserves_reviews_replacements_and_closed_reversal(engine):
    save(engine)
    _, first = publish(engine)
    original_calculation = first["results"][0]["calculation_id"]

    def selected(period, **scope):
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            sql, parameters = selected_voucher_sql(period, subject_ids={"charge"}, **scope)
            return [dict(row) for row in connection.execute(sql, parameters)]

    save(engine, revision=1, request="review")
    _, review = publish(engine, request="publish-review")
    reviewed_calculation = review["results"][0]["calculation_id"]
    assert reviewed_calculation != original_calculation
    for scope in ({"no_close_references": True}, {"current_heads": True}):
        rows = selected("2026-01", **scope)
        assert len(rows) == 1
        assert rows[0]["voucher_calculation_id"] == original_calculation
        assert rows[0]["basis_calculation_id"] == reviewed_calculation

    for revision, amount in ((2, 150), (3, 175)):
        save(engine, revision=revision, amount=amount, request=f"replace-{revision}")
        _, replacement = publish(engine, request=f"publish-replace-{revision}")
    latest_calculation = replacement["results"][0]["calculation_id"]
    for scope in ({"no_close_references": True}, {"current_heads": True}):
        rows = selected("2026-01", **scope)
        assert len(rows) == 1
        assert rows[0]["voucher_calculation_id"] == latest_calculation
        assert rows[0]["basis_calculation_id"] == latest_calculation

    close(engine)
    frozen = selected("2026-01")
    assert len(frozen) == 1
    assert frozen[0]["basis_calculation_id"] == latest_calculation
    assert selected("2026-01", current_heads=True)[0]["basis_calculation_id"] == latest_calculation

    save(engine, revision=4, amount=200, request="closed-correction")
    publish(engine, request="publish-closed-correction", posting_period="2026-02")
    correction = selected("2026-02", current_heads=True, posting_period="2026-02")
    assert len(correction) == 2
    reversal = next(row for row in correction if row["reverses_id"] is not None)
    assert reversal["reverses_id"] == frozen[0]["id"]
    assert reversal["basis_calculation_id"] == latest_calculation


def test_relationship_candidate_queries_do_not_scan_unrelated_calculation_metadata(engine):
    subjects = [f"charge-{index}" for index in range(8)]
    for subject in subjects:
        save(engine, subject=subject, request=subject)
    publish(engine, subjects)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        statements = []
        connection.set_trace_callback(statements.append)
        assert reads.settlement_subjects("2026-01") == set()
        assert reads.related_subjects({"charge-0"}) == {"charge-0"}
        connection.set_trace_callback(None)
        declared = next(sql for sql in statements if sql.startswith("SELECT candidate.subject_id"))
        related = next(sql for sql in statements if sql.startswith("WITH RECURSIVE related"))
        declared_plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + declared)]
        related_plan = [row[3] for row in connection.execute("EXPLAIN QUERY PLAN " + related)]
    assert any("calculation_obligations (<expr>>?)" in row for row in declared_plan)
    assert not any("SCAN c " in row for row in declared_plan + related_plan)
