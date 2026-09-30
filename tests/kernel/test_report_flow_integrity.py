"""Complete flow checks may reuse only independently verified source results."""

import pytest
from test_reports import book as book  # noqa: F401
from test_reports import close_quarter, scenario

from ai_accounting.kernel import report_projection
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import _check_sources
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.report_flow import require_report_flow
from ai_accounting.kernel.report_projection import require_report_projection
from ai_accounting.kernel.report_semantics import (
    compare_report_semantics,
    require_report_semantics,
)
from ai_accounting.kernel.verified_source_lease import (
    verified_calculation_source,
    verified_source_lease,
)


def test_full_verification_decodes_closes_once_for_all_projection_checks(book, monkeypatch):
    from ai_accounting.kernel import close_storage
    from ai_accounting.kernel.integrity import verify_integrity

    engine = book[0]
    scenario(book)
    close_quarter(book)
    calls = []
    original = close_storage.decode_close

    def counted(connection, row, **options):
        calls.append(row["period"])
        return original(connection, row, **options)

    monkeypatch.setattr(close_storage, "decode_close", counted)

    def verify():
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return verify_integrity(engine, connection)

    assert verify()["counts"]["closes"] == 3
    # Projection and full reference-directory comparisons reuse this transaction's
    # authenticated manifests, but still independently compare all derived rows.
    assert len(calls) == len(set(calls)) == 3
    calls.clear()
    assert verify()["status"] == "verified"
    assert len(calls) == len(set(calls)) == 3
    with engine.store.connection() as connection:
        connection.execute("UPDATE report_period_flow SET content='{}'")
        connection.commit()
    with pytest.raises(KernelError, match="月度汇总"):
        verify()


def test_flow_reuses_verified_source_work_only_inside_one_active_snapshot(book, monkeypatch):
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
            semantics = require_report_semantics(engine, first, _return_verified=True)
            original = report_projection._manifest_rows

            def forbidden(*_args, **_kwargs):
                raise AssertionError("flow read the already verified close sources again")

            monkeypatch.setattr(report_projection, "_manifest_rows", forbidden)
            assert (
                require_report_flow(
                    engine, first, _verified_reports=reports, _verified_semantics=semantics
                )["rows"]
                == 3
            )
            with pytest.raises(AssertionError):
                require_report_flow(engine, first)
            with pytest.raises(KernelError):
                require_report_flow(
                    engine, second, _verified_reports=reports, _verified_semantics=semantics
                )
            first.commit()
            first.execute("BEGIN")
            with pytest.raises(KernelError):
                require_report_flow(
                    engine, first, _verified_reports=reports, _verified_semantics=semantics
                )
            monkeypatch.setattr(report_projection, "_manifest_rows", original)
        with verified_source_lease(first):
            with pytest.raises(KernelError):
                require_report_flow(
                    engine, first, _verified_reports=reports, _verified_semantics=semantics
                )
            fresh_reports = require_report_projection(engine, first, _return_verified=True)
            fresh_semantics = require_report_semantics(engine, first, _return_verified=True)
            assert (
                require_report_flow(
                    engine,
                    first,
                    _verified_reports=fresh_reports,
                    _verified_semantics=fresh_semantics,
                )["rows"]
                == 3
            )


def test_report_semantics_reuses_verified_raw_calculations_without_reloading(book, monkeypatch):
    engine = book[0]
    scenario(book)
    close_quarter(book)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        normal = compare_report_semantics(engine, connection)
        source = _check_sources(engine, connection)
        with verified_source_lease(connection):
            token = verified_calculation_source(connection, source)

            def forbidden(*_args, **_kwargs):
                raise AssertionError("already verified calculations were loaded again")

            monkeypatch.setattr(QueryReads, "prime_calculations", forbidden)
            reused = compare_report_semantics(engine, connection, _verified_source=token)
            assert reused["expected_rows"] == normal["expected_rows"]
            assert reused["expected_seals"] == normal["expected_seals"]
            assert reused["changed"] == normal["changed"] is False
        connection.commit()
        connection.execute("BEGIN")
        with verified_source_lease(connection), pytest.raises(ValueError):
            compare_report_semantics(engine, connection, _verified_source=token)
