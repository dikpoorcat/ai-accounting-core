"""Exceptional missing-typed discovery ignores unrelated source references."""

import sqlite3

import pytest
from test_integrity_content import damage
from test_reports import book as _book
from test_reports import close_quarter, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _position
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.runtime import _PrivateConnection

book = _book


def test_position_authenticates_frozen_party_child_and_detects_later_conflict(book):
    engine, save, _, _ = book
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        voucher, old_id = connection.execute(
            "SELECT c.voucher_version_id,c.revision_id FROM fact_report_classification c"
        ).fetchone()
    save(
        "report_classification",
        "class-" + voucher,
        {
            "period": "2026-02",
            "voucher_version_id": voucher,
            "profit_details": [
                {"line_no": 1, "detail_code": "management_entertainment", "amount_fen": 10000}
            ],
            "counterparties": [{"line_no": 2, "counterparty_id": "supplier"}],
        },
        revision=1,
    )
    close_quarter(book)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        baseline = _position(snap)
    assert not any(issue["field"] == "report_classification" for issue in baseline["issues"])
    save(
        "report_classification",
        "later-conflict",
        {
            "period": "2026-04",
            "voucher_version_id": voucher,
            "profit_details": [
                {"line_no": 1, "detail_code": "management_entertainment", "amount_fen": 10000}
            ],
        },
    )
    with Dashboard(engine)._snapshot("2026-04") as snap:
        conflict = _position(snap)
    assert any(
        issue["field"] == "report_classification"
        and issue["voucher_version_id"] == voucher
        for issue in conflict["issues"]
    )
    assert old_id != voucher
    damage(
        engine,
        "fact_report_classification_counterparties",
        "UPDATE fact_report_classification_counterparties SET counterparty_id='owner' "
        "WHERE revision_id=(SELECT c.fact_id FROM fact_current c "
        "WHERE c.subject_id=?)",
        ("class-" + voucher,),
    )
    with Dashboard(engine)._snapshot("2026-03") as snap:
        with pytest.raises(KernelError) as caught:
            _position(snap)
    assert caught.value.code == "content_integrity_failed"


def test_position_rejects_missing_frozen_typed_row_and_damaged_directory(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        baseline = _position(snap)
    damage(
        engine,
        "report_classification_directory",
        "UPDATE report_classification_directory SET content='{}'",
    )
    with Dashboard(engine)._snapshot("2026-03") as snap:
        with pytest.raises(KernelError) as damaged:
            _position(snap)
    assert damaged.value.code == "content_integrity_failed"
    assert Maintenance(engine).rebuild_projections(
        request_id="repair-position-classification-directory"
    )["changed"]
    with Dashboard(engine)._snapshot("2026-03") as snap:
        assert _position(snap) == baseline
    damage(
        engine,
        "fact_report_classification",
        "DELETE FROM fact_report_classification",
        foreign_keys=False,
    )
    with Dashboard(engine)._snapshot("2026-03") as snap:
        with pytest.raises(KernelError) as missing:
            _position(snap)
    assert missing.value.code == "content_integrity_failed"


def test_position_open_classifications_seek_exact_unclosed_fact_periods(book, monkeypatch):
    engine = book[0]
    scenario(book)
    queries = []
    original = _PrivateConnection.execute

    def record(connection, sql, parameters=()):
        if "WITH RECURSIVE periods(period)" in sql and "JOIN fact_current h" in sql:
            queries.append(sql)
        return original(connection, sql, parameters)

    monkeypatch.setattr(_PrivateConnection, "execute", record)
    with Dashboard(engine)._snapshot("2026-02") as snap:
        _position(snap)
    assert len(queries) == 1
    query = queries[0]

    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
            "CREATE INDEX subject_kind ON subject(kind,id);"
            "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT,revision INTEGER,"
            "period INTEGER,UNIQUE(subject_id,revision));"
            "CREATE INDEX fact_period ON fact_revision(period,subject_id,id);"
            "CREATE TABLE fact_current(subject_id TEXT PRIMARY KEY,fact_id TEXT UNIQUE);"
            "CREATE TABLE fact_report_classification(revision_id TEXT PRIMARY KEY,"
            "voucher_version_id TEXT);"
            "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
        )
        connection.executemany("INSERT INTO period_close VALUES(?)", [(1,), (3,), (4,)])
        for ident, month, key in (
            ("late-old-open", 2, "old-voucher"),
            ("current-open", 5, "current-voucher"),
            ("open-missing-typed", 5, None),
        ):
            connection.execute("INSERT INTO subject VALUES(?,'report_classification')", (ident,))
            connection.execute(
                "INSERT INTO fact_revision VALUES(?,?,1,?)", (ident, ident, month)
            )
            connection.execute("INSERT INTO fact_current VALUES(?,?)", (ident, ident))
            if key is not None:
                connection.execute(
                    "INSERT INTO fact_report_classification VALUES(?,?)", (ident, key)
                )

        def measured():
            steps = 0

            def tick():
                nonlocal steps
                steps += 1
                return 0

            connection.set_progress_handler(tick, 1)
            try:
                rows = set(connection.execute(query, (5, 5)))
            finally:
                connection.set_progress_handler(None, 0)
            return rows, steps

        before, old_steps = measured()
        assert before == {
            ("late-old-open", "old-voucher"),
            ("current-open", "current-voucher"),
            ("open-missing-typed", None),
        }
        connection.executemany(
            "INSERT INTO subject VALUES(?,'report_classification')",
            [(f"historical-{number}",) for number in range(2000)],
        )
        connection.executemany(
            "INSERT INTO fact_revision VALUES(?,?,1,1)",
            [(f"historical-{number}", f"historical-{number}") for number in range(2000)],
        )
        connection.executemany(
            "INSERT INTO fact_current VALUES(?,?)",
            [(f"historical-{number}", f"historical-{number}") for number in range(2000)],
        )
        connection.executemany(
            "INSERT INTO fact_report_classification VALUES(?,?)",
            [(f"historical-{number}", f"voucher-{number}") for number in range(2000)],
        )
        after, new_steps = measured()
        assert after == before
        assert new_steps < old_steps + 1000, (old_steps, new_steps)
        plan = connection.execute("EXPLAIN QUERY PLAN " + query, (5, 5)).fetchall()
        assert any("fact_period" in row[3] for row in plan)


def test_position_party_child_drives_key_lookup_with_unrelated_history(book, monkeypatch):
    engine, save, _, _ = book
    scenario(book)
    with engine.store.connection(read_only=True) as connection:
        voucher = connection.execute(
            "SELECT voucher_version_id FROM fact_report_classification"
        ).fetchone()[0]
    save(
        "report_classification",
        "class-" + voucher,
        {
            "period": "2026-02",
            "voucher_version_id": voucher,
            "profit_details": [
                {"line_no": 1, "detail_code": "management_entertainment", "amount_fen": 10000}
            ],
            "counterparties": [{"line_no": 2, "counterparty_id": "supplier"}],
        },
        revision=1,
    )
    queries = []
    original = _PrivateConnection.execute

    def record(connection, sql, parameters=()):
        if "SELECT DISTINCT c.voucher_version_id" in sql and "counterparties child" in sql:
            queries.append(sql)
        return original(connection, sql, parameters)

    monkeypatch.setattr(_PrivateConnection, "execute", record)
    with Dashboard(engine)._snapshot("2026-02") as snap:
        assert _position(snap)["assets_fen"] == 50000
    assert len(queries) == 1

    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE fact_report_classification(revision_id TEXT PRIMARY KEY,"
            "voucher_version_id TEXT);"
            "CREATE INDEX report_classification_voucher_revision "
            "ON fact_report_classification(voucher_version_id,revision_id);"
            "CREATE TABLE fact_report_classification_counterparties("
            "revision_id TEXT,line_no INTEGER,PRIMARY KEY(revision_id,line_no));"
        )
        connection.execute(
            "INSERT INTO fact_report_classification VALUES('selected','voucher-selected')"
        )
        connection.execute(
            "INSERT INTO fact_report_classification_counterparties VALUES('selected',2)"
        )

        def measured():
            steps = 0

            def tick():
                nonlocal steps
                steps += 1
                return 0

            connection.set_progress_handler(tick, 1)
            try:
                rows = list(connection.execute(queries[0]))
            finally:
                connection.set_progress_handler(None, 0)
            return rows, steps

        before, old_steps = measured()
        assert before == [("voucher-selected",)]
        connection.executemany(
            "INSERT INTO fact_report_classification VALUES(?,?)",
            [(f"unrelated-{number}", f"voucher-{number}") for number in range(2000)],
        )
        after, new_steps = measured()
        assert after == before
        assert new_steps < old_steps + 1000, (old_steps, new_steps)
        plan = connection.execute("EXPLAIN QUERY PLAN " + queries[0]).fetchall()
        assert any("SCAN child" in row[3] for row in plan)


def test_position_missing_typed_probe_preserves_scope_and_ignores_unrelated_growth(
    book, monkeypatch
):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    queries = []
    original = _PrivateConnection.execute

    def observe(connection, sql, parameters=()):
        if (
            "readiness.financial_reports.facts[*]" in sql
            and "s.kind='report_classification'" in sql
            and len(parameters) == 1
        ):
            queries.append(sql)
        return original(connection, sql, parameters)

    monkeypatch.setattr(_PrivateConnection, "execute", observe)
    with Dashboard(engine)._snapshot("2026-03") as snap:
        result = _position(snap)
    assert result["assets_fen"] == 40000
    assert result["liabilities_fen"] == 0
    assert len(queries) == 1

    # Exercise the actual production query with the same key indexes, including
    # repeated closes, old revisions, a later-only reference and a wrong path.
    with sqlite3.connect(":memory:") as connection:
        connection.executescript(
            "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
            "CREATE INDEX subject_kind ON subject(kind,id);"
            "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT,revision INTEGER,"
            "UNIQUE(subject_id,revision));"
            "CREATE TABLE fact_report_classification(revision_id TEXT PRIMARY KEY);"
            "CREATE TABLE close_reference(reference_type TEXT,reference_id TEXT,"
            "close_period INTEGER,path TEXT);"
            "CREATE INDEX close_reference_lookup ON close_reference"
            "(reference_type,reference_id,close_period);"
        )
        connection.execute("INSERT INTO subject VALUES('classification','report_classification')")
        connection.executemany(
            "INSERT INTO fact_revision VALUES(?,'classification',?)",
            [(f"version-{number}", number) for number in range(1, 5)],
        )
        path = "readiness.financial_reports.facts[*]"
        connection.executemany(
            "INSERT INTO close_reference VALUES('fact',?,?,?)",
            [
                ("version-1", 1, path),
                ("version-1", 2, path),
                ("version-2", 2, path),
                ("version-3", 4, path),
                ("version-4", 2, "management_snapshot.typed_facts[*].id"),
            ],
        )

        def measured():
            ticks = [0]

            def progress():
                ticks[0] += 1
                return 0

            connection.set_progress_handler(progress, 1)
            try:
                assert {row[0] for row in connection.execute(queries[0], (2,))} == {
                    "version-1",
                    "version-2",
                }
            finally:
                connection.set_progress_handler(None, 0)
            return ticks[0]

        before = measured()
        connection.executemany(
            "INSERT INTO subject VALUES(?,'expense')", [(f"unrelated-{n}",) for n in range(2000)]
        )
        connection.executemany(
            "INSERT INTO fact_revision VALUES(?,?,1)",
            [(f"unrelated-fact-{n}", f"unrelated-{n}") for n in range(2000)],
        )
        connection.executemany(
            "INSERT INTO close_reference VALUES('fact',?,?,?)",
            [(f"unrelated-fact-{n}", month, path) for n in range(2000) for month in range(1, 13)],
        )
        after = measured()
        assert after <= before + 50, (before, after)
