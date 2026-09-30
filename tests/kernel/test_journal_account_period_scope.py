"""A bounded account query must not scan unrelated historical months."""

import sqlite3

import pytest

from ai_accounting.kernel.dashboard_reads import _account_voucher_ids


@pytest.fixture
def connection():
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE voucher_version(id TEXT PRIMARY KEY, voucher_id TEXT,"
            "period INTEGER, reverses_id TEXT);"
            "CREATE INDEX voucher_period ON voucher_version(period,voucher_id,id);"
            "CREATE TABLE voucher_line(version_id TEXT,line_no INTEGER,account TEXT,"
            "PRIMARY KEY(version_id,line_no));"
            "CREATE INDEX voucher_line_account ON voucher_line(account,version_id,line_no);"
        )
        yield connection


def add_voucher(connection, ident, period, accounts, *, reverses_id=None):
    connection.execute(
        "INSERT INTO voucher_version VALUES(?,?,?,?)", (ident, ident, period, reverses_id)
    )
    connection.executemany(
        "INSERT INTO voucher_line VALUES(?,?,?)",
        [(ident, index, account) for index, account in enumerate(accounts, 1)],
    )


def measured_candidates(connection, accounts, cutoff, **kwargs):
    steps = 0

    def progress():
        nonlocal steps
        steps += 1
        return 0

    connection.set_progress_handler(progress, 1)
    try:
        result = _account_voucher_ids(connection, accounts, cutoff, **kwargs)
    finally:
        connection.set_progress_handler(None, 0)
    return result, steps


def test_account_candidates_preserve_posting_cutoff_and_all_history(connection):
    add_voucher(connection, "previous", 98, ["1002", "1002", "5602"])
    add_voucher(connection, "current", 100, ["1001", "5602"])
    add_voucher(connection, "reversal", 100, ["1002", "5602"], reverses_id="previous")
    add_voucher(connection, "other", 100, ["222101", "5602"])
    add_voucher(connection, "future", 101, ["1002", "5602"])
    assert _account_voucher_ids(connection, {"1002", "1001"}, 100, posting_period=100) == {
        "current", "reversal"
    }
    assert _account_voucher_ids(connection, ["1002", "1002"], 100) == {
        "previous", "reversal"
    }
    assert _account_voucher_ids(connection, {"1002"}, 99, posting_period=100) == set()
    assert _account_voucher_ids(connection, {"1002"}, 100, posting_period=99) == set()
    assert _account_voucher_ids(connection, set(), 100, posting_period=100) == set()
    assert _account_voucher_ids(connection, set(), 100) == set()


@pytest.mark.parametrize("count", [1, 100, 500])
def test_month_account_work_does_not_grow_with_unrelated_history(connection, count):
    expected = {f"selected-{index}" for index in range(count)}
    for ident in expected:
        add_voucher(connection, ident, 24000, ["1002", "5602"])
    baseline, baseline_steps = measured_candidates(
        connection, {"1002"}, 24000, posting_period=24000
    )
    assert baseline == expected
    previous_months = 0
    for months in (12, 48, 120):
        for month in range(previous_months, months):
            for index in range(100):
                add_voucher(
                    connection, f"history-{month}-{index}", 23999 - month,
                    ["1002", "5602"],
                )
        previous_months = months
        result, steps = measured_candidates(
            connection, {"1002"}, 24000, posting_period=24000
        )
        assert result == expected
        # Index depth may change; scanning the growing 1,200/4,800/12,000
        # unrelated same-account vouchers must not dominate this fixed month.
        assert steps <= baseline_steps + 100
