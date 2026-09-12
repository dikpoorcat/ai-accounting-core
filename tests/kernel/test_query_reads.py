"""Exact batch loading and the shared page-summary boundaries."""

import hashlib
import json

from test_business_queries import state_review_engine as state_review_engine_fixture
from test_deletion_boundaries import book as domain_book_fixture
from test_deletion_boundaries import expense, payment, prepare_payment
from test_engine import close, publish, save
from test_engine import engine as engine_fixture

import ai_accounting.kernel.business_queries as business_query_module
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.query_reads import QueryReads, selected_voucher_sql
from ai_accounting.kernel.query_semantics import resolve_calculation_relations
from ai_accounting.kernel.read_indexes import close_rows, sync_close, sync_job
from ai_accounting.kernel.types import YearMonth, canonical

engine = engine_fixture
domain_book = domain_book_fixture
state_review_engine = state_review_engine_fixture


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


def test_one_response_reuses_complete_manifest_proof_across_subjects(engine, monkeypatch):
    for subject in ("first", "second"):
        save(engine, subject=subject, request=subject)
    publish(engine, ["first", "second"])
    close(engine)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        queries = BusinessQueries(engine, reads=reads)
        queries._selected_accounting(connection, "first", "2026-01", include_lines=False)

        def unexpected_second_graph_walk(_ident):
            raise AssertionError("the complete manifest graph was already proven in this request")

        monkeypatch.setattr(reads, "parents", unexpected_second_graph_walk)
        second = queries._selected_accounting(connection, "second", "2026-01", include_lines=False)
    assert len(second["through_period"]["voucher_events"]) == 1
    assert len(reads.closed_accounting_contexts) == 1


def test_repeated_later_dependency_manifests_do_not_expand_old_state_proof(engine):
    save(engine)
    _, published = publish(engine)
    close(engine)
    ident = published["results"][0]["calculation_id"]
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        for month in ("2026-02", "2026-03", "2026-04"):
            raw = canonical({"period": month, "calculations": [ident], "vouchers": []})
            connection.execute(
                "INSERT INTO period_close(period,manifest,digest) VALUES(?,?,?)",
                (
                    YearMonth(month).ordinal,
                    raw,
                    hashlib.sha256(raw.encode()).digest(),
                ),
            )
            sync_close(connection, YearMonth(month).ordinal)
        connection.commit()
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        reads = QueryReads(engine, connection)
        selected = BusinessQueries(engine, reads=reads)._selected_accounting(
            connection, "charge", "2026-04", include_lines=False
        )
    assert len(selected["through_period"]["voucher_events"]) == 1
    assert set(reads._close_manifests) == {YearMonth("2026-01").ordinal}


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


def test_scoped_voucher_candidates_use_identity_indexes_and_keep_cross_year_reversal(engine):
    save(engine, period="2025-12")
    save(engine, subject="unrelated", request="unrelated", period="2025-12")
    publish(engine, ["charge", "unrelated"])
    close(engine, "2025-12")
    save(engine, amount=150, revision=1, request="changed", period="2025-12")
    publish(engine, request="correct-next-year", correction_period="2026-01")
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
                assert any("calculation_kind_period (kind=?)" in row for row in scoped_plan)


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
