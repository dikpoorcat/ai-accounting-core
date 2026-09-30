"""Open report selection seeks months without scanning closed revisions."""

import sqlite3

from ai_accounting.kernel.reports import _report_references
from ai_accounting.kernel.types import YearMonth


def _connection():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT NOT NULL);"
        "CREATE INDEX subject_kind ON subject(kind,id);"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,"
        "revision INTEGER NOT NULL,period INTEGER NOT NULL,UNIQUE(subject_id,revision));"
        "CREATE INDEX fact_period ON fact_revision(period,subject_id,id);"
        "CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY,fact_id TEXT UNIQUE NOT NULL);"
        "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
    )
    return connection


def _add(connection, subject, kind, month, revision=1, *, current=True):
    ident = f"{subject}@{revision}"
    connection.execute("INSERT OR IGNORE INTO subject VALUES(?,?)", (subject, kind))
    connection.execute(
        "INSERT INTO fact_revision VALUES(?,?,?,?)", (ident, subject, revision, month)
    )
    if current:
        connection.execute(
            "INSERT OR REPLACE INTO fact_current VALUES(?,?)", (subject, ident)
        )
    return ident


def _read(connection, *, kinds=None, closed_rows=()):
    steps = 0

    def progress():
        nonlocal steps
        steps += 100
        return 0

    connection.set_progress_handler(progress, 100)
    try:
        values = _report_references(
            connection, YearMonth("2026-12"), "open", closed_rows=closed_rows,
            open_kinds=kinds,
        )
    finally:
        connection.set_progress_handler(None, 0)
    return values, steps


def test_open_reference_scope_preserves_gaps_heads_and_rare_sources():
    connection = _connection()
    try:
        assert _read(connection)[0] == set()
        january = YearMonth("2026-01").ordinal
        december = YearMonth("2026-12").ordinal
        old = _add(connection, "amended", "report_classification", january)
        active = _add(connection, "amended", "report_classification", january, 2)
        profile = _add(connection, "profile", "report_profile", january)
        tax = _add(connection, "tax", "report_income_tax_confirmation", december)
        _add(connection, "closed", "report_classification", january + 1)
        _add(connection, "future", "report_classification", december + 1)
        _add(connection, "withdrawn", "report_classification", january, current=False)
        _add(connection, "unrelated", "payroll", january)
        connection.execute("INSERT INTO period_close VALUES(?)", (january + 1,))
        # January is covered by a later close but has no independent close.
        # Exact existing selection semantics still include its current facts.
        frozen = [{"reference_id": "frozen-source"}, {"reference_id": old}]
        assert _read(connection, closed_rows=frozen)[0] == {
            active, profile, tax, old, "frozen-source",
        }
        assert _read(connection, kinds={"report_classification"})[0] == {active}
        assert _read(connection, kinds={"report_profile"})[0] == {profile}
        assert _read(connection, kinds=set())[0] == set()
    finally:
        connection.close()


def test_open_reference_work_does_not_scan_closed_classification_revisions():
    connection = _connection()
    try:
        end = YearMonth("2026-12").ordinal
        selected = {
            _add(connection, f"selected-{i}", "report_classification", end)
            for i in range(100)
        }
        _add(connection, "profile", "report_profile", end)
        expected = selected | {"profile@1"}
        observations = []
        previous = 0
        for months in (12, 48, 120):
            for number in range(previous, months):
                month = end - number - 1
                connection.execute("INSERT INTO period_close VALUES(?)", (month,))
                for item in range(100):
                    subject = f"closed-{number}-{item}"
                    _add(connection, subject, "report_classification", month)
                    _add(connection, subject, "report_classification", month, 2)
                    _add(connection, f"unrelated-{number}-{item}", "payroll", month)
            previous = months
            actual, steps = _read(connection)
            assert actual == expected
            observations.append(steps)
        # 120 month seeks are necessary; 36,000 historical row scans are not.
        assert observations[-1] < observations[0] + 15_000, observations
        before = _read(connection)[1]
        for item in range(5000):
            _add(connection, f"more-unrelated-{item}", "bank_statement", end - 120)
        actual, after = _read(connection)
        assert actual == expected
        assert after < before + 1000, (before, after)
    finally:
        connection.close()
