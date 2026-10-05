"""An asset tail locator keeps every owner while avoiding unrelated closed history."""

import sqlite3

import pytest

from ai_accounting.kernel.dashboard import _asset_open_batch_owners
from ai_accounting.kernel.types import YearMonth


def book():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE calculation(id TEXT PRIMARY KEY,kind TEXT,period INTEGER);"
        "CREATE INDEX calculation_kind_period ON calculation(kind,period,id);"
        "CREATE TABLE calculation_publication(id TEXT PRIMARY KEY,"
        "posting_period INTEGER,calculation_id TEXT UNIQUE,baseline_calculation_id TEXT);"
        "CREATE INDEX publication_posting ON calculation_publication(posting_period,id);"
    )
    january, february, march, april = (
        YearMonth(f"2026-{month:02}").ordinal for month in range(1, 5)
    )
    connection.executemany(
        "INSERT INTO calculation VALUES(?,?,?)",
        [("closed-owner", "asset_consumption_month", january),
         ("baseline-only", "asset_consumption_month", january),
         ("replaced-owner", "asset_consumption_month", february),
         ("current-owner", "asset_consumption_month", march),
         ("correction", "cash_payment", march),
         ("future-owner", "asset_consumption_month", april)],
    )
    connection.executemany(
        "INSERT INTO calculation_publication VALUES(?,?,?,?)",
        [("closed", january, "closed-owner", None),
         ("previous", february, "replaced-owner", None),
         ("current", march, "current-owner", "replaced-owner"),
         ("corrected", march, "correction", "baseline-only"),
         ("withdrawn", march, None, "closed-owner"),
         ("future", april, "future-owner", "current-owner")],
    )
    return connection, february, march


def original(connection, through, after):
    return {row[0] for row in connection.execute(
        "SELECT c.id FROM calculation_publication p JOIN calculation c "
        "ON c.id=p.calculation_id WHERE p.posting_period<=? "
        "AND (? IS NULL OR p.posting_period>?) AND c.kind='asset_consumption_month' "
        "UNION SELECT c.id FROM calculation_publication p JOIN calculation c "
        "ON c.id=p.baseline_calculation_id WHERE p.posting_period<=? "
        "AND (? IS NULL OR p.posting_period>?) AND c.kind='asset_consumption_month'",
        (through, after, after) * 2,
    )}


@pytest.mark.parametrize("has_close", [False, True])
def test_owner_locator_preserves_current_replacement_withdrawal_and_baselines(has_close):
    connection, closed, through = book()
    try:
        after = closed if has_close else None
        expected = {"closed-owner", "baseline-only", "replaced-owner", "current-owner"}
        assert original(connection, through, after) == expected
        statements = []
        connection.set_trace_callback(statements.append)
        assert _asset_open_batch_owners(connection, through, after) == expected
        connection.set_trace_callback(None)
        assert len(statements) == 1
        assert "IS NULL OR" not in statements[0]
        assert ("p.posting_period>" in statements[0]) is has_close
        assert "baseline_calculation_id" in statements[0]
        assert _asset_open_batch_owners(connection, closed, closed) == set()
    finally:
        connection.close()


def test_known_tail_work_does_not_follow_unrelated_closed_publication_growth():
    connection, closed, through = book()
    try:
        def measure(operation):
            steps = 0

            def progress():
                nonlocal steps
                steps += 100
                return 0

            connection.set_progress_handler(progress, 100)
            try:
                result = operation()
            finally:
                connection.set_progress_handler(None, 0)
            return result, steps

        expected, before = measure(lambda: _asset_open_batch_owners(connection, through, closed))
        connection.executemany(
            "INSERT INTO calculation VALUES(?,?,?)",
            ((f"unrelated-{index}", "bank_statement", closed) for index in range(4000)),
        )
        connection.executemany(
            "INSERT INTO calculation_publication VALUES(?,?,?,?)",
            ((f"historical-{index}", closed, f"unrelated-{index}", None)
             for index in range(4000)),
        )
        actual, after = measure(lambda: _asset_open_batch_owners(connection, through, closed))
        old_actual, old_steps = measure(lambda: original(connection, through, closed))
        assert actual == old_actual == expected
        assert after <= before + 200
        assert old_steps > after + 20000
    finally:
        connection.close()
