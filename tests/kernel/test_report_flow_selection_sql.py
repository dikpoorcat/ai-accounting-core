"""The closed report selector must stay bounded by its exact candidate ranges."""

import sqlite3

from ai_accounting.kernel.report_flow import _classification_refs as current_refs
from ai_accounting.kernel.report_flow_v1 import _classification_refs as v1_refs
from ai_accounting.kernel.schema import schema_sql
from ai_accounting.kernel.service import default_registry


def _database():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT NOT NULL);
        CREATE INDEX subject_kind ON subject(kind,id);
        CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,
            period INTEGER NOT NULL,digest BLOB NOT NULL);
        CREATE INDEX fact_period ON fact_revision(period,subject_id,id);
        CREATE TABLE fact_report_classification(
            revision_id TEXT PRIMARY KEY,period INTEGER NOT NULL,
            voucher_version_id TEXT NOT NULL);
        CREATE INDEX report_classification_voucher_revision
            ON fact_report_classification(voucher_version_id,revision_id);
        CREATE TABLE close_reference(reference_type TEXT NOT NULL,reference_id TEXT NOT NULL,
            close_period INTEGER NOT NULL,path TEXT NOT NULL);
        CREATE INDEX close_reference_lookup
            ON close_reference(reference_type,reference_id,close_period);
        """
    )
    return connection


def _add(connection, ident, period, voucher, *, kind="report_classification", refs=()):
    connection.execute("INSERT INTO subject VALUES(?,?)", (ident, kind))
    connection.execute(
        "INSERT INTO fact_revision VALUES(?,?,?,?)",
        (ident, ident, period, ident.encode().ljust(32, b"\0")[:32]),
    )
    connection.execute(
        "INSERT INTO fact_report_classification VALUES(?,?,?)", (ident, period, voucher)
    )
    connection.executemany(
        "INSERT INTO close_reference VALUES('fact',?,?,?)",
        ((ident, close_period, path) for close_period, path in refs),
    )


def _old_selector(connection, period, vouchers):
    # Independent oracle for the former exact selection expression.
    import json

    rows = connection.execute(
        "WITH candidate AS MATERIALIZED ("
        "SELECT f.id,f.digest FROM fact_report_classification c "
        "JOIN fact_revision f ON f.id=c.revision_id "
        "JOIN subject s ON s.id=f.subject_id "
        "WHERE s.kind='report_classification' AND "
        "(c.voucher_version_id IN (SELECT value FROM json_each(?)) OR f.period=?)"
        ") SELECT DISTINCT candidate.id,candidate.digest FROM candidate "
        "CROSS JOIN close_reference r INDEXED BY close_reference_lookup "
        "ON r.reference_type='fact' AND r.reference_id=candidate.id "
        "WHERE r.close_period<=? AND r.path='readiness.financial_reports.facts[*]' "
        "ORDER BY candidate.id",
        (json.dumps(sorted(vouchers)), period, period),
    )
    return tuple((ident, digest.hex()) for ident, digest in rows)


def _vm_steps(connection, query):
    calls = 0

    def progress():
        nonlocal calls
        calls += 1
        return 0

    connection.set_progress_handler(progress, 100)
    try:
        query()
    finally:
        connection.set_progress_handler(None, 0)
    return calls * 100


def test_closed_classification_ranges_preserve_exact_reference_selection():
    connection = _database()
    path = "readiness.financial_reports.facts[*]"
    selected = {"voucher-selected"}
    for number in range(3000):
        # Historic classifications for other vouchers and months are not a
        # reason to scan all typed rows when selecting one closed month.
        _add(
            connection,
            f"noise-{number:04d}",
            24000 + number % 12,
            f"other-{number:04d}",
            refs=((24000 + number % 12, path),),
        )
    _add(connection, "old-first", 24199, "voucher-selected", refs=((24200, path),))
    _add(connection, "old-revision", 24200, "voucher-selected", refs=((24201, path),))
    _add(connection, "orphan-this-month", 24203, "not-selected", refs=((24203, path),))
    _add(
        connection,
        "selected-this-month",
        24203,
        "voucher-selected",
        refs=((24203, path), (24203, path)),
    )
    _add(connection, "wrong-path", 24203, "voucher-selected", refs=((24203, "other"),))
    _add(connection, "future-close", 24203, "voucher-selected", refs=((24204, path),))
    _add(connection, "missing-reference", 24203, "voucher-selected")
    _add(
        connection, "different-kind", 24203, "voucher-selected", kind="other", refs=((24203, path),)
    )

    expected = _old_selector(connection, 24203, selected)
    assert {ident for ident, _ in expected} == {
        "old-first",
        "old-revision",
        "orphan-this-month",
        "selected-this-month",
    }
    assert current_refs(connection, 24203, selected, closed=True) == expected
    assert v1_refs(connection, 24203, selected, closed=True) == expected
    baseline = _vm_steps(connection, lambda: _old_selector(connection, 24203, selected))
    current = _vm_steps(connection, lambda: current_refs(connection, 24203, selected, closed=True))
    assert current < baseline * 0.7
    connection.close()


def test_generated_company_ddl_has_classification_candidate_index():
    assert (
        "CREATE INDEX report_classification_voucher_revision "
        "ON fact_report_classification(voucher_version_id,revision_id)"
        in schema_sql(default_registry())
    )
