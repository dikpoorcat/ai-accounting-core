"""Historical report discovery preserves exact frozen checks and bounded reads."""

import pytest
from stage9_metrics import measure_work
from test_integrity_content import damage
from test_reports import book as book  # noqa: F401
from test_reports import close_quarter, scenario

from ai_accounting.kernel import close_storage
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.reports import _closed_report_fact_sources
from ai_accounting.kernel.types import YearMonth


def _rows(connection):
    return tuple(connection.execute("SELECT * FROM period_close ORDER BY period"))


def test_frozen_report_discovery_same_content_with_bounded_physical_reads(book, record_property):
    engine = book[0]
    scenario(book)
    close_quarter(book)

    def discover(grouped):
        with QueryReads.snapshot(engine) as reads:
            consumer = reads if grouped else QueryReads(engine, reads.connection)
            return _closed_report_fact_sources(
                reads.connection, YearMonth("2026-03"), consumer
            )

    original_work, expected = measure_work(engine, lambda: discover(False))
    grouped_work, actual = measure_work(engine, lambda: discover(True))
    assert expected and len({item["close_period"] for item in expected}) == 3
    assert actual == expected
    with QueryReads.snapshot(engine) as reads:
        assert _closed_report_fact_sources(
            reads.connection, YearMonth("2026-03"), reads
        ) == expected
        statements = []
        reads.connection.set_trace_callback(statements.append)
        again = _closed_report_fact_sources(
            reads.connection, YearMonth("2026-03"), reads
        )
        assert again == expected
        reads.connection.set_trace_callback(None)
        assert len(statements) == 1 and "SELECT * FROM period_close" in statements[0]
    assert grouped_work["counters"]["sql_calls"] < original_work["counters"]["sql_calls"]
    assert grouped_work["counters"]["returned_rows"] <= original_work["counters"]["returned_rows"]
    record_property("original_frozen_readiness_work", original_work["counters"])
    record_property("grouped_frozen_readiness_work", grouped_work["counters"])


@pytest.mark.parametrize("component", ["subroot", "directory", "block"])
def test_late_frozen_checker_damage_is_rejected_without_prefix_proofs(book, component):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    table, predicate = {
        "subroot": ("close_storage_subroot", "family='management'"),
        "directory": ("close_storage_directory", "field='readiness:financial_reports'"),
        "block": ("close_storage_block", "field='readiness:financial_reports'"),
    }[component]
    damage(engine, table, f"UPDATE {table} SET digest=? WHERE period=? AND {predicate}",
           (b"x" * 32, YearMonth("2026-03").ordinal))
    with QueryReads.snapshot(engine) as reads:
        rows = _rows(reads.connection)
        for _ in range(2):
            with pytest.raises(KernelError) as caught:
                reads.close_readiness_checks_many(rows, "financial_reports")
            assert caught.value.code == "content_integrity_failed"
            assert not reads._close_headers
            assert not reads._close_sections


def test_unowned_and_fixed_history_keep_independent_checker_reads(book):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        rows = _rows(connection)
        expected = tuple(close_storage.read_readiness_check(
            connection, close_storage.verified_header(connection, row), "financial_reports"
        ) for row in rows)
        reads = QueryReads(engine, connection)
        assert reads.close_readiness_checks_many(rows, "financial_reports") == expected
        assert not reads._close_headers and not reads._close_sections
    with QueryReads.snapshot(engine) as reads, historical_content(1):
        rows = _rows(reads.connection)
        assert reads.close_readiness_checks_many(rows, "financial_reports") == expected
