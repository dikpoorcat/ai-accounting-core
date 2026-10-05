"""Exact report-source reuse preserves authority and each semantic comparison."""

from contextlib import contextmanager
from dataclasses import replace
from types import SimpleNamespace

import pytest
from test_integrity_content import damage
from test_reports import book as book  # noqa: F401
from test_reports import close_quarter, scenario

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import (
    _check_sources,
    verify_close_integrity,
    verify_integrity,
)
from ai_accounting.kernel.report_projection import require_report_projection
from ai_accounting.kernel.report_semantics import (
    compare_report_semantics,
    require_report_semantics,
)
from ai_accounting.kernel.reports import Reports
from ai_accounting.kernel.types import YearMonth
from ai_accounting.kernel.verified_source_lease import (
    verified_calculation_source,
    verified_source_lease,
)


@contextmanager
def _manifest_reads(connection):
    """Count executed authoritative SQL, while running the actual source reader."""
    queries = []

    def traced(sql):
        if sql.startswith("SELECT v.id,v.voucher_id,v.calculation_id,v.reverses_id,v.total,"):
            queries.append(sql)

    connection.set_trace_callback(traced)
    try:
        yield queries
    finally:
        connection.set_trace_callback(None)


def test_verified_rows_keep_exact_semantics_and_roots_with_one_source_read(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        source = _check_sources(engine, connection)
        with verified_source_lease(connection):
            calculations = verified_calculation_source(connection, source)
            with _manifest_reads(connection) as independent_queries:
                independent_reports = require_report_projection(
                    engine, connection, _return_verified=True
                )
                independent = compare_report_semantics(
                    engine, connection, _verified_source=calculations
                )
            with _manifest_reads(connection) as reused_queries:
                reports = require_report_projection(engine, connection, _return_verified=True)
                reused = compare_report_semantics(
                    engine,
                    connection,
                    _verified_source=calculations,
                    _verified_reports=reports,
                )
            assert (len(independent_queries), len(reused_queries)) == (6, 3)
            assert reports.closes == independent_reports.closes
            assert reused == independent
            assert reused["changed"] is False
            assert all(len(row) == 17 for _, prepared in reports.closes for row in prepared.rows)
            for period, semantics in reused["_verified_closed"]:
                close = connection.execute(
                    "SELECT * FROM period_close WHERE period=?", (period,)
                ).fetchone()
                header = close_storage.verified_header(connection, close, require_marker=True)
                assert semantics.root_digest == close_storage.derived_root(
                    header, "report_semantics"
                )


def test_full_and_close_verification_each_read_manifest_rows_once(book):
    engine = book[0]
    report = scenario(book)
    close_quarter(book)
    expected = report.report(2026, 1, source="closed")
    assert expected["statements"]["cash_flow_statement"]["22"]["current_fen"] == 40000
    # A new verification must read its own original sources again.
    for _ in range(2):
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            with _manifest_reads(connection) as queries:
                verified = verify_integrity(engine, connection)
            assert verified["status"] == "verified"
            assert verified["counts"]["closes"] == len(queries) == 3
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with _manifest_reads(connection) as queries:
            assert verify_close_integrity(engine, connection, "2026-02")["status"] == "verified"
        assert len(queries) == 2
    assert Reports(engine).report(2026, 1, source="closed") == expected


def test_semantics_rejects_unowned_wrong_connection_and_expired_leases(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with (
        engine.store.connection(read_only=True) as first,
        engine.store.connection(read_only=True) as second,
    ):
        first.execute("BEGIN")
        second.execute("BEGIN")
        with verified_source_lease(first):
            reports = require_report_projection(engine, first, _return_verified=True)
            unowned = SimpleNamespace(
                connection=first, closes=reports.closes, lease=reports.lease
            )
            with pytest.raises(KernelError, match="同一读取事务"):
                require_report_semantics(engine, first, _verified_reports=unowned)
            with pytest.raises(KernelError, match="同一读取事务"):
                require_report_semantics(engine, second, _verified_reports=reports)
            with verified_source_lease(first):
                with pytest.raises(KernelError, match="同一读取事务"):
                    require_report_semantics(engine, first, _verified_reports=reports)
            assert require_report_semantics(engine, first, _verified_reports=reports)[
                "periods"
            ] == 3
            first.commit()
            first.execute("BEGIN")
            with pytest.raises(KernelError, match="同一读取事务"):
                require_report_semantics(engine, first, _verified_reports=reports)
        with verified_source_lease(first):
            with pytest.raises(KernelError, match="同一读取事务"):
                require_report_semantics(engine, first, _verified_reports=reports)
            fresh = require_report_projection(engine, first, _return_verified=True)
            assert require_report_semantics(engine, first, _verified_reports=fresh)["periods"] == 3


def test_semantics_requires_exact_period_scope_and_close_identity(book):
    from ai_accounting.kernel.report_projection_v1 import _VerifiedReports as V1Reports

    engine = book[0]
    scenario(book)
    close_quarter(book)
    feb = YearMonth("2026-02").ordinal
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with verified_source_lease(connection):
            full = require_report_projection(engine, connection, _return_verified=True)
            prefix = require_report_projection(
                engine, connection, through_period=feb, _return_verified=True
            )
            with pytest.raises(KernelError, match="关账集合"):
                require_report_semantics(
                    engine, connection, through_period=feb, _verified_reports=full
                )
            with pytest.raises(KernelError, match="关账集合"):
                require_report_semantics(engine, connection, _verified_reports=prefix)
            assert require_report_semantics(
                engine, connection, through_period=feb, _verified_reports=prefix
            )["periods"] == 2
            period, prepared = full.closes[0]
            altered = (
                replace(full, closes=full.closes + full.closes[:1]),
                replace(
                    full,
                    closes=((period, replace(prepared, period=period + 1)),) + full.closes[1:],
                ),
                replace(
                    full,
                    closes=((period, replace(prepared, close_digest=bytes(32))),) + full.closes[1:],
                ),
            )
            for token in altered:
                with pytest.raises(KernelError, match="关账集合"):
                    require_report_semantics(engine, connection, _verified_reports=token)
            historical = V1Reports(connection, full.closes, full.lease)
            with pytest.raises(KernelError, match="同一读取事务"):
                require_report_semantics(engine, connection, _verified_reports=historical)


@pytest.mark.parametrize(
    "sql",
    [
        "UPDATE report_semantic_line SET content='{}'",
        "DELETE FROM report_semantic_line WHERE posting_period="
        "(SELECT min(posting_period) FROM report_semantic_line)",
        "UPDATE report_semantic_seal SET root_digest=zeroblob(32)",
        "UPDATE report_semantic_seal SET row_count=row_count+1",
    ],
)
def test_verified_source_rows_still_reject_damaged_semantic_rows_and_seals(book, sql):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with engine.store.connection() as connection:
        connection.execute(sql)
        connection.commit()
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with verified_source_lease(connection):
            reports = require_report_projection(engine, connection, _return_verified=True)
            with pytest.raises(KernelError) as failure:
                require_report_semantics(engine, connection, _verified_reports=reports)
            assert failure.value.details["reason"] == "projection_mismatch"


def test_verified_source_rows_still_check_original_semantic_root(book, monkeypatch):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with verified_source_lease(connection):
            reports = require_report_projection(engine, connection, _return_verified=True)
            original = close_storage.derived_root

            def wrong_root(header, name):
                return bytes(32) if name == "report_semantics" else original(header, name)

            monkeypatch.setattr(close_storage, "derived_root", wrong_root)
            with pytest.raises(KernelError) as failure:
                require_report_semantics(engine, connection, _verified_reports=reports)
            assert failure.value.details["reason"] == "frozen_root_mismatch"


def test_full_verification_still_rejects_actual_original_voucher_damage(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    damage(engine, "voucher_line", "UPDATE voucher_line SET debit=debit+1 WHERE debit>0")
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError):
            verify_integrity(engine, connection)
