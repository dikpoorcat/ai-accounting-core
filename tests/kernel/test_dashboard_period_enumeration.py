"""Period discovery must enumerate periods, not decode every current business."""

import sqlite3
from types import SimpleNamespace

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.types import YearMonth


def database():
    # Only the schema used by this read is needed to measure its SQLite work.
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,period INTEGER,subject_id TEXT);"
        "CREATE INDEX fact_period ON fact_revision(period,subject_id,id);"
        "CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY,fact_id TEXT UNIQUE);"
        "CREATE TABLE voucher_version(id TEXT PRIMARY KEY,period INTEGER,voucher_id TEXT);"
        "CREATE INDEX voucher_period ON voucher_version(period,voucher_id,id);"
        "CREATE TABLE voucher_current(voucher_id TEXT PRIMARY KEY,version_id TEXT UNIQUE);"
        "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
        "CREATE TABLE material_revision(period INTEGER);"
    )
    return connection


def test_current_facts_vouchers_materials_and_closes_keep_their_own_months():
    with database() as connection:

        def month(value):
            return YearMonth(value).ordinal

        connection.executemany(
            "INSERT INTO fact_revision VALUES(?,?,?)",
            [
                ("old", month("2026-01"), "moved"),
                ("new", month("2026-03"), "moved"),
                ("deleted", month("2026-02"), "deleted"),
                ("timeless", 0, "profile"),
            ],
        )
        connection.executemany(
            "INSERT INTO fact_current VALUES(?,?)", [("moved", "new"), ("profile", "timeless")]
        )
        connection.executemany(
            "INSERT INTO voucher_version VALUES(?,?,?)",
            [("old-v", month("2026-01"), "v"), ("new-v", month("2026-04"), "v")],
        )
        connection.execute("INSERT INTO voucher_current VALUES('v','new-v')")
        connection.execute("INSERT INTO material_revision VALUES(?)", (month("2026-05"),))
        connection.execute("INSERT INTO period_close VALUES(?)", (month("2026-06"),))
        rows = Dashboard(SimpleNamespace(store=None))._periods(connection)
    assert [row["key"] for row in rows] == ["2026-06", "2026-05", "2026-04", "2026-03"]
    assert [row["status"] for row in rows] == ["closed", "open", "open", "open"]


def test_many_current_facts_are_found_by_period_index_without_visiting_each_head():
    with database() as connection:
        rows = [
            (f"{month}-{item:04}", YearMonth(f"2026-{month:02}").ordinal, f"{month}-{item:04}")
            for month in range(1, 13)
            for item in range(1000)
        ]
        connection.executemany("INSERT INTO fact_revision VALUES(?,?,?)", rows)
        connection.executemany(
            "INSERT INTO fact_current VALUES(?,?)", [(subject, ident) for ident, _, subject in rows]
        )
        steps = []
        connection.set_progress_handler(lambda: steps.append(100) or 0, 100)
        actual = Dashboard(SimpleNamespace(store=None))._periods(connection)
        connection.set_progress_handler(None, 0)
    assert [row["key"] for row in actual] == [f"2026-{month:02}" for month in range(12, 0, -1)]
    assert sum(steps) < 5000


def test_empty_database_has_no_invented_month():
    with database() as connection:
        assert Dashboard(SimpleNamespace(store=None))._periods(connection) == []
