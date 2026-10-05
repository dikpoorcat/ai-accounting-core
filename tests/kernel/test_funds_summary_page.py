"""The funds page shares each event source without changing its summary or page."""

import sqlite3

import pytest
import test_banking as banking
import test_deletion_boundaries as unrelated
import test_investments as investments
from test_dashboard_funds_alignment import _publish_filter_funding

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import (
    FundsRead,
    _sql_summary_page,
    funds,
)

bank_book = banking.book
investment_book = investments.book


def _steps(connection, call):
    steps = [0]

    def progress():
        steps[0] += 100
        return 0

    connection.set_progress_handler(progress, 100)
    try:
        result = call()
    finally:
        connection.set_progress_handler(None, 0)
    return result, steps[0]


def _source_rows(rows):
    metadata = {"summary_json", "total_count", "filtered_count", "cursor_present"}
    return [{key: row[key] for key in row.keys() if key not in metadata} for row in rows]


def _independent_page(connection, source, parameters, *, after, limit):
    """Small synthetic test oracle, independent of the production pager SQL."""
    all_rows = [
        dict(row)
        for row in connection.execute(
            "WITH source_rows AS MATERIALIZED (" + source + ") "
            "SELECT * FROM source_rows ORDER BY page_key",
            parameters,
        )
    ]
    assert after is None or any(row["page_key"] == after for row in all_rows)
    selected = [row for row in all_rows if row["page_key"] > (after or "")]
    items = selected[:limit]
    more = len(selected) > limit
    return items, {
        "total_count": len(all_rows),
        "filtered_count": len(all_rows),
        "returned_count": len(items),
        "has_more": more,
        "next_cursor": items[-1]["page_key"] if more else None,
    }


def _bounded_source_page(connection, source, parameters, *, after, limit):
    """The former separate count and bounded page, without loading all rows."""
    count = connection.execute(
        "WITH source_rows AS MATERIALIZED (" + source + ") SELECT count(*) FROM source_rows",
        parameters,
    ).fetchone()[0]
    found = list(
        connection.execute(
            "WITH source_rows AS MATERIALIZED (" + source + ") "
            "SELECT * FROM source_rows WHERE (? IS NULL OR page_key>?) "
            "ORDER BY page_key LIMIT ?",
            (*parameters, after, after, limit + 1),
        )
    )
    rows = [dict(row) for row in found[:limit]]
    more = len(found) > limit
    return rows, {
        "total_count": count,
        "filtered_count": count,
        "returned_count": len(rows),
        "has_more": more,
        "next_cursor": rows[-1]["page_key"] if more else None,
    }


def test_shared_sql_preserves_global_summary_filtered_page_and_cursor():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE event(page_key TEXT, account TEXT, amount INTEGER)")
    connection.executemany(
        "INSERT INTO event VALUES(?,?,?)",
        [("a", "bank-a", 3), ("b", "bank-b", -2), ("c", "bank-a", 5)],
    )
    source = "SELECT * FROM event"
    summary = "SELECT account,sum(amount) amount,count(*) n FROM source_rows GROUP BY account"
    kwargs = {
        "after": None,
        "limit": 1,
        "where": "account=?",
        "filters": ("bank-a",),
    }
    summaries, rows, page = _sql_summary_page(
        connection, source, (), summary, ("account", "amount", "n"), **kwargs
    )
    assert summaries == [
        {"account": "bank-a", "amount": 8, "n": 2},
        {"account": "bank-b", "amount": -2, "n": 1},
    ]
    assert [row["page_key"] for row in rows] == ["a"]
    assert page == {
        "total_count": 3,
        "filtered_count": 2,
        "returned_count": 1,
        "has_more": True,
        "next_cursor": "a",
    }
    _, rows, final_page = _sql_summary_page(
        connection,
        source,
        (),
        summary,
        ("account", "amount", "n"),
        **(kwargs | {"after": "c"}),
    )
    assert rows == [] and final_page["total_count"] == 3
    with pytest.raises(KernelError) as failure:
        _sql_summary_page(
            connection,
            source,
            (),
            summary,
            ("account", "amount", "n"),
            **(kwargs | {"after": "missing"}),
        )
    assert failure.value.code == "dashboard_snapshot_changed"


def test_real_movement_and_statement_share_full_summary_with_bounded_pages(bank_book):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    _publish_filter_funding(engine, ["bank-a", "bank-a", "bank-b"])
    banking.statement(
        save,
        publish,
        [banking.entry("filter-2026-09-0000", amount=1)],
    )
    dashboard = Dashboard(engine)
    with dashboard._snapshot("2026-09") as snap:
        selected = funds(snap, sections={"movements", "statements"}, limit=1)
        baseline = FundsRead(snap)
        expected_totals = baseline.account_summary()
        assert {key: selected[key] for key in expected_totals} == expected_totals
        bank_summary = baseline.bank_summary()
        assert {
            key: value for key, value in selected["bank_statement"].items()
            if key != "review_state"
        } == {
            key: value for key, value in bank_summary.items()
            if key not in {
                "matched_count", "unmatched_count", "needs_review_count", "unmatched_totals"
            }
        }
        assert selected["bank_statement"]["review_state"] == "pending"
        for section, source, parameters in (
            ("movements", *baseline.movements()),
            ("statements", baseline.bank_source, baseline.bank_parameters),
        ):
            rows, page = _independent_page(snap.connection, source, parameters, after=None, limit=1)
            assert selected["collections"][section]["page"] == page
            assert len(selected["collections"][section]["items"]) == len(rows) == 1
        first_cursor = selected["collections"]["movements"]["page"]["next_cursor"]
        last = funds(
            snap,
            sections={"movements"},
            cursors={"movements": first_cursor},
            limit=500,
        )
        assert last["collections"]["movements"]["page"]["has_more"] is False
        final_cursor = last["collections"]["movements"]["items"][-1]["id"]
        empty = funds(
            snap,
            sections={"movements"},
            cursors={"movements": final_cursor},
            limit=1,
        )
        assert empty["collections"]["movements"]["items"] == []
        assert empty["collections"]["movements"]["page"]["total_count"] == 3


def test_real_investment_event_summary_and_page_remain_separate_from_cost(investment_book):
    engine, save, publish = investment_book
    for subject in ("first", "second"):
        save("money_fund_subscription", subject, investments.subscription())
    publish("first", "second")
    with Dashboard(engine)._snapshot("2026-01") as snap:
        selected = funds(snap, sections={"investment_events"}, limit=1)
        baseline = FundsRead(snap)
        expected = baseline.investment_summary()
        assert selected["investments"] == expected
        source, parameters = baseline.investment_events()
        rows, page = _independent_page(snap.connection, source, parameters, after=None, limit=1)
        assert len(rows) == len(selected["collections"]["investment_events"]["items"]) == 1
        assert selected["collections"]["investment_events"]["page"] == page
        assert expected["closing_cost_fen"] == 20200


def test_nonempty_bank_and_empty_last_page_work_from_one_source(bank_book):
    engine, save, publish, _ = bank_book
    banking.opening(save, publish)
    banking.statement(
        save,
        publish,
        [banking.entry(f"row-{index}", amount=1) for index in range(40)],
    )
    with Dashboard(engine)._snapshot("2026-09") as snap:
        old = FundsRead(snap)
        old.account_summary()

        def separate():
            summary = old.bank_summary()
            rows, page = _independent_page(
                snap.connection, old.bank_source, old.bank_parameters, after=None, limit=3
            )
            return summary, rows, page

        expected = separate()
        new = FundsRead(snap)
        new.account_summary()
        actual, shared_steps = _steps(
            snap.connection,
            lambda: new.bank_summary(page_request={"after": None, "limit": 3}),
        )
        rows, page = new.shared_pages["statements"]
        assert (actual, _source_rows(rows), page) == expected
        assert shared_steps < 12_000

        cursor = _independent_page(
            snap.connection,
            old.bank_source,
            old.bank_parameters,
            after=None,
            limit=40,
        )[0][-1]["page_key"]
        old_rows, old_page = _independent_page(
            snap.connection, old.bank_source, old.bank_parameters, after=cursor, limit=3
        )
        _, new_empty_steps = _steps(
            snap.connection,
            lambda: new.bank_summary(page_request={"after": cursor, "limit": 3}),
        )
        new_rows, new_page = new.shared_pages["statements"]
        assert _source_rows(new_rows) == old_rows == []
        assert new_page == old_page
        assert new_empty_steps < 12_000


def test_nonempty_investment_event_work_preserves_history_cost(investment_book):
    engine, save, publish = investment_book
    for index in range(12):
        save("money_fund_subscription", f"buy-{index}", investments.subscription())
    publish(*(f"buy-{index}" for index in range(12)))
    dashboard = Dashboard(engine)

    def compare():
        with dashboard._snapshot("2026-01") as snap:
            old = FundsRead(snap)
            old.account_summary()
            old.bank_summary()

            def separate():
                summary = old.investment_summary()
                source, parameters = old.investment_events()
                rows, page = _bounded_source_page(
                    snap.connection, source, parameters, after=None, limit=3
                )
                return summary, rows, page

            expected, separate_steps = _steps(snap.connection, separate)
        with dashboard._snapshot("2026-01") as snap:
            new = FundsRead(snap)
            new.account_summary()
            new.bank_summary()
            actual, shared_steps = _steps(
                snap.connection,
                lambda: new.investment_summary(page_request={"after": None, "limit": 3}),
            )
            rows, page = new.shared_pages["investment_events"]
            assert (actual, _source_rows(rows), page) == expected
        return expected, separate_steps, shared_steps

    expected, separate_steps, shared_steps = compare()
    assert shared_steps < separate_steps
    for index in range(60):
        save("expense", f"unrelated-{index}", unrelated.expense(period="2025-12"))
    publish(*(f"unrelated-{index}" for index in range(60)))
    grown_expected, grown_separate, grown_shared = compare()
    assert grown_expected == expected
    assert grown_shared < grown_separate
    assert grown_shared - shared_steps <= grown_separate - separate_steps + 2_000


def test_real_current_movement_work_is_bounded_as_unrelated_history_grows(bank_book, monkeypatch):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 24)
    dashboard = Dashboard(engine)
    measurements = []
    original = _sql_summary_page

    def counted(connection, *args, **kwargs):
        result, steps = _steps(connection, lambda: original(connection, *args, **kwargs))
        measurements.append(steps)
        return result

    monkeypatch.setattr("ai_accounting.kernel.dashboard_funds._sql_summary_page", counted)
    before = dashboard.funds("2026-09", section="movements", limit=2)["data"]
    _publish_filter_funding(engine, ["bank-a"] * 80, period="2026-08")
    after = dashboard.funds("2026-09", section="movements", limit=2)["data"]
    assert before["collections"]["movements"]["items"] == after["collections"]["movements"]["items"]
    assert before["collections"]["movements"]["page"]["total_count"] == 24
    assert after["collections"]["movements"]["page"]["total_count"] == 24
    assert after["opening_fen"] == before["opening_fen"] + 80
    assert measurements[1] <= measurements[0] + 2_000
