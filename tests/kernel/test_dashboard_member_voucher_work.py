"""Exact member locators keep SQL work bounded as unrelated originals grow.

The active contract supplies tables/indexes. Proof callbacks are deliberately
isolated here; native reviewed/frozen voucher tests cover complete authority.
"""

import sqlite3
from types import SimpleNamespace

import pytest

from ai_accounting.kernel.dashboard_brief_groups import exact_open_voucher_rows
from ai_accounting.kernel.dashboard_open_groups import _member_vouchers
from ai_accounting.kernel.dashboard_reads import Journal
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.types import YearMonth

TABLES = {
    "calculation",
    "calculation_current",
    "calculation_publication",
    "voucher",
    "voucher_version",
    "voucher_current",
    "period_close",
    "close_reference",
}
INDEXES = {
    "calculation_subject",
    "voucher_calculation",
    "voucher_period",
    "voucher_version_reverses",
    "publication_subject",
    "publication_posting",
    "close_reference_lookup",
}
PERIOD = YearMonth("2026-09")


class CapturingConnection(sqlite3.Connection):
    def execute(self, query, parameters=()):
        self.queries.append((query, parameters))
        return super().execute(query, parameters)


def _database():
    objects = production_bundle().current("company")["objects"]
    selected = [
        item
        for item in objects
        if (item["type"] == "table" and item["name"] in TABLES)
        or (item["type"] == "index" and item["name"] in INDEXES)
    ]
    assert {(item["type"], item["name"]) for item in selected} == {
        *(("table", name) for name in TABLES),
        *(("index", name) for name in INDEXES),
    }
    connection = sqlite3.connect(":memory:", factory=CapturingConnection)
    connection.row_factory = sqlite3.Row
    connection.queries = []
    for item in sorted(selected, key=lambda value: value["type"] == "index"):
        connection.execute(item["sql"])
    return connection


def _originals(connection, prefix, start, count):
    records = [(f"{prefix}-{index}", index + 1) for index in range(start, start + count)]
    connection.executemany(
        "INSERT INTO calculation(id,subject_id,fact_id,kind,period,outcome,digest,program_version) "
        "VALUES(?,?,?,'expense',?,'{}',?,'work-fixture')",
        [(ident, ident, ident, PERIOD.ordinal, b"d" * 32) for ident, _ in records],
    )
    connection.executemany(
        "INSERT INTO calculation_current(subject_id,calculation_id) VALUES(?,?)",
        [(ident, ident) for ident, _ in records],
    )
    connection.executemany(
        "INSERT INTO voucher(id,number) VALUES(?,?)",
        records,
    )
    connection.executemany(
        "INSERT INTO voucher_version(id,voucher_id,calculation_id,period,total) VALUES(?,?,?,?,?)",
        [(ident, ident, ident, PERIOD.ordinal, 100 + index) for ident, index in records],
    )
    connection.executemany(
        "INSERT INTO voucher_current(voucher_id,version_id) VALUES(?,?)",
        [(ident, ident) for ident, _ in records],
    )
    connection.executemany(
        "INSERT INTO calculation_publication"
        "(id,sequence,subject_id,calculation_id,mode,posting_period,voucher_id) "
        "VALUES(?,?,?,?,'initial',?,?)",
        [(ident, index, ident, ident, PERIOD.ordinal, ident) for ident, index in records],
    )


def _snapshot(connection):
    def publications(rows):
        assert {row["calculation_id"] for row in rows} == {f"target-{index}" for index in range(20)}

    def adoptions(rows, *, through_period):
        assert through_period == PERIOD.ordinal
        assert {row["id"] for row in rows} == {f"target-{index}" for index in range(20)}
        assert all(row["close_period"] is None for row in rows)

    def scope(through_period, *, posting_period):
        assert through_period == posting_period == PERIOD.ordinal

    class QueryWorkJournal(Journal):
        def hydrate(self, rows, *, include_lines=True):
            adoptions(rows, through_period=self.snapshot.month)
            return [dict(row) for row in rows]

    snap = SimpleNamespace(
        connection=connection,
        period=PERIOD,
        month=PERIOD.ordinal,
        reads=SimpleNamespace(
            verify_publication_records=publications,
            verify_selected_voucher_adoptions=adoptions,
            verify_open_voucher_scope=scope,
            _snapshot_active=False,
        ),
    )
    snap.month_journal = QueryWorkJournal(snap)
    return snap


def _measure(connection, read):
    calls = 0

    def progress():
        nonlocal calls
        calls += 1
        # A regressed history join fails promptly, without a timing assertion.
        return int(calls > 1000)

    connection.set_progress_handler(progress, 100)
    try:
        result = read()
    finally:
        connection.set_progress_handler(None, 0)
    return result, calls * 100


@pytest.mark.parametrize("reader", ["members", "voucher_rows"])
def test_twenty_exact_members_ignore_growing_unrelated_originals(record_property, reader):
    connection = _database()
    try:
        _originals(connection, "target", 0, 20)
        # Legitimate old physical versions retain their stable voucher identity.
        # They must enter candidate location, while selection keeps the current
        # adopted version and duplicate checking stays with that exact owner.
        connection.executemany(
            "INSERT INTO voucher_version(id,voucher_id,calculation_id,period,total) "
            "VALUES(?,?,?,?,100)",
            [
                (f"old-{index}", f"target-{index}", f"old-owner-{index}", PERIOD.ordinal)
                for index in range(2)
            ],
        )
        headers = {f"target-{index}": {"subject_id": f"target-{index}"} for index in range(20)}

        def read():
            snap = _snapshot(connection)
            if reader == "members":
                return {ident: dict(row) for ident, row in _member_vouchers(snap, headers).items()}
            rows = exact_open_voucher_rows(
                snap,
                [{"voucher_version_id": ident} for ident in headers],
            )
            assert [row["id"] for row in rows] == list(headers)
            return {row["id"]: row for row in rows}

        expected, before = _measure(connection, read)
        assert expected.keys() == headers.keys()
        assert all(
            row["id"] == ident and row["total"] == 101 + int(ident.split("-")[1])
            for ident, row in expected.items()
        )
        if reader == "members":
            locator = next(
                query
                for query in connection.queries
                if query[0].startswith("SELECT v.id FROM json_each")
            )
            assert len(connection.execute(*locator).fetchall()) == 22
        _originals(connection, "unrelated", 20, 4000)
        actual, after = _measure(connection, read)
        assert actual == expected
        assert after <= before + 1000
        record_property("twenty_member_baseline_vm_steps", before)
        record_property("twenty_member_with_4000_unrelated_vm_steps", after)
        # Inspect both public locator stages; IDs drive existing exact indexes.
        for query, parameters in connection.queries.copy():
            if query.startswith("SELECT v.id"):
                plan = [
                    row[3]
                    for row in connection.execute(
                        "EXPLAIN QUERY PLAN " + query,
                        parameters,
                    )
                ]
                assert plan[0].startswith("SCAN ids")
                assert not any("USING INDEX voucher_version_reverses" in step for step in plan)
            elif query.startswith("WITH scoped_vouchers"):
                plan = [
                    row[3]
                    for row in connection.execute(
                        "EXPLAIN QUERY PLAN " + query,
                        parameters,
                    )
                ]
                assert plan[0].startswith("SCAN json_each")
                assert any(
                    "SEARCH v USING INDEX sqlite_autoindex_voucher_version_1 (id=?)" in step
                    for step in plan
                )
    finally:
        connection.close()
