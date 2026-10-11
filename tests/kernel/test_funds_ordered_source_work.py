"""Business sorting consumes scalar keys from one complete SQL event source."""

import gc
import sqlite3
import weakref

import pytest
import test_banking as banking
from test_funds_summary_page import _steps
from test_integrity_content import damage
from test_reimbursement_assets import asset

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_funds import _sql_summary_page
from ai_accounting.kernel.entities import employee_entities

bank_book = banking.book


@pytest.mark.parametrize("size", [8, 64, 502])
def test_ordered_source_is_evaluated_once_with_full_summary_and_bounded_payload(size):
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE event(page_key TEXT, account TEXT, amount INTEGER)")
    connection.executemany(
        "INSERT INTO event VALUES(?,?,?)",
        [(f"{index:04}", "a" if index % 2 == 0 else "b", index) for index in range(size)],
    )
    visits = []
    connection.create_function("visit", 1, lambda key: visits.append(key) or key)
    seen = []

    def reverse_order(rows):
        seen.extend(rows)
        assert all(set(row) == {"page_key", "calculation_id", "internal_transfer"} for row in rows)
        # The real business sorter performs necessary reads on this connection.
        assert connection.execute("SELECT count(*) FROM event").fetchone()[0] == size
        return sorted((row["page_key"] for row in rows), reverse=True)

    options = dict(
        connection=connection,
        source="SELECT visit(page_key) page_key,page_key calculation_id,0 internal_transfer,"
        "account,amount,zeroblob(1024) payload FROM event",
        parameters=(),
        summary_sql="SELECT account,sum(amount) amount,count(*) n "
        "FROM source_rows GROUP BY account",
        summary_columns=("account", "amount", "n"),
        after=None,
        limit=2,
        where="account=?",
        filters=("a",),
        order_rows=reverse_order,
    )
    summary, rows, page = _sql_summary_page(**options)
    assert len(visits) == size
    assert len(seen) == size // 2
    assert summary == [
        {"account": account, "amount": sum(range(start, size, 2)), "n": size // 2}
        for account, start in (("a", 0), ("b", 1))
    ]
    assert [row["page_key"] for row in rows] == [f"{size - 2:04}", f"{size - 4:04}"]
    assert len(rows) == 2 and all(len(row["payload"]) == 1024 for row in rows)
    assert page["total_count"] == size and page["filtered_count"] == size // 2
    assert page["has_more"] and page["next_cursor"] == rows[-1]["page_key"]
    visits.clear()
    _, remaining, last = _sql_summary_page(**(options | {"after": "0000"}))
    assert remaining == [] and not last["has_more"]
    assert len(visits) == size
    if size == 502:
        _, maximum, maximum_page = _sql_summary_page(
            **(options | {"limit": 500, "where": "1=1", "filters": ()})
        )
        assert len(maximum) == 500 and maximum_page["has_more"]


def test_ordered_source_preserves_unknown_summary_cursor_error_and_retry():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    options = dict(
        connection=connection,
        source="SELECT 'a' page_key,'calc' calculation_id,0 internal_transfer,NULL amount",
        parameters=(),
        summary_sql="SELECT sum(amount) amount FROM source_rows",
        summary_columns=("amount",),
        after=None,
        limit=1,
        order_rows=lambda rows: [row["page_key"] for row in rows],
    )
    assert _sql_summary_page(**options)[0] == [{"amount": None}]
    with pytest.raises(KernelError) as failure:
        _sql_summary_page(**(options | {"after": "missing"}))
    assert failure.value.code == "dashboard_snapshot_changed"
    error = KernelError("content_integrity_failed", "排序来源损坏")

    def failed_order(_rows):
        raise error

    with pytest.raises(KernelError) as failure:
        _sql_summary_page(**(options | {"order_rows": failed_order}))
    assert failure.value is error
    summary, rows, page = _sql_summary_page(**options)
    assert summary == [{"amount": None}] and len(rows) == 1 and not page["has_more"]
    empty = _sql_summary_page(**(options | {"where": "0=1"}))
    assert empty[0] == summary and empty[1] == []
    assert empty[2]["total_count"] == 1 and empty[2]["filtered_count"] == 0


def test_same_connection_keeps_prepared_reads_and_releases_sort_request():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    options = dict(
        connection=connection,
        source="SELECT 'a' page_key,'calc' calculation_id,0 internal_transfer,1 amount",
        parameters=(),
        summary_sql="SELECT sum(amount) amount FROM source_rows",
        summary_columns=("amount",),
        after=None,
        limit=1,
    )

    class Order:
        def __call__(self, rows):
            return [row["page_key"] for row in rows]

    request = Order()
    reference = weakref.ref(request)
    _sql_summary_page(**options, order_rows=request)
    del request
    gc.collect()
    assert reference() is None
    compilations = []
    connection.set_authorizer(lambda *args: compilations.append(args) or sqlite3.SQLITE_OK)
    connection.execute("SELECT 123").fetchall()
    for _ in range(3):
        _sql_summary_page(**options, order_rows=Order())
        compilations.clear()
        connection.execute("SELECT 123").fetchall()
        assert compilations == []


@pytest.mark.parametrize("corruption", ["source", "current_role"])
def test_public_employee_membership_keeps_work_bounded_by_actual_roles_and_proves_sources(
    bank_book,
    corruption,
):
    engine, save, publish, _ = bank_book
    banking.funding(save, publish)
    saved = save(
        "reimbursed_asset",
        "accepted",
        asset(
            asset_id="item",
            creditors=[{"employee_id": "owner", "amount_fen": 120000}],
        ),
    )

    def selected():
        with engine.store.connection(read_only=True) as connection:
            return _steps(
                connection,
                lambda: employee_entities(
                    connection,
                    "2026-09",
                    registry=engine.store.registry,
                ),
            )

    before, before_steps = selected()
    assert before == ["owner"]
    for index in range(40):
        banking.funding(
            save,
            publish,
            subject=f"unrelated-{index}",
            amount=index + 1,
            day="2026-08-02",
            posted=False,
        )
    publish(*(f"unrelated-{index}" for index in range(40)))
    after, after_steps = selected()
    assert after == before
    assert after_steps <= before_steps + 100
    if corruption == "source":
        damage(
            engine,
            "fact_revision",
            "DELETE FROM fact_revision WHERE id=?",
            (saved["fact_id"],),
            foreign_keys=False,
        )
    else:
        damage(
            engine,
            "entity_reference_current",
            "UPDATE entity_reference_current SET source_digest=? "
            "WHERE fact_id=? AND role='employee'",
            (b"\0" * 32, saved["fact_id"]),
        )
    with pytest.raises(KernelError):
        selected()
