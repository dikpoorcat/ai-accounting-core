"""The open report-flow selector keeps the full historical fact set."""

import sqlite3

import pytest

from ai_accounting.kernel import report_flow, report_flow_v1
from ai_accounting.kernel.types import canonical


def _original_refs(connection, period, vouchers):
    selected = (
        "SELECT r.reference_id id FROM close_reference r "
        "WHERE r.reference_type='fact' AND r.close_period<=? "
        "AND r.path='readiness.financial_reports.facts[*]' "
        "UNION SELECT f.id FROM fact_current c JOIN fact_revision f ON f.id=c.fact_id "
        "WHERE f.period<=? AND NOT EXISTS("
        "SELECT 1 FROM period_close p WHERE p.period=f.period)"
    )
    return tuple(
        (row[0], row[1].hex())
        for row in connection.execute(
            "SELECT DISTINCT f.id,f.digest FROM (" + selected + ") ids "
            "JOIN fact_revision f ON f.id=ids.id JOIN subject s ON s.id=f.subject_id "
            "JOIN fact_report_classification c ON c.revision_id=f.id "
            "WHERE s.kind='report_classification' AND "
            "(c.voucher_version_id IN (SELECT value FROM json_each(?)) OR f.period=?) "
            "ORDER BY f.id",
            (period, period, canonical(sorted(vouchers)), period),
        )
    )


@pytest.mark.parametrize("module", [report_flow, report_flow_v1])
def test_open_selector_preserves_closed_current_future_and_damaged_candidates(module):
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
        "CREATE TABLE fact_revision("
        "id TEXT PRIMARY KEY,subject_id TEXT,period INTEGER,digest BLOB);"
        "CREATE INDEX fact_period ON fact_revision(period);"
        "CREATE TABLE fact_current(fact_id TEXT);"
        "CREATE TABLE period_close(period INTEGER PRIMARY KEY);"
        "CREATE TABLE close_reference("
        "reference_id TEXT,reference_type TEXT,path TEXT,close_period INTEGER);"
        "CREATE INDEX close_reference_lookup ON "
        "close_reference(reference_type,reference_id,close_period,path);"
        "CREATE TABLE fact_report_classification(revision_id TEXT,voucher_version_id TEXT);"
        "CREATE INDEX report_classification_voucher_revision ON "
        "fact_report_classification(voucher_version_id,revision_id);"
    )
    specs = (
        ("closed_selected", 1, "v-selected", "report_classification"),
        ("closed_same_period", 1, "v-other", "report_classification"),
        ("open_selected", 2, "v-selected", "report_classification"),
        ("open_same_period", 2, "v-other", "report_classification"),
        ("future_open", 3, "v-selected", "report_classification"),
        ("future_closed", 3, "v-selected", "report_classification"),
        ("wrong_path", 1, "v-selected", "report_classification"),
        ("future_reference", 1, "v-selected", "report_classification"),
        ("closed_missing_typed", 1, None, "report_classification"),
        ("closed_wrong_kind", 1, "v-selected", "report_profile"),
        ("closed_no_reference", 1, "v-selected", "report_classification"),
    )
    path = "readiness.financial_reports.facts[*]"
    try:
        connection.executemany(
            "INSERT INTO subject VALUES(?,?)", [(name, kind) for name, _, _, kind in specs]
        )
        connection.executemany(
            "INSERT INTO fact_revision VALUES(?,?,?,?)",
            [(name, name, month, name.encode().ljust(32, b"x")) for name, month, _, _ in specs],
        )
        connection.executemany(
            "INSERT INTO fact_report_classification VALUES(?,?)",
            [(name, voucher) for name, _, voucher, _ in specs if voucher is not None],
        )
        connection.executemany("INSERT INTO period_close VALUES(?)", [(1,), (3,)])
        connection.executemany(
            "INSERT INTO fact_current VALUES(?)",
            [(name,) for name in (
                "open_selected", "open_same_period", "future_open", "closed_no_reference",
                "missing_fact_header",
            )],
        )
        connection.executemany(
            "INSERT INTO close_reference VALUES(?,'fact',?,?)",
            [(name, path, month) for name, month in (
                ("closed_selected", 1), ("closed_selected", 1),
                ("closed_same_period", 1), ("future_closed", 3),
                ("future_reference", 3), ("closed_missing_typed", 1),
                ("closed_wrong_kind", 1),
            )] + [("wrong_path", "other.path", 1), ("wrong_path", "other.path", 1)],
        )
        for period in (1, 2, 3):
            expected = _original_refs(connection, period, {"v-selected"})
            assert module._classification_refs(
                connection, period, {"v-selected"}, closed=False
            ) == expected
        assert {name for name, _ in _original_refs(connection, 2, {"v-selected"})} == {
            "closed_selected", "open_selected", "open_same_period"
        }
    finally:
        connection.close()
