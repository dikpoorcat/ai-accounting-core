"""Exact frozen classification checks return status without transferring every header."""

import sqlite3
from collections import Counter, defaultdict
from contextlib import closing

import pytest
from stage9_metrics import _Connection
from test_integrity_content import damage
from test_reports import book as _book
from test_reports import close_quarter, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_flow import (
    _flow_classification_headers_match,
    read_report_flow,
)
from ai_accounting.kernel.types import YearMonth, canonical

book = _book
FIRST = bytes.fromhex("11" * 32)
SECOND = bytes.fromhex("22" * 32)
REFS = (("a", FIRST.hex()), ("b", SECOND.hex()))


def _connection():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    # Relax the source digest only to compare the old NULL failure boundary;
    # production's strict fact_revision rejects it before an ordinary read.
    connection.executescript(
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,digest BLOB,period INTEGER,"
        "subject_id TEXT);"
        "CREATE TABLE fact_report_classification(revision_id TEXT PRIMARY KEY,"
        "voucher_version_id TEXT);"
    )
    connection.executemany(
        "INSERT INTO subject VALUES(?,'report_classification')", (("sa",), ("sb",))
    )
    connection.executemany(
        "INSERT INTO fact_revision VALUES(?,?,?,?)",
        (("a", FIRST, 10, "sa"), ("b", SECOND, 9, "sb")),
    )
    connection.executemany(
        "INSERT INTO fact_report_classification VALUES(?,?)", (("a", "va"), ("b", "vb"))
    )
    return connection


def _former_headers_match(connection, period, expected_refs, source_vouchers):
    """Independent former ordinary-reader predicates, including tuple ordering."""
    selected = list(connection.execute(
        "SELECT f.id,f.digest,f.period,s.kind,c.voucher_version_id "
        "FROM json_each(?) ids LEFT JOIN fact_revision f ON f.id=ids.value "
        "LEFT JOIN subject s ON s.id=f.subject_id "
        "LEFT JOIN fact_report_classification c ON c.revision_id=f.id ORDER BY ids.value",
        (canonical([ident for ident, _ in expected_refs]),),
    ))
    if len(selected) != len(expected_refs) or any(
        row["id"] is None or row["kind"] != "report_classification"
        or row["voucher_version_id"] is None for row in selected
    ):
        raise KernelError("content_integrity_failed", "冻结报表分类来源缺失")
    vouchers = set(source_vouchers)
    actual = tuple(
        (row["id"], row["digest"].hex()) for row in selected
        if row["period"] == period or row["voucher_version_id"] in vouchers
    )
    if expected_refs != actual:
        digests = dict(actual)
        if any(ident in digests and digests[ident] != value for ident, value in expected_refs):
            raise KernelError("content_integrity_failed", "冻结报表分类来源摘要不一致")
        return False
    return True


def _outcome(operation):
    try:
        return ("returned", operation())
    except KernelError as error:
        return (type(error), error.code, str(error), error.details)
    except AttributeError as error:
        return (type(error), str(error))


@pytest.mark.parametrize("refs,vouchers,changes", [
    (REFS, ["vb"], ()),
    ((), [], ()),
    (REFS[:1], [], ()),
    (REFS, [], ()),
    (REFS, ["vb", "vb"], ()),
    (REFS[::-1], ["vb"], ()),
    ((REFS[0], REFS[0], REFS[1]), ["vb"], ()),
    ((REFS[0], ("a", SECOND.hex())), [], ()),
    ((("missing", FIRST.hex()),), [], ()),
    (REFS, ["vb"], ("DELETE FROM fact_revision WHERE id='b'",)),
    (REFS, ["vb"], ("DELETE FROM subject WHERE id='sb'",)),
    (REFS, ["vb"], ("UPDATE subject SET kind='expense' WHERE id='sb'",)),
    (REFS, ["vb"], ("UPDATE subject SET kind=NULL WHERE id='sb'",)),
    (REFS, ["vb"], ("DELETE FROM fact_report_classification WHERE revision_id='b'",)),
    (REFS, ["vb"], ("UPDATE fact_report_classification SET voucher_version_id=NULL "
                   "WHERE revision_id='b'",)),
    (REFS, ["vb"], ("UPDATE fact_revision SET period=8 WHERE id='a'",)),
    (REFS, ["vb"], ("UPDATE fact_revision SET period=NULL WHERE id='a'",)),
    (REFS, [], ("UPDATE fact_revision SET period=NULL WHERE id='a'",)),
    (REFS, ["vb"], ("UPDATE fact_report_classification SET voucher_version_id='other' "
                   "WHERE revision_id='b'",)),
    (REFS, ["vb"], ("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='a'",)),
    # A changed ineligible header retains selection fallback; eligible damage
    # still rejects when a different header has changed membership.
    (REFS, [], ("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='b'",)),
    (REFS, [], ("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='a'",)),
    (REFS, [], ("UPDATE fact_revision SET digest=NULL WHERE id='b'",)),
    (REFS, ["vb"], ("UPDATE fact_revision SET digest=NULL WHERE id='b'",)),
    ((("a", None),), [], ()),
    (REFS, ["vb"], ("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='a'",
                   "DELETE FROM fact_report_classification WHERE revision_id='b'")),
    (REFS, ["vb"], ("UPDATE fact_revision SET digest=NULL WHERE id='a'",
                   "DELETE FROM subject WHERE id='sb'")),
])
def test_exact_classification_outcomes_preserve_former_predicates(refs, vouchers, changes):
    with closing(_connection()) as connection:
        for statement in changes:
            connection.execute(statement)
        expected = _outcome(lambda: _former_headers_match(connection, 10, refs, vouchers))
        actual = _outcome(lambda: _flow_classification_headers_match(
            connection, 10, refs, vouchers
        ))
        assert actual == expected


@pytest.mark.parametrize("identifiers", [
    ("",), ("0", "01", "1e2"), ("null", "true", "false"),
    ('a"b', "a\\b", "a\nb", "[]{}$.[]"),
    ("中文😀", "é", "e\u0301", "a\u001fb"),
    ("\0",), ("a", "a\0b", "a\0c"),
])
def test_escaped_and_unicode_named_ids_keep_full_row_identity(identifiers):
    with closing(_connection()) as connection:
        refs = tuple((ident, FIRST.hex()) for ident in sorted(identifiers))
        for ident, _ in refs:
            connection.execute(
                "INSERT OR REPLACE INTO fact_revision VALUES(?,?,10,'sa')", (ident, FIRST)
            )
            connection.execute(
                "INSERT OR REPLACE INTO fact_report_classification VALUES(?,'escaped')", (ident,)
            )
        expected = _outcome(lambda: _former_headers_match(connection, 10, refs, []))
        actual = _outcome(lambda: _flow_classification_headers_match(connection, 10, refs, []))
        assert actual == expected == ("returned", True)
        connection.execute("DELETE FROM fact_revision WHERE id=?", (refs[-1][0],))
        expected = _outcome(lambda: _former_headers_match(connection, 10, refs, []))
        actual = _outcome(lambda: _flow_classification_headers_match(connection, 10, refs, []))
        assert actual == expected
        assert actual[1:3] == ("content_integrity_failed", "冻结报表分类来源缺失")


@pytest.mark.parametrize("expected_digest", [
    None, False, True, 0, 1.5, [], {}, {"b": 2, "a": 1},
    FIRST.hex().upper(), FIRST.hex() + "\0", "not-a-digest",
])
def test_unusual_digest_payloads_keep_the_original_array_decision(expected_digest):
    with closing(_connection()) as connection:
        refs = (("a", expected_digest),)
        expected = _outcome(lambda: _former_headers_match(connection, 10, refs, []))
        actual = _outcome(lambda: _flow_classification_headers_match(connection, 10, refs, []))
        assert actual == expected


@pytest.mark.parametrize("period", [None, True, 10.0, "10"])
def test_unusual_period_types_keep_python_membership(period):
    with closing(_connection()) as connection:
        expected = _outcome(lambda: _former_headers_match(connection, period, REFS, ["vb"]))
        actual = _outcome(lambda: _flow_classification_headers_match(
            connection, period, REFS, ["vb"]
        ))
        assert actual == expected


@pytest.mark.parametrize("expected_digest,error", [
    (float("nan"), ValueError), (float("inf"), ValueError),
    ({1: "number", "a": "string"}, TypeError), (b"not-json", TypeError),
])
def test_unusual_payload_encoding_failure_is_not_replaced_by_a_digest_decision(
    expected_digest, error,
):
    refs = (("a", expected_digest),)
    # These exceptional supplied values formerly failed while encoding the
    # array probe. The object shortcut must retain that boundary too.
    with pytest.raises(error) as previous:
        canonical(refs)
    with closing(_connection()) as connection:
        with pytest.raises(error) as current:
            _flow_classification_headers_match(connection, 10, refs, [])
        assert str(current.value) == str(previous.value)


def _previous_array_status(connection, period, refs):
    """The former one-status probe, retained only as a work/decision baseline."""
    return connection.execute(
        "WITH requested AS MATERIALIZED ("
        "SELECT json_extract(value,'$[0]') ident,"
        "json_extract(value,'$[1]') expected_digest FROM json_each(?)) "
        "SELECT count(*) row_count,coalesce(max(f.period IS NOT ?),0) other_period,"
        "coalesce(max(CASE "
        "WHEN f.id IS NULL OR s.kind IS NOT 'report_classification' "
        "OR c.voucher_version_id IS NULL THEN 4 "
        "WHEN f.digest IS NULL THEN 3 "
        "WHEN lower(hex(f.digest)) IS NOT requested.expected_digest THEN 2 ELSE 0 END "
        "),0) decision FROM requested LEFT JOIN fact_revision f ON f.id=requested.ident "
        "LEFT JOIN subject s ON s.id=f.subject_id "
        "LEFT JOIN fact_report_classification c ON c.revision_id=f.id",
        (canonical(refs), period),
    ).fetchone()


def _measured(connection, operation):
    counts, statements, work = Counter(), Counter(), defaultdict(Counter)

    class ObservedConnection(_Connection):
        def execute(self, statement, parameters=()):
            counts["sql_parameter_bytes"] += sum(
                len(value.encode("utf-8")) if isinstance(value, str) else 8
                for value in parameters
            )
            return super().execute(statement, parameters)

    observed = ObservedConnection(connection, counts, statements, work, set())

    def progress():
        counts["sqlite_vm_steps"] += 1
        return 0

    connection.set_progress_handler(progress, 1)
    try:
        result = operation(observed)
    finally:
        connection.set_progress_handler(None, 0)
    return result, counts


def test_twelve_exact_months_reduce_request_vm_work_and_keep_4932_named_headers(
    record_testsuite_property,
):
    with closing(_connection()) as connection:
        by_month = {
            month: tuple((f"month-{month:02d}-class-{index:04d}", FIRST.hex())
                         for index in range(411))
            for month in range(12)
        }
        for month, refs in by_month.items():
            connection.executemany(
                "INSERT INTO fact_revision VALUES(?,?,?,'sa')",
                ((ident, FIRST, month) for ident, _ in refs),
            )
            connection.executemany(
                "INSERT INTO fact_report_classification VALUES(?,'selected')",
                ((ident,) for ident, _ in refs),
            )
        previous, current = Counter(), Counter()
        for month, refs in by_month.items():
            status, before = _measured(connection, lambda observed: _previous_array_status(
                observed, month, refs
            ))
            actual, after = _measured(connection, lambda observed:
                _flow_classification_headers_match(observed, month, refs, [])
            )
            assert tuple(status) == (len(refs), 0, 0) and actual is True
            previous.update(before)
            current.update(after)
        assert sum(map(len, by_month.values())) == 4932
        assert previous["returned_rows"] == current["returned_rows"] == 12
        assert previous["sql_calls"] == current["sql_calls"] == 12
        assert current["sqlite_vm_steps"] < previous["sqlite_vm_steps"]
        assert current["sql_parameter_bytes"] < previous["sql_parameter_bytes"]
        for name, counts in (("array", previous), ("object", current)):
            for field in ("sql_calls", "returned_rows", "returned_value_bytes",
                          "sql_parameter_bytes", "sqlite_vm_steps"):
                record_testsuite_property(f"report_4932_twelve_month_{name}_{field}", counts[field])
        record_testsuite_property("report_4932_twelve_month_named_headers", 4932)
        # Damage the last named source in each distinct month. No month may
        # borrow another month's success or skip a trailing exact header.
        for month, refs in by_month.items():
            connection.execute("UPDATE fact_revision SET digest=zeroblob(32) WHERE id=?",
                               (refs[-1][0],))
            with pytest.raises(KernelError, match="冻结报表分类来源摘要不一致"):
                _flow_classification_headers_match(connection, month, refs, [])


def test_selected_header_work_returns_one_status_and_ignores_unrelated_growth(
    record_testsuite_property,
):
    with closing(_connection()) as connection:
        selected = tuple((f"selected-{index:04}", FIRST.hex()) for index in range(400))
        connection.executemany(
            "INSERT INTO fact_revision VALUES(?,?,10,'sa')",
            ((ident, FIRST) for ident, _ in selected),
        )
        connection.executemany(
            "INSERT INTO fact_report_classification VALUES(?,'selected-voucher')",
            ((ident,) for ident, _ in selected),
        )
        vouchers = [f"voucher-{index}" for index in range(1000)] + ["selected-voucher"]

        def current(observed):
            return _flow_classification_headers_match(observed, 10, selected, vouchers)

        former, previous = _measured(connection, lambda observed: _former_headers_match(
            observed, 10, selected, vouchers
        ))
        actual, before = _measured(connection, current)
        assert actual is former is True
        assert previous["returned_rows"] == len(selected)
        assert before["returned_rows"] == 1
        assert before["returned_value_bytes"] < previous["returned_value_bytes"]
        # Actual query parameter bytes prove the same-month check never sends
        # the unrelated voucher set through SQL, even when that set grows.
        without_vouchers, absent = _measured(connection, lambda observed:
            _flow_classification_headers_match(observed, 10, selected, [])
        )
        more_vouchers, larger = _measured(connection, lambda observed:
            _flow_classification_headers_match(
                observed, 10, selected, vouchers + [f"other-{i}" for i in range(10000)]
            )
        )
        assert without_vouchers is more_vouchers is True
        assert absent["sql_parameter_bytes"] == before["sql_parameter_bytes"]
        assert larger["sql_parameter_bytes"] == before["sql_parameter_bytes"]
        assert larger["sqlite_vm_steps"] == absent["sqlite_vm_steps"]
        assert larger["sqlite_vm_steps"] <= before["sqlite_vm_steps"]
        # Same named IDs and membership, with many other objects/months behind
        # the typed tables. Necessary head checks stay within the exact scope.
        connection.executemany(
            "INSERT INTO fact_revision VALUES(?,?,?,'sa')",
            ((f"unrelated-{index:05}", SECOND, index % 120) for index in range(12000)),
        )
        connection.executemany(
            "INSERT INTO fact_report_classification VALUES(?,?)",
            ((f"unrelated-{index:05}", f"unrelated-voucher-{index}") for index in range(12000)),
        )
        actual, after = _measured(connection, current)
        assert actual is True and after["returned_rows"] == 1
        assert after["returned_value_bytes"] == before["returned_value_bytes"]
        assert after["sqlite_vm_steps"] <= before["sqlite_vm_steps"] + 100
        for name, values in (("former", previous), ("same_month", before),
                             ("without_vouchers", absent), ("more_vouchers", larger),
                             ("grown", after)):
            for field in ("returned_rows", "returned_value_bytes", "sql_parameter_bytes",
                          "sqlite_vm_steps"):
                record_testsuite_property(f"report_header_{name}_{field}", values[field])
        # Prove a named source is still checked after unrelated growth.
        connection.execute("UPDATE fact_revision SET digest=zeroblob(32) WHERE id=?",
                           (selected[-1][0],))
        with pytest.raises(KernelError, match="冻结报表分类来源摘要不一致"):
            current(connection)


def test_cross_month_probe_defers_null_and_digest_priority_to_complete_rows():
    with closing(_connection()) as connection:
        # Same-month wrong digest cannot outrank an eligible cross-month NULL
        # digest, which the former reader encountered while forming tuples.
        connection.execute("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='a'")
        connection.execute("UPDATE fact_revision SET digest=NULL WHERE id='b'")
        expected = _outcome(lambda: _former_headers_match(connection, 10, REFS, ["vb"]))
        actual = _outcome(lambda: _flow_classification_headers_match(
            connection, 10, REFS, ["vb"]
        ))
        assert actual == expected == (AttributeError, "'NoneType' object has no attribute 'hex'")
        # The ineligible NULL source does not affect the eligible digest error.
        expected = _outcome(lambda: _former_headers_match(connection, 10, REFS, []))
        actual = _outcome(lambda: _flow_classification_headers_match(connection, 10, REFS, []))
        assert actual == expected
        assert actual[1:3] == ("content_integrity_failed", "冻结报表分类来源摘要不一致")


def test_formal_closed_month_and_quarter_keep_statements_and_cached_results(book):
    engine = book[0]
    reports = scenario(book)
    expected = reports.report(2026, 1)["statements"]
    close_quarter(book)
    assert reports.report(2026, 1, source="closed")["statements"] == expected
    board = Dashboard(engine)
    result = board.quarterly_report(2026, 1, preparation="deferred")
    assert result["export"]["available"]
    assert result["summary"]["assets_total_fen"] == expected["balance_sheet"]["30"]["ending_fen"]
    with QueryReads.snapshot(engine) as reads:
        for value in ("2026-01", "2026-02", "2026-03"):
            period = YearMonth(value).ordinal
            flow = read_report_flow(reads.connection, period, reads=reads)
            assert flow is not None
            assert read_report_flow(reads.connection, period, reads=reads) is flow


def test_failed_named_header_does_not_publish_same_snapshot_flow_cache(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    period = YearMonth("2026-02").ordinal
    with engine.store.connection(read_only=True) as connection:
        ident = connection.execute(
            "SELECT f.id FROM fact_revision f JOIN subject s ON s.id=f.subject_id "
            "WHERE s.kind='report_classification' AND f.period=?", (period,),
        ).fetchone()[0]
    damage(engine, "fact_report_classification",
           "DELETE FROM fact_report_classification WHERE revision_id=?", (ident,),
           foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        for _ in range(2):
            with pytest.raises(KernelError, match="冻结报表分类来源缺失"):
                read_report_flow(reads.connection, period, reads=reads)
            assert ("report_period_flow", period) not in reads._report_snapshot_cache
