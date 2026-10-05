"""Frozen classification headers preserve full-row identity and error decisions."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from ai_accounting.kernel import report_flow, report_flow_v1
from ai_accounting.kernel.content_history_context import historical_content, report_flow_reader
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.types import canonical


class _Cursor:
    def __init__(self, cursor, owner):
        self.cursor = cursor
        self.owner = owner

    def __iter__(self):
        rows = list(self.cursor)
        self.owner.rows += len(rows)
        return iter(rows)

    def fetchone(self):
        row = self.cursor.fetchone()
        self.owner.rows += int(row is not None)
        return row


class _Connection:
    def __init__(self, raw):
        self.raw = raw
        self.rows = 0
        self.queries = 0
        self.vm_steps = 0

    def _progress(self):
        self.vm_steps += 1
        return 0

    def execute(self, sql, params=()):
        self.queries += 1
        self.raw.set_progress_handler(self._progress, 1)
        return _Cursor(self.raw.execute(sql, params), self)


@pytest.fixture
def database():
    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.executescript(
        "CREATE TABLE period_close(period INTEGER PRIMARY KEY,digest BLOB);"
        "CREATE TABLE report_period_flow(posting_period INTEGER PRIMARY KEY,"
        "content TEXT,close_digest BLOB,root_digest BLOB);"
        "CREATE TABLE subject(id TEXT PRIMARY KEY,kind TEXT);"
        "CREATE TABLE fact_revision(id TEXT PRIMARY KEY,subject_id TEXT,"
        "period INTEGER,digest BLOB);"
        "CREATE TABLE fact_report_classification(revision_id TEXT PRIMARY KEY,"
        "voucher_version_id TEXT);"
    )
    yield connection
    connection.close()


def _add(connection, ident, *, period=7, voucher="selected"):
    checksum = ident.encode().ljust(32, b"\0")[:32]
    connection.execute("INSERT INTO subject VALUES(?,'report_classification')", (ident,))
    connection.execute(
        "INSERT INTO fact_revision VALUES(?,?,?,?)", (ident, ident, period, checksum)
    )
    connection.execute("INSERT INTO fact_report_classification VALUES(?,?)", (ident, voucher))
    return ident, checksum.hex()


def _store(connection, monkeypatch, refs, *, period=7, vouchers=("selected",)):
    close_digest, source_root, semantic_root = b"c" * 32, b"r" * 32, b"s" * 32
    content = {"classification_refs": refs, "source_vouchers": vouchers, "profit": [], "cash": []}
    raw = canonical(content)
    bound = report_flow._root(period, close_digest, source_root, semantic_root, raw)
    connection.execute("INSERT INTO period_close VALUES(?,?)", (period, close_digest))
    connection.execute(
        "INSERT INTO report_period_flow VALUES(?,?,?,?)", (period, raw, close_digest, bound)
    )
    roots = {"report_flow": bound, "report": source_root, "report_semantics": semantic_root}
    reader = SimpleNamespace(
        verified_header=lambda *_args, **_kwargs: roots,
        derived_root=lambda header, name: header[name],
    )
    monkeypatch.setattr(report_flow, "close_reader", lambda: reader)
    return json.loads(raw) | {"root": source_root}


def _old_decision(connection, refs, period, vouchers):
    identifiers = [ident for ident, _ in refs]
    rows = list(connection.execute(
        "SELECT f.id,f.digest,f.period,s.kind,c.voucher_version_id "
        "FROM json_each(?) ids LEFT JOIN fact_revision f ON f.id=ids.value "
        "LEFT JOIN subject s ON s.id=f.subject_id "
        "LEFT JOIN fact_report_classification c ON c.revision_id=f.id ORDER BY ids.value",
        (canonical(identifiers),),
    ))
    if len(rows) != len(refs) or any(
        row["id"] is None or row["kind"] != "report_classification"
        or row["voucher_version_id"] is None for row in rows
    ):
        return "missing"
    selected = tuple(
        (row["id"], row["digest"].hex()) for row in rows
        if row["period"] == period or row["voucher_version_id"] in set(vouchers)
    )
    if tuple(refs) != selected:
        digests = dict(selected)
        if any(ident in digests and digests[ident] != checksum for ident, checksum in refs):
            return "digest"
        return "scope"
    return "valid"


@pytest.mark.parametrize("case", [
    "valid", "empty", "duplicate", "duplicate_digest", "unsorted", "missing_fact",
    "missing_subject", "missing_binding", "wrong_kind", "digest", "scope", "outside_digest",
    "mixed", "invalid_and_digest", "numeric_id", "numeric_voucher", "string_period",
    "escaped_unicode", "nul_ids", "duplicate_digest_reversed", "unusual_digest",
])
def test_named_headers_match_full_row_decision_and_never_cache_failure(database, monkeypatch, case):
    refs = [_add(database, "a"), _add(database, "b")]
    period, vouchers = 7, ["selected"]
    if case == "empty":
        refs = []
    elif case == "duplicate":
        refs.insert(0, refs[0])
    elif case == "duplicate_digest":
        refs.insert(0, ("a", "0" * 64))
    elif case == "duplicate_digest_reversed":
        refs.insert(1, ("a", "0" * 64))
    elif case == "unsorted":
        refs.reverse()
    elif case == "missing_fact":
        database.execute("DELETE FROM fact_revision WHERE id='b'")
    elif case == "missing_subject":
        database.execute("DELETE FROM subject WHERE id='b'")
    elif case == "missing_binding":
        database.execute("DELETE FROM fact_report_classification WHERE revision_id='b'")
    elif case == "wrong_kind":
        database.execute("UPDATE subject SET kind='other' WHERE id='b'")
    elif case == "digest":
        database.execute("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='a'")
    elif case in {"scope", "outside_digest", "mixed"}:
        database.execute("UPDATE fact_revision SET period=6 WHERE id='b'")
        database.execute(
            "UPDATE fact_report_classification SET voucher_version_id='other' WHERE revision_id='b'"
        )
        if case == "outside_digest":
            database.execute("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='b'")
        elif case == "mixed":
            database.execute("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='a'")
    elif case == "invalid_and_digest":
        database.execute("DELETE FROM subject WHERE id='b'")
        database.execute("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='a'")
    elif case == "numeric_id":
        ident, checksum = _add(database, "123")
        refs = [(123, checksum)]
    elif case == "numeric_voucher":
        refs = [_add(database, "number", period=6, voucher="123")]
        vouchers = [123]
    elif case == "string_period":
        period, vouchers = "7", []
    elif case == "escaped_unicode":
        refs = [_add(database, ident) for ident in sorted(('a"b', "a\\b", "中文😀", "é"))]
    elif case == "nul_ids":
        refs = [_add(database, ident) for ident in ("a\0b", "a\0c")]
    elif case == "unusual_digest":
        refs[0] = ("a", {"b": 2, "a": 1})
    expected = _old_decision(database, refs, period, vouchers)
    content = _store(database, monkeypatch, refs, period=period, vouchers=vouchers)
    connection = _Connection(database)
    reads = SimpleNamespace(connection=connection, _snapshot_active=True, _report_snapshot_cache={})
    messages = {"missing": "冻结报表分类来源缺失", "digest": "冻结报表分类来源摘要不一致"}
    for attempt in range(2):
        if attempt:
            reads._report_snapshot_cache["unrelated_verified"] = True
        if expected in messages:
            with pytest.raises(KernelError, match=messages[expected]) as caught:
                report_flow.read_report_flow(connection, period, reads=reads)
            assert caught.value.code == "content_integrity_failed"
        else:
            actual = report_flow.read_report_flow(connection, period, reads=reads)
            assert actual == (content if expected == "valid" else None)
        cached = ("report_period_flow", period) in reads._report_snapshot_cache
        assert cached == (expected == "valid")
    cross_month = case in {"scope", "outside_digest", "mixed", "missing_fact"}
    query_count = 4 if cross_month else 3
    assert connection.queries == query_count * (1 if expected == "valid" else 2)
    fallback = case in {"numeric_id", "numeric_voucher", "string_period"}
    per_read = 2 + (len(refs) if fallback else 1 + (len(refs) if cross_month else 0))
    assert connection.rows == per_read * (1 if expected == "valid" else 2)
    database.set_progress_handler(None, 0)


@pytest.mark.parametrize("changes,vouchers", [
    (("UPDATE fact_revision SET digest=NULL WHERE id='a'",), ["selected"]),
    (("UPDATE fact_revision SET digest=NULL WHERE id='a'",
      "DELETE FROM subject WHERE id='b'"), ["selected"]),
    (("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='a'",
      "UPDATE fact_revision SET period=6,digest=NULL WHERE id='b'"), ["selected"]),
    (("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='a'",
      "UPDATE fact_revision SET period=6,digest=NULL WHERE id='b'",
      "UPDATE fact_report_classification SET voucher_version_id='other' WHERE revision_id='b'"),
     ["selected"]),
    (("UPDATE fact_revision SET period=6,digest=NULL WHERE id='b'",
      "UPDATE fact_report_classification SET voucher_version_id='other' WHERE revision_id='b'"),
     ["selected"]),
])
def test_null_missing_and_mixed_month_priority_never_publishes_a_failed_flow(
    database, monkeypatch, changes, vouchers,
):
    refs = [_add(database, "a"), _add(database, "b")]
    _store(database, monkeypatch, refs, vouchers=vouchers)
    for statement in changes:
        database.execute(statement)
    try:
        decision = _old_decision(database, refs, 7, vouchers)
    except AttributeError as error:
        expected_error, expected_message = AttributeError, str(error)
    else:
        messages = {"missing": "冻结报表分类来源缺失", "digest": "冻结报表分类来源摘要不一致"}
        expected_error = KernelError if decision in messages else None
        expected_message = messages.get(decision)
    connection = _Connection(database)
    reads = SimpleNamespace(connection=connection, _snapshot_active=True, _report_snapshot_cache={})
    for _ in range(2):
        if expected_error is not None:
            with pytest.raises(expected_error) as caught:
                report_flow.read_report_flow(connection, 7, reads=reads)
            assert str(caught.value) == expected_message
        else:
            assert report_flow.read_report_flow(connection, 7, reads=reads) is None
        assert ("report_period_flow", 7) not in reads._report_snapshot_cache
    database.set_progress_handler(None, 0)


def test_earlier_header_error_precedes_later_body_error_and_keeps_month_cache_separate(
    database, monkeypatch,
):
    first_refs = [_add(database, "first", period=7)]
    second_refs = [_add(database, "second", period=8)]
    first = _store(database, monkeypatch, first_refs, period=7)
    _store(database, monkeypatch, second_refs, period=8)
    roots = {
        row["posting_period"]: {
            "report_flow": row["root_digest"], "report": b"r" * 32, "report_semantics": b"s" * 32,
        }
        for row in database.execute("SELECT posting_period,root_digest FROM report_period_flow")
    }
    reader = SimpleNamespace(
        verified_header=lambda _connection, close, **_kwargs: roots[close["period"]],
        derived_root=lambda header, name: header[name],
    )
    monkeypatch.setattr(report_flow, "close_reader", lambda: reader)
    database.execute("UPDATE fact_revision SET digest=zeroblob(32) WHERE id='first'")
    database.execute("UPDATE report_period_flow SET content='{}' WHERE posting_period=8")
    connection = _Connection(database)
    reads = SimpleNamespace(connection=connection, _snapshot_active=True, _report_snapshot_cache={})
    for _ in range(2):
        with pytest.raises(KernelError, match="冻结报表分类来源摘要不一致"):
            for month in (7, 8):
                report_flow.read_report_flow(connection, month, reads=reads)
        assert ("report_period_flow", 7) not in reads._report_snapshot_cache
        assert ("report_period_flow", 8) not in reads._report_snapshot_cache
    database.execute("UPDATE fact_revision SET digest=? WHERE id='first'",
                     (bytes.fromhex(first_refs[0][1]),))
    assert report_flow.read_report_flow(connection, 7, reads=reads) == first
    for _ in range(2):
        with pytest.raises(KernelError, match="报表月度汇总与冻结来源不一致"):
            report_flow.read_report_flow(connection, 8, reads=reads)
        assert ("report_period_flow", 7) in reads._report_snapshot_cache
        assert ("report_period_flow", 8) not in reads._report_snapshot_cache
    database.set_progress_handler(None, 0)


def test_all_named_headers_checked_with_bounded_vm_and_reused_success(
    database, monkeypatch, record_testsuite_property,
):
    refs = [_add(database, f"classification-{number:04d}") for number in range(4932)]
    content = _store(database, monkeypatch, refs)
    connection = _Connection(database)
    reads = SimpleNamespace(connection=connection, _snapshot_active=True, _report_snapshot_cache={})
    assert report_flow.read_report_flow(connection, 7, reads=reads) == content
    assert connection.rows == 3
    # Count all real statements and returned rows, including the close and
    # bound flow body. Every named identity is still checked within this gate.
    assert connection.vm_steps < 4932 * 60
    record_testsuite_property("report_4932_same_month_sqlite_vm_steps", connection.vm_steps)
    record_testsuite_property("report_4932_same_month_all_returned_rows", connection.rows)
    assert report_flow.read_report_flow(connection, 7, reads=reads) == content
    assert connection.queries == 3
    database.set_progress_handler(None, 0)
    reads._report_snapshot_cache.clear()
    database.execute("DELETE FROM fact_revision WHERE id=?", (refs[-1][0],))
    for _ in range(2):
        with pytest.raises(KernelError, match="冻结报表分类来源缺失"):
            report_flow.read_report_flow(connection, 7, reads=reads)
    assert connection.rows == 3 + 2 * (3 + len(refs))
    assert ("report_period_flow", 7) not in reads._report_snapshot_cache
    database.set_progress_handler(None, 0)


def test_historical_dispatch_keeps_fixed_v1_flow_reader(monkeypatch):
    calls = []
    monkeypatch.setattr(
        report_flow, "require_report_flow", lambda *_args, **_kwargs: calls.append(2)
    )
    monkeypatch.setattr(
        report_flow_v1, "require_report_flow", lambda *_args, **_kwargs: calls.append(1)
    )
    with historical_content(1):
        assert report_flow_reader() is report_flow_v1
        report_flow_reader().require_report_flow(None, None)
    assert report_flow_reader() is report_flow
    report_flow_reader().require_report_flow(None, None)
    assert calls == [1, 2]
