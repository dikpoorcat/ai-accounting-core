"""Independent model reconstruction borrows a real protected transaction."""

import sqlite3

import pytest

from ai_accounting.kernel import integrity
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard_model_integrity import authority_snapshot
from ai_accounting.kernel.dashboard_model_runtime import producing_model, production_reads
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store, _active_fact_reads
from ai_accounting.kernel.verified_source_lease import (
    verified_calculation_source,
    verified_source_lease,
)
from ai_accounting.kernel.verified_source_reads import VerifiedSourceReads, readonly_verification


@pytest.fixture
def engine(tmp_path):
    return Engine(Store.create(
        tmp_path / "company.sqlite", production_bundle(), "co", "911100000000000001", "db",
    ))


def snapshot(engine, connection):
    return authority_snapshot(engine, connection, "2026-01", as_of="2026-01-31")


def scratch(connection):
    connection.execute("CREATE TEMP TABLE guard_probe(value INTEGER)")
    connection.execute("BEGIN IMMEDIATE")
    connection.execute("INSERT INTO guard_probe VALUES(1)")


def test_writer_reconstruction_sees_pending_rows_and_restores_original_transaction(engine):
    with engine.store.connection() as connection:
        scratch(connection)
        connection.execute("SAVEPOINT caller")
        before = connection.total_changes
        with snapshot(engine, connection) as snap:
            reads = snap.reads
            assert snap.connection is connection and snap.as_of == "2026-01-31"
            assert reads is _active_fact_reads.get() and reads._snapshot_active
            assert producing_model()
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
            assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert connection.execute("SELECT value FROM guard_probe").fetchone()[0] == 1
            with pytest.raises(sqlite3.OperationalError, match="readonly"):
                connection.execute("INSERT INTO guard_probe VALUES(2)")
        assert not reads._snapshot_active and not producing_model()
        assert _active_fact_reads.get() is None and connection.in_transaction
        assert connection.total_changes == before
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        connection.execute("ROLLBACK TO caller")
        connection.execute("RELEASE caller")
        connection.execute("INSERT INTO guard_probe VALUES(3)")
        connection.rollback()


def test_body_failure_restores_writer_and_expires_reconstruction_reader(engine):
    with engine.store.connection() as connection:
        scratch(connection)
        before = connection.total_changes
        with pytest.raises(RuntimeError, match="synthetic"):
            with snapshot(engine, connection) as snap:
                reads = snap.reads
                raise RuntimeError("synthetic reconstruction interruption")
        assert connection.in_transaction and connection.total_changes == before
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        assert not reads._snapshot_active and _active_fact_reads.get() is None
        assert not producing_model()
        connection.execute("INSERT INTO guard_probe VALUES(2)")
        connection.rollback()


def test_existing_authorizer_is_retained_during_and_after_reconstruction(engine):
    with engine.store.connection() as connection:
        scratch(connection)
        denied = []

        def authorizer(action, first, *_):
            if action == sqlite3.SQLITE_DELETE and first == "guard_probe":
                denied.append(first)
                return sqlite3.SQLITE_DENY
            return sqlite3.SQLITE_OK

        connection.set_authorizer(authorizer)
        try:
            with snapshot(engine, connection):
                with pytest.raises(sqlite3.DatabaseError, match="authorized"):
                    connection.execute("DELETE FROM guard_probe")
            with pytest.raises(sqlite3.DatabaseError, match="authorized"):
                connection.execute("DELETE FROM guard_probe")
            assert denied == ["guard_probe", "guard_probe"]
            connection.execute("INSERT INTO guard_probe VALUES(2)")
        finally:
            connection.set_authorizer(None)
            connection.rollback()


@pytest.mark.parametrize("fails", [False, True])
@pytest.mark.parametrize("same_company", [False, True])
@pytest.mark.parametrize("scope", ["production", "authority"])
def test_independent_writer_cannot_borrow_or_replace_outer_read_snapshot(
    engine, tmp_path, fails, same_company, scope,
):
    other = engine if same_company else Engine(Store.create(
        tmp_path / "other.sqlite", production_bundle(), "other",
        "911100000000000002", "other-db",
    ))
    with QueryReads.snapshot(engine) as outer:
        before = engine.store.epochs(outer.connection)
        outer_token = outer._snapshot_token
        outer._verified_saved_input_identities.add("outer-only-proof")
        with other.store.connection() as writer:
            writer.execute("BEGIN IMMEDIATE")
            writer.execute("UPDATE state SET management=management+1 WHERE id=1")
            seen = []

            def reconstruct():
                context = (production_reads(other, writer) if scope == "production"
                           else snapshot(other, writer))
                with context as view:
                    inner = view if scope == "production" else view.reads
                    seen.append(inner)
                    assert inner is _active_fact_reads.get() and inner is not outer
                    assert inner.connection is writer and inner.store is other.store
                    assert not inner._verified_saved_input_identities
                    inner._verified_saved_input_identities.add("inner-only-proof")
                    assert other.store.epochs(writer)["management"] == before["management"] + 1
                    with pytest.raises(sqlite3.DatabaseError):
                        writer.execute("UPDATE state SET management=management+1")
                    if fails:
                        raise RuntimeError("synthetic independent writer interruption")

            if fails:
                with pytest.raises(RuntimeError, match="independent writer"):
                    reconstruct()
            else:
                reconstruct()
            assert _active_fact_reads.get() is outer
            assert outer._snapshot_active and outer._snapshot_token is outer_token
            assert outer._verified_saved_input_identities == {"outer-only-proof"}
            assert not seen[0]._snapshot_active
            assert not seen[0]._verified_saved_input_identities
            assert not producing_model()
            assert writer.execute("PRAGMA query_only").fetchone()[0] == 0
            writer.commit()
        assert engine.store.epochs(outer.connection) == before
        assert outer.connection.execute("PRAGMA query_only").fetchone()[0] == 1
    assert _active_fact_reads.get() is None
    with other.store.connection(read_only=True) as connection:
        assert other.store.epochs(connection)["management"] == before["management"] + 1


def test_production_cannot_promote_same_connection_or_nest_another_producer(engine):
    with QueryReads.snapshot(engine) as outer:
        with pytest.raises(KernelError) as error:
            with production_reads(engine, outer.connection):
                pytest.fail("read-only connection was promoted to a writer")
        assert error.value.code == "dashboard_model_scope_invalid"
        assert _active_fact_reads.get() is outer and outer._snapshot_active
    with engine.store.connection() as writer:
        writer.execute("BEGIN IMMEDIATE")
        with production_reads(engine, writer) as reads:
            with pytest.raises(KernelError) as error:
                with production_reads(engine, writer):
                    pytest.fail("producer proof was reused across a second scope")
            assert error.value.code == "dashboard_model_scope_invalid"
            assert _active_fact_reads.get() is reads and reads._snapshot_active
        writer.rollback()


@pytest.mark.parametrize("scope", ["production", "authority"])
def test_independent_writer_cannot_inherit_fixed_historical_read_rules(engine, scope):
    from ai_accounting.kernel import close_storage
    from ai_accounting.kernel.content_history_context import close_reader, historical_content

    with QueryReads.snapshot(engine) as outer, engine.store.connection() as writer:
        writer.execute("BEGIN IMMEDIATE")
        before = engine.store.epochs(writer)
        with historical_content(1):
            historical_reader = close_reader()
            context = (production_reads(engine, writer) if scope == "production"
                       else snapshot(engine, writer))
            with pytest.raises(KernelError):
                with context:
                    pytest.fail("current production inherited frozen historical rules")
            assert close_reader() is historical_reader and historical_reader is not close_storage
            assert _active_fact_reads.get() is outer and outer._snapshot_active
            assert engine.store.epochs(writer) == before
            assert writer.execute("PRAGMA query_only").fetchone()[0] == 0
            assert outer.connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert close_reader() is close_storage and not producing_model()
        writer.rollback()


@pytest.mark.parametrize("owner", ["query", "production"])
def test_nested_owned_reader_keeps_its_guard_and_caches(engine, owner):
    with engine.store.connection() as writer:
        if owner == "production":
            writer.execute("BEGIN IMMEDIATE")
        context = (QueryReads.snapshot(engine) if owner == "query"
                   else production_reads(engine, writer))
        with context as reads:
            connection = reads.connection
            token = reads._snapshot_token
            with snapshot(engine, connection) as snap:
                assert snap.reads is reads and _active_fact_reads.get() is reads
                assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
                with pytest.raises(sqlite3.DatabaseError):
                    connection.execute("COMMIT")
                if owner == "production":
                    with pytest.raises(sqlite3.DatabaseError):
                        connection.execute("PRAGMA query_only=OFF")
            assert reads._snapshot_active and reads._snapshot_token is token
            assert _active_fact_reads.get() is reads
            assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        assert not reads._snapshot_active and _active_fact_reads.get() is None
        if owner == "production":
            assert writer.in_transaction
            assert writer.execute("PRAGMA query_only").fetchone()[0] == 0
            writer.rollback()


def test_independent_reconstruction_preserves_completed_source_proof(engine):
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        with readonly_verification(connection), verified_source_lease(connection):
            source = integrity._check_sources(engine, connection)
            reads = VerifiedSourceReads(
                engine, connection, verified_calculation_source(connection, source),
            )
            assert reads.facts([]) == {} and not reads._snapshot_active
            with snapshot(engine, connection) as snap:
                assert snap.reads is not reads and snap.reads._snapshot_active
                assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
            assert reads.facts([]) == {} and not reads._snapshot_active
            # A completed source adapter is not an owned selector snapshot.
            active = _active_fact_reads.set(reads)
            try:
                with pytest.raises(KernelError, match="看板生产记录"):
                    with snapshot(engine, connection):
                        pytest.fail("an unowned source reader was promoted")
                assert _active_fact_reads.get() is reads
                assert reads.facts([]) == {} and not reads._snapshot_active
            finally:
                _active_fact_reads.reset(active)
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        connection.rollback()


@pytest.mark.parametrize("end", ["COMMIT", "ROLLBACK"])
def test_transaction_replacement_invalidates_reconstruction(engine, end):
    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        with pytest.raises(KernelError) as failure:
            with snapshot(engine, connection) as snap:
                reads = snap.reads
                connection.execute(end)
                connection.execute("BEGIN")
        assert failure.value.code == "content_integrity_failed"
        assert not reads._snapshot_active and _active_fact_reads.get() is None
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        connection.rollback()


def test_disabling_guard_then_writing_cannot_return_a_certified_reconstruction(engine):
    with engine.store.connection() as connection:
        scratch(connection)
        with pytest.raises(KernelError) as failure:
            with snapshot(engine, connection):
                connection.execute("PRAGMA query_only=OFF")
                connection.execute("INSERT INTO guard_probe VALUES(2)")
                connection.execute("PRAGMA query_only=ON")
        assert failure.value.code == "content_integrity_failed"
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 0
        assert _active_fact_reads.get() is None
        connection.rollback()


def test_failed_guard_restoration_closes_connection(engine):
    class FailedRestore:
        def __init__(self, connection):
            self.connection = connection

        def __getattr__(self, name):
            return getattr(self.connection, name)

        def execute(self, sql, *args):
            if sql == "PRAGMA query_only=0":
                raise sqlite3.OperationalError("synthetic guard restoration failure")
            return self.connection.execute(sql, *args)

    with engine.store.connection() as connection:
        connection.execute("BEGIN")
        with pytest.raises(sqlite3.OperationalError, match="restoration failure"):
            with snapshot(engine, FailedRestore(connection)) as snap:
                reads = snap.reads
        assert not reads._snapshot_active and _active_fact_reads.get() is None
        assert not producing_model()
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            connection.execute("SELECT 1")
