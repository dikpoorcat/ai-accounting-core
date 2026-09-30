"""The full voucher page keeps exact sources while page-local loads stay batched."""

from collections import Counter
from sqlite3 import Row, connect
from types import SimpleNamespace
from unittest.mock import patch

from test_banking import book as _bank_book
from test_dashboard_funds_alignment import _publish_filter_funding

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_reads import Journal
from ai_accounting.kernel.query_reads import selected_voucher_sql
from ai_accounting.kernel.runtime import _PrivateConnection

bank_book = _bank_book


def test_brief_voucher_page_and_distant_focus_batch_sql_without_losing_sources(
    bank_book, monkeypatch
):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 31)
    calls = Counter()
    original = _PrivateConnection.execute

    def observed(connection, sql, *args, **kwargs):
        calls[" ".join(sql.split())] += 1
        return original(connection, sql, *args, **kwargs)

    monkeypatch.setattr(_PrivateConnection, "execute", observed)
    response = Dashboard(engine).brief(
        "2026-09", limit=30, voucher_number=31, preparation="deferred"
    )
    data = response["data"]
    page = data["collections"]["vouchers"]
    assert page["page"]["total_count"] == 31
    assert page["page"]["returned_count"] == len(page["items"]) == 30
    assert {item["number"] for item in page["items"]} == {str(i) for i in range(1, 31)}
    assert data["focused_voucher"]["number"] == "31"
    assert data["focused_voucher"]["voucher_version_id"] not in {
        item["voucher_version_id"] for item in page["items"]
    }
    for index, item in enumerate([*page["items"], data["focused_voucher"]]):
        assert item["business_amount_fen"] == 1
        assert item["components"][0]["party_sources"][0]["party_id"] == (
            f"filter-owner-2026-09-{index:04}"
        )
        assert item["evidence"] and item["lines"]
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
    assert (
        sum(
            count
            for sql, count in calls.items()
            if "JOIN voucher_line l ON l.version_id=ids.value" in sql
        )
        <= 2
    )
    assert (
        sum(
            count
            for sql, count in calls.items()
            if "JOIN dependency_fact d ON d.calculation_id=ids.value" in sql
        )
        == 1
    )


def test_journal_query_and_count_reuse_stays_inside_one_page_snapshot(bank_book, monkeypatch):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 5)
    calls = []
    original = _PrivateConnection.execute

    def observed(connection, sql, *args, **kwargs):
        calls.append(" ".join(sql.split()))
        return original(connection, sql, *args, **kwargs)

    monkeypatch.setattr(_PrivateConnection, "execute", observed)
    dashboard = Dashboard(engine)
    for _ in range(2):
        with dashboard._snapshot("2026-09") as snapshot:
            calls.clear()
            journal = snapshot.month_journal
            with patch(
                "ai_accounting.kernel.dashboard_reads.selected_voucher_sql",
                wraps=selected_voucher_sql,
            ) as selected:
                first, first_page = journal.page(0, 2)
                second, second_page = journal.page(first[-1]["number"], 2)
                assert len(journal) == first_page["total_count"] == second_page["total_count"] == 5
                assert {row["id"] for row in first}.isdisjoint(row["id"] for row in second)
                assert selected.call_count == 1
                assert selected.call_args.kwargs["no_close_references"] is True
            assert sum(sql.startswith("SELECT count(*) FROM (") for sql in calls) == 1


def test_brief_journal_summary_uses_one_selection_for_page_and_all_totals(bank_book, monkeypatch):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 5)
    calls = []
    original = _PrivateConnection.execute

    def observed(connection, sql, *args, **kwargs):
        calls.append(" ".join(sql.split()))
        return original(connection, sql, *args, **kwargs)

    monkeypatch.setattr(_PrivateConnection, "execute", observed)
    dashboard = Dashboard(engine)
    for _ in range(2):
        with dashboard._snapshot("2026-09") as snapshot:
            calls.clear()
            journal = snapshot.month_journal
            reads = snapshot.reads
            with patch(
                "ai_accounting.kernel.dashboard_reads.selected_voucher_sql",
                wraps=selected_voucher_sql,
            ) as selected:
                summary = journal.prime_summary()
                first, first_page = journal.page(0, 2)
                second, second_page = journal.page(first[-1]["number"], 2)
                last, last_page = journal.page(second[-1]["number"], 2)
                assert len(journal) == 5
                assert {
                    first_page["total_count"],
                    second_page["total_count"],
                    last_page["total_count"],
                } == {5}
                assert [len(first), len(second), len(last)] == [2, 2, 1]
                assert [
                    first_page["has_more"],
                    second_page["has_more"],
                    last_page["has_more"],
                ] == [True, True, False]
                assert sum(item["count"] for item in journal.kind_counts()) == summary["count"]
                assert journal.totals() == summary["totals"]
                assert selected.call_count == 1
            assert sum("count(DISTINCT j.id) count" in sql for sql in calls) == 1
            assert not any(sql.startswith("SELECT count(*) FROM (") for sql in calls)
            assert not any(sql.startswith("SELECT count(*) AS line_count") for sql in calls)
            assert not any(sql.startswith("SELECT basis_kind AS kind") for sql in calls)
            funding = journal.select(kinds={"funding"})
            funding.prime_summary()
            assert len(funding) == 5
            assert funding.kind_counts() == [{"kind": "funding", "reversal": 0, "count": 5}]
            empty = journal.select(kinds={"nonexistent"})
            empty.prime_summary()
            assert len(empty) == 0
            assert empty.kind_counts() == []
            assert empty.totals() == {"line_count": 0, "debit": 0, "credit": 0}
        assert reads._report_snapshot_cache == {}


def test_journal_summary_preserves_zero_line_count_and_reversal_group():
    connection = connect(":memory:")
    connection.row_factory = Row
    connection.executescript(
        "CREATE TABLE picked (id TEXT PRIMARY KEY,basis_kind TEXT,reverses_id TEXT);"
        "CREATE TABLE voucher_line (version_id TEXT,debit INTEGER,credit INTEGER);"
        "INSERT INTO picked VALUES ('a','expense',NULL),('b','expense','a'),('c','wage',NULL);"
        "INSERT INTO voucher_line VALUES ('a',100,0),('a',0,100),('b',0,50),('b',50,0);"
    )
    reads = SimpleNamespace(_snapshot_active=True, _report_snapshot_cache={})
    snapshot = SimpleNamespace(connection=connection, reads=reads, month=1)
    journal = Journal(snapshot, month=1)
    journal.sql = lambda: ("SELECT id,basis_kind,reverses_id FROM picked", [])
    try:
        journal.prime_summary()
        assert len(journal) == 3
        assert journal.kind_counts() == [
            {"kind": "expense", "reversal": 0, "count": 1},
            {"kind": "expense", "reversal": 1, "count": 1},
            {"kind": "wage", "reversal": 0, "count": 1},
        ]
        assert journal.totals() == {"line_count": 4, "debit": 150, "credit": 150}
        connection.execute("DELETE FROM picked")
        reads._report_snapshot_cache.clear()
        assert journal.prime_summary()["count"] == 0
        assert journal.kind_counts() == []
        assert journal.totals() == {"line_count": 0, "debit": 0, "credit": 0}
    finally:
        connection.close()


def test_brief_asset_counts_do_not_load_consumption_histories(tmp_path, monkeypatch):
    from test_asset_batch_reads import prepare_batch_assets
    from test_banking import consume_assets
    from test_payroll_corrections import Company

    from ai_accounting.kernel.business_queries import BusinessQueries
    from ai_accounting.kernel.dashboard import _long_term_assets

    company = Company(tmp_path / "asset-summary.sqlite")
    prepare_batch_assets(company)
    for month in ("2026-02", "2026-03", "2026-04"):
        consume_assets(company.engine, company.owner_confirmation, month)

    def unexpected_charge_history(*args, **kwargs):
        raise AssertionError("card counts must not load consumption history")

    monkeypatch.setattr(BusinessQueries, "_selected_asset_member_heads", unexpected_charge_history)
    dashboard = Dashboard(company.engine)
    with dashboard._snapshot("2026-04") as snapshot:
        values = _long_term_assets(snapshot)
    # March and April each charge 120000 / 12 + 30000 / 6.
    assert values == {
        "net_fen": 120000,
        "fixed_net_fen": 120000,
        "intangible_net_fen": 0,
        "fixed_active_count": 2,
        "intangible_active_count": 0,
        "pending_count": 0,
        "project_cost_fen": 0,
    }
