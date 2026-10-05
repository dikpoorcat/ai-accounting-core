"""Exact open months and arbitrary earlier history keep different source scopes."""

import sqlite3

import pytest

from ai_accounting.kernel.report_projection import _has_accounting_history_before
from ai_accounting.kernel.reports import _has_open_publication


@pytest.fixture
def database():
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE calculation_publication(posting_period INTEGER);"
            "CREATE INDEX publication_posting ON calculation_publication(posting_period);"
            "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
            "CREATE TABLE voucher_version(id TEXT PRIMARY KEY,period INTEGER);"
            "CREATE INDEX voucher_period ON voucher_version(period,id);"
            "CREATE TABLE voucher_current(version_id TEXT UNIQUE);"
        )
        yield connection


def _voucher(connection, ident, period, *, current=True):
    connection.execute("INSERT INTO voucher_version VALUES(?,?)", (ident, period))
    if current:
        connection.execute("INSERT INTO voucher_current VALUES(?)", (ident,))


def test_missing_publication_is_visible_in_exact_open_month_and_ignores_future(database):
    assert not _has_open_publication(database, 10)
    _voucher(database, "unpublished", 3)
    _voucher(database, "future", 11)
    _voucher(database, "withdrawn", 1, current=False)
    database.execute("INSERT INTO period_close VALUES(8)")
    assert not _has_open_publication(database, 2)
    assert _has_open_publication(database, 3)
    # A later close cannot erase an earlier physical open voucher.
    assert _has_open_publication(database, 10)
    database.execute("INSERT INTO period_close VALUES(3)")
    assert not _has_open_publication(database, 10)
    assert _has_open_publication(database, 11)
    database.execute("DELETE FROM voucher_current WHERE version_id='future'")
    assert not _has_open_publication(database, 11)


def test_history_probe_includes_closed_history_but_keeps_strict_cutoff(database):
    _voucher(database, "unpublished", 3)
    database.execute("INSERT INTO period_close VALUES(3)")
    assert not _has_open_publication(database, 3)
    assert not _has_accounting_history_before(database, 3)
    assert _has_accounting_history_before(database, 4)
    database.execute("DELETE FROM period_close")
    assert _has_accounting_history_before(database, 4)
    database.execute("DELETE FROM voucher_current")
    assert not _has_accounting_history_before(database, 4)
    database.execute("INSERT INTO calculation_publication VALUES(3)")
    assert _has_accounting_history_before(database, 4)


def test_physical_probe_work_scales_with_actual_months_not_closed_voucher_count(database):
    def measured():
        steps = 0

        def progress():
            nonlocal steps
            steps += 1
            return 0

        database.set_progress_handler(progress, 1)
        try:
            assert not _has_open_publication(database, 10)
        finally:
            database.set_progress_handler(None, 0)
        return steps

    for month in (1, 3, 8):
        _voucher(database, str(month), month)
        database.execute("INSERT INTO period_close VALUES(?)", (month,))
    before = measured()
    for month in (1, 3, 8):
        for number in range(1000):
            _voucher(database, f"{month}:{number}", month)
    after = measured()
    assert after < before + 100, (before, after)
    # An unclosed month containing only retired versions is still not open.
    for number in range(1000):
        _voucher(database, f"retired:{number}", 5, current=False)
    assert not _has_open_publication(database, 10)
    _voucher(database, "live-in-hole", 5)
    assert _has_open_publication(database, 10)
