"""Page keys precede fact expansion; bank child rows never hydrate the parent fact."""

import sqlite3
from collections import Counter

import pytest
from test_banking import book as _bank_book
from test_banking import entry, funding, match, opening, reconciliation, statement
from test_dashboard_funds_alignment import _publish_filter_funding

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import FundsRead, _sql_summary_page, funds
from ai_accounting.kernel.runtime import _PrivateConnection

bank_book = _bank_book


def test_sql_page_expands_source_once_and_keeps_filter_and_cursor_counts():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    visited = []

    def observed(value):
        visited.append(value)
        return value

    connection.create_function("observed", 1, observed)
    source = (
        "SELECT observed(value) page_key, category FROM "
        "(SELECT 'a' value,'bank' category UNION ALL "
        "SELECT 'b','cash' UNION ALL SELECT 'c','bank')"
    )
    summary, rows, page = _sql_summary_page(
        connection,
        source,
        [],
        "SELECT category,count(*) n FROM source_rows GROUP BY category",
        ("category", "n"),
        after="a",
        limit=1,
        where="category=?",
        filters=("bank",),
    )
    assert summary == [{"category": "bank", "n": 2}, {"category": "cash", "n": 1}]
    assert [row["page_key"] for row in rows] == ["c"]
    assert page == {
        "total_count": 3,
        "filtered_count": 2,
        "returned_count": 1,
        "has_more": False,
        "next_cursor": None,
    }
    assert visited == ["a", "b", "c"]
    with pytest.raises(KernelError, match="分页位置已变化"):
        _sql_summary_page(
            connection,
            source,
            [],
            "SELECT category,count(*) n FROM source_rows GROUP BY category",
            ("category", "n"),
            after="b",
            limit=1,
            where="category=?",
            filters=("bank",),
        )
    _, rows, page = _sql_summary_page(
        connection,
        source,
        [],
        "SELECT category,count(*) n FROM source_rows GROUP BY category",
        ("category", "n"),
        after=None,
        limit=1,
        where="category=?",
        filters=("missing",),
    )
    assert rows == [] and page["total_count"] == 3 and page["filtered_count"] == 0


@pytest.mark.parametrize("count", [3, 503])
def test_statement_page_and_summary_do_not_decode_complete_parent(bank_book, monkeypatch, count):
    engine, save, publish, _ = bank_book
    opening(save, publish)
    funding(save, publish)
    statement(save, publish, [entry(f"row-{index}", amount=1) for index in range(count)])
    loaded = set()
    original = engine.store.facts

    def watched(connection, identifiers):
        loaded.update(identifiers)
        result = original(connection, identifiers)
        assert all(item.fact.kind != "bank_statement" for item in result.values())
        return result

    monkeypatch.setattr(engine.store, "facts", watched)
    with Dashboard(engine)._snapshot("2026-09") as snap:
        data = funds(snap, sections={"statements"}, limit=2)
        assert len(data["collections"]["statements"]["items"]) == 2
        assert data["bank_statement"]["transaction_count"] == count
        assert data["collections"]["statements"]["page"]["filtered_count"] == count
        assert data["bank_statement"]["unmatched_totals"] == {
            "count": count,
            "inflow_fen": count,
            "outflow_fen": 0,
        }
        assert loaded == set()
    with Dashboard(engine)._snapshot("2026-09") as snap:
        summary = funds(snap, summary_only=True)
        assert summary["collections"] == {}
        assert "rows" not in summary["bank_statement"] and "movements" not in summary
        assert (
            summary["bank_statement"]["unmatched_totals"]
            == data["bank_statement"]["unmatched_totals"]
        )
        assert loaded == set()


def test_movement_expansion_is_limited_after_filtering(bank_book, monkeypatch):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 37 + ["bank-b"] * 4)
    loaded = set()
    original = engine.store.facts

    def watched(connection, identifiers):
        loaded.update(identifiers)
        return original(connection, identifiers)

    monkeypatch.setattr(engine.store, "facts", watched)
    with Dashboard(engine)._snapshot("2026-09") as snap:
        summary = funds(snap, summary_only=True)
        assert summary["inflow_fen"] == 41
        assert loaded == set()
    with Dashboard(engine)._snapshot("2026-09") as snap:
        page = funds(
            snap,
            sections={"movements"},
            limit=2,
            filters={"movement_account_type": "bank", "movement_account_id": "bank-b"},
        )
        assert page["inflow_fen"] == 41
        movements = page["collections"]["movements"]["items"]
        assert {row["account_id"] for row in movements} == {"bank-b"}
        assert len(loaded) == len(movements) == 2
        assert page["collections"]["movements"]["page"]["total_count"] == 41
        assert page["collections"]["movements"]["page"]["filtered_count"] == 4


def test_bank_match_lookup_does_not_rescan_all_matches_for_each_statement_row(bank_book):
    engine, save, publish, _ = bank_book
    count = 1000
    opening(save, publish)
    funding(save, publish, amount=count)
    statement(save, publish, [entry(f"row-{i}", amount=1) for i in range(count)])
    reconciliation(save, publish, [match(f"row-{i}", "funding", "funding") for i in range(count)])
    with Dashboard(engine)._snapshot("2026-09") as snap:
        reader = FundsRead(snap)
        reader.account_summary()
        reader.bank_summary()
        plan = [
            row[3]
            for row in snap.connection.execute(
                "EXPLAIN QUERY PLAN " + reader.bank_source, reader.bank_parameters
            )
        ]
        assert any("bank_reconciliation_reference" in item for item in plan)
        steps = [0]

        def progress():
            steps[0] += 100
            return 0

        snap.connection.set_progress_handler(progress, 100)
        try:
            _, rows, page = _sql_summary_page(
                snap.connection,
                reader.bank_source,
                reader.bank_parameters,
                "SELECT account_id,count(*) n FROM source_rows GROUP BY account_id",
                ("account_id", "n"),
                after=None,
                limit=2,
            )
        finally:
            snap.connection.set_progress_handler(None, 0)
        assert page["total_count"] == count
        assert all(row["state"] == "matched" for row in rows)
        assert all(row["source_rows_total_fen"] == count for row in rows)
        # Bound the complete summary/count/window work, not just LIMIT 2;
        # repeatedly scanning all matches grows quadratically with row count.
        assert steps[0] < 500_000


def test_default_funds_page_batches_actual_sql_and_keeps_exact_bank_matches(bank_book, monkeypatch):
    engine, save, publish, _ = bank_book
    opening(save, publish)
    subjects = [f"filter-2026-09-{index:04}" for index in range(30)]
    _publish_filter_funding(engine, ["bank-a"] * len(subjects))
    statement(
        save,
        publish,
        [entry(subject, day="2026-09-02", amount=1) for subject in subjects],
    )
    reconciliation(save, publish, [match(subject, "funding", subject) for subject in subjects])

    calls = Counter()
    original = _PrivateConnection.execute

    def observed(connection, sql, *args, **kwargs):
        calls[" ".join(sql.split())] += 1
        return original(connection, sql, *args, **kwargs)

    monkeypatch.setattr(_PrivateConnection, "execute", observed)
    with Dashboard(engine)._snapshot("2026-09") as snap:
        data = funds(snap, sections={"movements", "statements"}, limit=30)
    movements = data["collections"]["movements"]["items"]
    statements = data["collections"]["statements"]["items"]
    assert len(movements) == len(statements) == 30
    assert all(item["state"] == "matched" for item in statements)
    assert {item["reference"] for item in statements} == set(subjects)
    assert {item["party_sources"][0]["party_id"] for item in statements} == {
        f"filter-owner-2026-09-{index:04}" for index in range(30)
    }
    assert (
        sum(
            count
            for sql, count in calls.items()
            if "FROM display_profile_revision p" in sql and "p.kind=?" in sql
        )
        <= 2
    )
    assert (
        sum(
            count
            for sql, count in calls.items()
            if "FROM management_revision p" in sql and "p.subject_id IN" in sql
        )
        <= 2
    )
    assert sum(count for sql, count in calls.items() if "WITH RECURSIVE selected(id)" in sql) <= 2
    assert (
        sum(
            count
            for sql, count in calls.items()
            if "json_array_length(c.outcome,'$.lines') AS line_count" in sql
        )
        <= 6
    )
    assert (
        sum(
            count
            for sql, count in calls.items()
            if "JOIN calculation c INDEXED BY calculation_subject" in sql
            and "JOIN dependency_calculation d INDEXED BY dependency_upstream" in sql
        )
        == 1
    )

    def prior_page_load(read, identifiers):
        identifiers = set(identifiers)
        if not identifiers:
            return
        read.snap.reads.prime_calculations(identifiers, ancestors=True)
        read.snap.reads.relations_many(identifiers)
        subjects = {read.snap.calculation(ident)["subject_id"] for ident in identifiers}
        read.snap.metadata.prime_profiles("business", subjects)
        read.snap.management.prime(subjects)

    with Dashboard(engine)._snapshot("2026-09") as snap:
        with monkeypatch.context() as patch:
            patch.setattr(FundsRead, "prepare_calculation_items", prior_page_load)
            prior = funds(snap, sections={"movements", "statements"}, limit=30)
        current = funds(snap, sections={"movements", "statements"}, limit=30)
        assert prior == current
