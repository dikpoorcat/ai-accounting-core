"""Current snapshot header support is grouped without weakening frozen roots."""

import hashlib
import json
import sqlite3

import pytest
from test_close_accounting_filter import _close_empty_months, _closed_charge
from test_engine import engine as engine_fixture
from test_integrity_content import damage

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth, canonical

engine = engine_fixture


@pytest.fixture
def closed_pair(engine):
    first = _closed_charge(engine)
    _close_empty_months(engine, ["2026-02"])
    return first, YearMonth("2026-02").ordinal


def _rows(connection, periods):
    return list(connection.execute(
        "SELECT * FROM period_close WHERE period IN (?,?) ORDER BY period", periods
    ))


def _support_counts(statements):
    return (
        sum("close_storage_root" in sql for sql in statements),
        sum("read_index_source" in sql for sql in statements),
        sum("SELECT company_id,database_id FROM identity WHERE id=1" in sql for sql in statements),
    )


class ExactKeys(dict):
    """Unrelated cached months must never drive this exact request's work."""

    def __iter__(self):
        raise AssertionError("whole-cache traversal")

    def items(self):
        raise AssertionError("whole-cache traversal")

    def values(self):
        raise AssertionError("whole-cache traversal")


@pytest.mark.parametrize("cache_months", [12, 48, 120])
@pytest.mark.parametrize("route", ["authority", "accounting"])
def test_grouped_headers_exact_request_work(engine, closed_pair, cache_months, route):
    with engine.store.connection(read_only=True) as connection:
        rows = _rows(connection, closed_pair)
        expected = tuple(close_storage.verified_header(connection, row) for row in rows)
    with QueryReads.snapshot(engine) as reads:
        reads._close_headers = ExactKeys({-i - 1: None for i in range(cache_months)})
        reads._authoritative_closes = ExactKeys({-i - 1: None for i in range(cache_months)})
        reads._closes = ExactKeys({-i - 1: None for i in range(cache_months)})
        statements = []
        reads.connection.set_trace_callback(statements.append)
        if route == "authority":
            assert [row["period"] for row in reads.authoritative_close_rows(
                periods=closed_pair
            )] == list(closed_pair)
        else:
            result = reads.close_accounting_many(rows, subjects={"included"})
            assert [item["subject_id"] for item in result[0].adopted_results] == ["included"]
            assert result[1].adopted_results == ()
        assert tuple(reads._close_headers[p] for p in closed_pair) == expected
        assert _support_counts(statements) == (1, 1, 1)
        statements.clear()
        if route == "authority":
            reads.authoritative_close_rows(periods=closed_pair)
        else:
            reads.close_accounting_many(rows, subjects={"included"})
        assert _support_counts(statements) == (0, 0, 0)


@pytest.mark.parametrize("table,column", [
    ("close_storage_root", "storage_digest"),
    ("read_index_source", "source_digest"),
    ("close_storage_subroot", "digest"),
])
def test_last_damage_has_no_prefix_and_retry_rechecks(engine, closed_pair, table, column):
    last = closed_pair[-1]
    where = "period=?" if table != "read_index_source" else "source_kind='close' AND source_id=?"
    if table == "close_storage_subroot":
        where += " AND family='accounting'"
    params = (str(last) if table == "read_index_source" else last,)
    with engine.store.connection(read_only=True) as connection:
        original = connection.execute(
            f"SELECT {column} FROM {table} WHERE {where}", params
        ).fetchone()[0]
    damage(engine, table, f"UPDATE {table} SET {column}=? WHERE {where}", (b"x" * 32, *params))
    with QueryReads.snapshot(engine) as reads:
        rows = _rows(reads.connection, closed_pair)
        for _ in range(2):
            with pytest.raises(KernelError) as error:
                reads.close_accounting_many(rows, subjects={"included"})
            assert error.value.code == "content_integrity_failed"
            assert not reads._close_headers
            assert not reads._close_accounting_slices
            assert not reads._verified_close_storage_parts
            assert not reads._close_accounting_positions
        if table != "close_storage_subroot":
            with pytest.raises(KernelError):
                reads.authoritative_close_rows(periods=closed_pair)
            assert not reads._authoritative_closes
            assert not reads._close_headers
    damage(engine, table, f"UPDATE {table} SET {column}=? WHERE {where}", (original, *params))
    with QueryReads.snapshot(engine) as reads:
        result = reads.close_accounting_many(
            _rows(reads.connection, closed_pair), subjects={"included"}
        )
        assert result[0].adopted_results
        assert set(reads._close_headers) == set(closed_pair)


def test_only_owned_current_connection_can_group(engine, closed_pair):
    with QueryReads.snapshot(engine) as reads:
        assert close_storage._owned_header_snapshot(reads.connection)
        with engine.store.connection(read_only=True) as other:
            other.execute("BEGIN")
            assert not close_storage._owned_header_snapshot(other)
        with pytest.raises(sqlite3.DatabaseError):
            reads.connection.execute("COMMIT")
        with historical_content(1):
            assert not close_storage._owned_header_snapshot(reads.connection)
            statements = []
            reads.connection.set_trace_callback(statements.append)
            headers = close_storage.verified_headers(
                reads.connection, _rows(reads.connection, closed_pair)
            )
            assert len(headers) == 2
            assert _support_counts(statements) == (2, 2, 2)
    with engine.store.connection(read_only=True) as connection:
        assert not close_storage._owned_header_snapshot(connection)
        statements = []
        connection.set_trace_callback(statements.append)
        assert len(close_storage.verified_headers(connection, _rows(connection, closed_pair))) == 2
        assert _support_counts(statements) == (2, 2, 2)
        connection.execute("BEGIN")
        statements.clear()
        assert len(close_storage.verified_headers(connection, _rows(connection, closed_pair))) == 2
        assert _support_counts(statements) == (2, 2, 2)


def test_batch_scope_and_cutoff(engine, closed_pair):
    with QueryReads.snapshot(engine) as reads:
        rows = _rows(reads.connection, closed_pair)
        with pytest.raises(ValueError):
            close_storage.verified_headers(reads.connection, [rows[0], rows[0]])
        with pytest.raises(ValueError):
            close_storage.verified_headers(reads.connection, [{"period": True}])
        selected = reads.authoritative_close_rows(
            periods=closed_pair, through_period=closed_pair[0]
        )
        assert [row["period"] for row in selected] == [closed_pair[0]]
        assert set(reads._close_headers) == {closed_pair[0]}
        assert [row["period"] for row in reads.authoritative_close_rows(
            periods=closed_pair
        )] == list(closed_pair)


@pytest.mark.parametrize("change", ["identity", "contract"])
def test_last_resealed_root_still_checks_identity_and_contract(engine, closed_pair, change):
    last = closed_pair[-1]
    with engine.store.connection(read_only=True) as connection:
        root = json.loads(_rows(connection, closed_pair)[-1]["manifest"])
    if change == "identity":
        root["company_id"] = root["small"]["company_id"] = "unrelated-company"
    else:
        root["source_changes"]["highwater"] = True
    manifest = canonical(root)
    damage(engine, "period_close", "UPDATE period_close SET manifest=? WHERE period=?",
           (manifest, last))
    damage(engine, "close_storage_root",
           "UPDATE close_storage_root SET storage_digest=? WHERE period=?",
           (hashlib.sha256(manifest.encode()).digest(), last))
    with QueryReads.snapshot(engine) as reads:
        with pytest.raises(KernelError) as error:
            reads.authoritative_close_rows(periods=closed_pair)
        assert error.value.code == "content_integrity_failed"
        assert not reads._close_headers
        assert not reads._authoritative_closes
