"""Failed read resources discard proofs and preserve the original failure."""

import sqlite3
from contextlib import contextmanager

import pytest
from test_engine import engine as engine_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.resident_reads import ResidentReadPool
from ai_accounting.kernel.storage import _active_fact_reads

engine = engine_fixture


@contextmanager
def attached(engine):
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        with QueryReads.attached_snapshot(engine, connection) as reads:
            yield reads


@pytest.mark.parametrize("scope,resident", [
    (QueryReads.snapshot, False), (QueryReads.snapshot, True),
    (attached, False), (attached, True),
])
@pytest.mark.parametrize("failure", [
    None, RuntimeError("synthetic body failure"),
    KernelError("content_integrity_failed", "synthetic body failure"),
    KeyboardInterrupt("synthetic body interruption"),
])
def test_closed_read_resource_restores_parent_and_invalidates_every_proof(
    engine, scope, resident, failure,
):
    pool = ResidentReadPool() if resident else None
    engine.store.read_pool = pool
    escaped = None
    try:
        with QueryReads.snapshot(engine) as parent:
            parent._report_snapshot_cache["parent"] = "retained"
            expected = sqlite3.ProgrammingError if failure is None else type(failure)
            with pytest.raises(expected) as error:
                with scope(engine) as reads:
                    escaped = reads
                    assert _active_fact_reads.get() is reads
                    assert reads.connection.execute("SELECT 1").fetchone()[0] == 1
                    reads._report_snapshot_cache["failed-proof"] = object()
                    reads._facts["failed-fact"] = object()
                    reads.connection.close()
                    if failure is not None:
                        raise failure
            if failure is not None:
                assert error.value is failure
            else:
                assert "closed database" in str(error.value)
            assert not escaped._snapshot_active
            assert escaped._snapshot_changes is None
            assert not escaped._report_snapshot_cache and not escaped._facts
            assert _active_fact_reads.get() is parent
            assert parent._report_snapshot_cache == {"parent": "retained"}
            with QueryReads.snapshot(engine) as fresh:
                assert fresh.connection is not escaped.connection
                assert fresh.connection.execute("SELECT 1").fetchone()[0] == 1
            assert _active_fact_reads.get() is parent
        assert _active_fact_reads.get() is None
    finally:
        if pool is not None:
            pool.close()
        engine.store.read_pool = None


@pytest.mark.parametrize("resident", [False, True])
def test_handled_outer_failure_does_not_hide_successful_scope_cleanup_error(engine, resident):
    pool = ResidentReadPool() if resident else None
    engine.store.read_pool = pool
    try:
        try:
            raise RuntimeError("already handled outside this snapshot")
        except RuntimeError:
            with pytest.raises(sqlite3.ProgrammingError, match="closed database"):
                with QueryReads.snapshot(engine) as reads:
                    reads.connection.close()
            assert _active_fact_reads.get() is None
        with QueryReads.snapshot(engine) as fresh:
            assert fresh.connection.execute("SELECT 1").fetchone()[0] == 1
    finally:
        if pool is not None:
            pool.close()
        engine.store.read_pool = None


@pytest.mark.parametrize("body_fails", [False, True])
def test_unrelated_cleanup_failure_is_reported_after_context_and_proofs_reset(
    engine, monkeypatch, body_fails,
):
    original_reset = QueryReads._reset
    escaped = None
    cleanup_failure = sqlite3.OperationalError("synthetic unrelated cleanup failure")

    def fail_reset(reads):
        original_reset(reads)
        if reads is escaped:
            raise cleanup_failure

    with monkeypatch.context() as fault:
        fault.setattr(QueryReads, "_reset", fail_reset)
        with pytest.raises(sqlite3.OperationalError) as error:
            with QueryReads.snapshot(engine) as reads:
                escaped = reads
                reads._report_snapshot_cache["temporary"] = object()
                if body_fails:
                    raise RuntimeError("synthetic failed body")
    assert error.value is cleanup_failure
    assert _active_fact_reads.get() is None
    assert not escaped._snapshot_active and not escaped._report_snapshot_cache
    with QueryReads.snapshot(engine) as fresh:
        assert fresh.connection.execute("SELECT 1").fetchone()[0] == 1
