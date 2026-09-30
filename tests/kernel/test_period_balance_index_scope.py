"""The verified open-tail balance read must not visit unrelated historical rows."""

import sqlite3

from ai_accounting.kernel.period_balances import _sum_projection_rows


def _work(months, other_current_rows, *, keys=None):
    connection = sqlite3.connect(":memory:")
    connection.execute(
        "CREATE TABLE period_balance(posting_period INTEGER,category TEXT,"
        "balance_key TEXT,amount INTEGER)"
    )
    connection.execute(
        "CREATE INDEX period_balance_key ON period_balance(category,balance_key,posting_period)"
    )
    connection.execute(
        "CREATE INDEX period_balance_period ON period_balance(posting_period,category)"
    )
    connection.executemany(
        "INSERT INTO period_balance VALUES(?,?,?,?)",
        (
            (month, category, f"account-{key:03d}", key + month)
            for month in range(1, months + 1)
            for category in ("bank", "cash", "platform")
            for key in range(100)
        ),
    )
    connection.executemany(
        "INSERT INTO period_balance VALUES(?,?,?,?)",
        ((months, "asset", f"asset-{key:05d}", key) for key in range(other_current_rows)),
    )
    connection.commit()
    connection.execute("ANALYZE")
    vm = [0]
    connection.set_progress_handler(lambda: vm.__setitem__(0, vm[0] + 100) or 0, 100)
    try:
        rows = _sum_projection_rows(
            connection,
            "posting_period>? AND posting_period<=?",
            [months - 1, months],
            ("bank", "cash", "platform"),
            keys,
            periods={months} if keys is None else None,
        )
    finally:
        connection.set_progress_handler(None, 0)
        connection.close()
    return rows, vm[0]


def test_open_tail_balance_work_stays_bounded_by_selected_month_and_category():
    baseline = None
    for months in (12, 48, 120):
        for other_current_rows in (0, 5000, 50000):
            rows, vm = _work(months, other_current_rows)
            assert rows == [
                {"category": category, "key": f"account-{key:03d}", "amount": key + months}
                for category in ("bank", "cash", "platform")
                for key in range(100)
            ]
            if baseline is None:
                baseline = vm
            assert vm <= baseline * 2


def test_exact_balance_keys_keep_the_category_key_lookup():
    for months in (12, 48, 120):
        rows, vm = _work(months, 50000, keys={"account-001"})
        assert rows == [
            {"category": category, "key": "account-001", "amount": months + 1}
            for category in ("bank", "cash", "platform")
        ]
        assert vm < 2000
