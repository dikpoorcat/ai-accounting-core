"""Resident snapshot transactions preserve epochs and compiled SQL, not results."""

import gc
import sqlite3
import weakref
from pathlib import Path

import pytest

from test_engine import engine as engine_fixture, publish, save
from test_resident_read_guard_work import _widen_file_permission

from ai_accounting.kernel import runtime
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import Read
from ai_accounting.kernel.permissions import PrivatePathError, ensure_private_file
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.resident_reads import READ_PAGE_CACHE_KIB, ResidentReadPool
from ai_accounting.kernel.runtime import RuntimeConfigurationError


engine = engine_fixture


@pytest.fixture
def resident_engine(engine):
    pool = ResidentReadPool()
    engine.store.read_pool = pool
    try:
        yield engine, pool
    finally:
        pool.close()
        engine.store.read_pool = None


def compile_observer(monkeypatch):
    events = []
    authorize = runtime._OwnedReadGuard.authorize

    def observe(guard, action, operation, *rest):
        decision = authorize(guard, action, operation, *rest)
        events.append((action, operation, decision))
        return decision

    monkeypatch.setattr(runtime._OwnedReadGuard, "authorize", observe)
    return events


def test_compiled_programs_survive_new_snapshot_and_cache_is_bounded(
    resident_engine, monkeypatch, record_property
):
    engine, pool = resident_engine
    events = compile_observer(monkeypatch)
    programs = [f"SELECT ? AS value,{number} AS program" for number in range(322)]
    compiled = []
    for value in (17, 29):
        with QueryReads.snapshot(engine) as reads:
            connection = reads.connection
            before = len(events)
            for number, sql in enumerate(programs):
                assert tuple(connection.execute(sql, (value,)).fetchone()) == (value, number)
            compiled.append(len(events) - before)
    assert compiled[0] > 0 and compiled[1] == 0
    record_property("owned_322_program_first_compile_callbacks", compiled[0])
    record_property("owned_322_program_second_compile_callbacks", compiled[1])
    assert pool._total == 1
    assert runtime.OWNED_READ_CACHED_STATEMENTS == 512
    assert connection.execute("PRAGMA cache_size").fetchone()[0] == -READ_PAGE_CACHE_KIB

    # More distinct programs than the explicit budget must evict old programs.
    with QueryReads.snapshot(engine) as reads:
        for number in range(550):
            assert reads.connection.execute(f"SELECT {number} AS bounded_program").fetchone()[0] == number
    with QueryReads.snapshot(engine) as reads:
        before = len(events)
        assert reads.connection.execute("SELECT 549 AS bounded_program").fetchone()[0] == 549
        assert len(events) == before
        assert reads.connection.execute(programs[0], (31,)).fetchone()[0] == 31
        assert len(events) > before
    assert pool._total == 1


def test_generic_cached_transactions_do_not_enter_owned_snapshot(resident_engine):
    engine, pool = resident_engine
    with engine.store.connection(read_only=True) as generic:
        cursor = generic.cursor()
        for _ in range(2):
            cursor.execute("BEGIN")
            cursor.execute("COMMIT")
    with QueryReads.snapshot(engine) as reads:
        assert reads.connection is not generic
        assert type(reads.connection) is runtime._OwnedReadConnection
        for statement in ("BEGIN", "COMMIT", "ROLLBACK"):
            with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                reads.connection.cursor().execute(statement)
            assert reads.connection.in_transaction
    with engine.store.connection(read_only=True) as reused:
        assert reused is generic
        reused.execute("BEGIN")
        reused.commit()
    assert pool._total == 2


def test_generic_and_owned_share_one_pool_budget(resident_engine):
    engine, original_pool = resident_engine
    pool = ResidentReadPool(maximum=1)
    engine.store.read_pool = pool
    try:
        with engine.store.connection(read_only=True) as generic:
            generic.execute("BEGIN")
            generic.execute("COMMIT")
        with QueryReads.snapshot(engine) as reads:
            owned = reads.connection
            assert owned is not generic and pool._total == 1
            with pytest.raises(RuntimeConfigurationError, match="unfinished transaction"):
                with runtime._owned_read_transaction(owned):
                    pytest.fail("Nested private transaction replaced the snapshot")
            assert owned.in_transaction
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            generic.execute("SELECT 1")
        with engine.store.connection(read_only=True) as replacement:
            assert replacement is not owned and pool._total == 1
            replacement.execute("BEGIN")
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            owned.execute("SELECT 1")
    finally:
        pool.close()
        engine.store.read_pool = original_pool


def test_owned_snapshot_rejects_all_public_epoch_replacement_routes(resident_engine):
    engine, _ = resident_engine
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        cursor = connection.cursor()
        actions = [
            *(lambda sql=sql: connection.execute(sql) for sql in
              ("BEGIN", "COMMIT", "END", "ROLLBACK", "SAVEPOINT replacement",
               "RELEASE replacement", "ROLLBACK TO replacement")),
            *(lambda sql=sql: cursor.execute(sql) for sql in ("BEGIN", "COMMIT", "ROLLBACK")),
            connection.commit, connection.rollback,
            lambda: sqlite3.Connection.commit(connection),
            lambda: sqlite3.Connection.rollback(connection),
            lambda: connection.executescript("SELECT 1"),
            lambda: cursor.executescript("SELECT 1"),
            lambda: connection.executescript("COMMIT;BEGIN;SELECT 1"),
            *(lambda value=value: setattr(connection, "autocommit", value)
              for value in (True, False, sqlite3.LEGACY_TRANSACTION_CONTROL)),
            lambda: setattr(connection, "isolation_level", None),
            lambda: connection.set_authorizer(None),
            lambda: connection.set_authorizer(lambda *_: sqlite3.SQLITE_OK),
        ]
        for action in actions:
            for _ in range(2):
                with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                    action()
                assert connection.in_transaction and reads._snapshot_active
                assert connection._owned_read_guard.permit is None
                assert connection.autocommit == sqlite3.LEGACY_TRANSACTION_CONTROL
                assert connection.execute("SELECT 1").fetchone()[0] == 1


def test_bootstrap_cached_transactions_are_expired_when_owned_guard_installs(engine):
    connection = runtime.connect(
        engine.store.path, read_only=True, validator=engine.store.validate_connection,
        _cross_thread=True, _owned_snapshot=True,
    )
    try:
        cursor = connection.cursor()
        cursor.execute("BEGIN")
        cursor.execute("COMMIT")
        runtime._install_owned_read_guard(connection)
        with runtime._owned_read_transaction(connection):
            for statement in ("BEGIN", "COMMIT", "ROLLBACK"):
                with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                    cursor.execute(statement)
                assert connection.in_transaction
    finally:
        connection.close()


def test_private_allowance_is_consumed_before_trace_reentry(resident_engine):
    engine, _ = resident_engine
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
    attempts = []

    def reenter(statement):
        if statement in ("BEGIN", "ROLLBACK"):
            try:
                connection.execute("COMMIT")
            except sqlite3.DatabaseError:
                attempts.append(connection._owned_read_guard.permit)
            else:
                attempts.append("unexpected epoch replacement")

    connection.set_trace_callback(reenter)
    with QueryReads.snapshot(engine) as reads:
        assert reads.connection is connection
        assert reads.connection.in_transaction
    assert attempts and all(permit is None for permit in attempts)


def test_private_allowance_is_consumed_before_progress_reentry(resident_engine):
    engine, _ = resident_engine
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
    attempts = []

    def reenter():
        permit = connection._owned_read_guard.permit
        operation = permit[0] if permit is not None else "COMMIT"
        try:
            connection.execute(operation)
        except sqlite3.DatabaseError as error:
            attempts.append((permit, "not authorized" in str(error)))
        else:
            attempts.append((permit, False))
        return 0

    connection.set_progress_handler(reenter, 1)
    with QueryReads.snapshot(engine) as reads:
        assert reads.connection is connection
        assert reads.connection.in_transaction
    assert attempts and all(permit is None and denied for permit, denied in attempts)


@pytest.mark.parametrize("attribute,value", [
    ("autocommit", True), ("autocommit", False), ("isolation_level", "IMMEDIATE"),
])
def test_native_descriptor_mode_change_is_detected_and_discarded(
    resident_engine, attribute, value
):
    engine, pool = resident_engine
    escaped = None
    with pytest.raises((RuntimeConfigurationError, sqlite3.DatabaseError)):
        with QueryReads.snapshot(engine) as reads:
            escaped = reads
            connection = reads.connection
            reads._report_snapshot_cache["synthetic"] = object()
            descriptor = getattr(sqlite3.Connection, attribute)
            if attribute == "autocommit" and value is True:
                # The native setter changes Python mode before its COMMIT is denied.
                with pytest.raises(sqlite3.DatabaseError, match="not authorized"):
                    descriptor.__set__(connection, value)
            else:
                descriptor.__set__(connection, value)
            assert connection.in_transaction
    assert escaped is not None
    assert pool._total == 0 and connection._owned_read_guard.permit is None
    assert not escaped._snapshot_active and not escaped._report_snapshot_cache
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("SELECT 1")


@pytest.mark.parametrize("phase", ["entry", "partial_entry", "body", "exit"])
def test_failed_owned_scope_discards_connection_and_clears_proofs(
    resident_engine, monkeypatch, phase
):
    engine, pool = resident_engine
    with QueryReads.snapshot(engine) as reads:
        prior = reads.connection
        guard = prior._owned_read_guard
    escaped = None
    original = runtime._owned_read_transaction_operation
    failed = False

    def fail_once(connection, operation):
        nonlocal failed
        if connection is prior and not failed:
            if phase == "entry" and operation == "BEGIN":
                failed = True
                raise RuntimeError("synthetic entry failure")
            if phase == "partial_entry" and operation == "BEGIN":
                failed = True
                original(connection, operation)
                raise RuntimeError("synthetic partial entry failure")
            if phase == "exit" and operation == "ROLLBACK":
                # The borrow validator also has its own private transaction;
                # fail only the business scope after it was yielded below.
                if escaped is not None:
                    failed = True
                    raise RuntimeError("synthetic exit failure")
        return original(connection, operation)

    monkeypatch.setattr(runtime, "_owned_read_transaction_operation", fail_once)
    with pytest.raises(RuntimeError, match="synthetic"):
        with QueryReads.snapshot(engine) as reads:
            escaped = reads
            reads._report_snapshot_cache["synthetic"] = object()
            if phase == "body":
                raise RuntimeError("synthetic body failure")
    assert pool._total == 0 and guard.permit is None
    if escaped is not None:
        assert not escaped._snapshot_active and not escaped._report_snapshot_cache
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        prior.execute("SELECT 1")
    with QueryReads.snapshot(engine) as fresh:
        assert fresh.connection is not prior
        assert fresh.connection.execute("SELECT 1").fetchone()[0] == 1


def test_new_owned_snapshot_sees_new_fact_and_publication(resident_engine, monkeypatch):
    engine, _ = resident_engine
    save(engine)
    publish(engine)
    events = compile_observer(monkeypatch)
    sql = "SELECT fact_id FROM fact_current WHERE subject_id=?"
    observations = []
    for expected in (100, 250):
        with QueryReads.snapshot(engine) as reads:
            connection = reads.connection
            before = len(events)
            fact_id = connection.execute(sql, ("charge",)).fetchone()[0]
            compiled = len(events) - before
            facts = reads.select(Read("fact", "test_charge", "2026-01"))
            selected = BusinessQueries(engine, reads=reads)._selected_accounting(
                connection, "charge", "2026-01"
            )
            assert facts[0].fact.amount == expected
            assert selected["period_events"][0]["lines"][0]["debit"] == expected
            observations.append((connection, fact_id, compiled))
        if expected == 100:
            save(engine, amount=250, revision=1, request="new-owned-fact")
            publish(engine, request="new-owned-publication")
    assert observations[0][0] is observations[1][0]
    assert observations[0][1] != observations[1][1]
    assert observations[0][2] > 0 and observations[1][2] == 0


def test_fixed_v1_and_unpooled_reads_keep_generic_transaction_path(resident_engine):
    engine, pool = resident_engine
    with QueryReads.snapshot(engine) as current:
        assert type(current.connection) is runtime._OwnedReadConnection
    with historical_content(1), QueryReads.snapshot(engine) as historic:
        assert type(historic.connection) is runtime._PrivateConnection
        assert historic.connection.execute("SELECT 1").fetchone()[0] == 1
    assert pool._total == 2
    engine.store.read_pool = None
    with QueryReads.snapshot(engine) as ordinary:
        assert type(ordinary.connection) is runtime._PrivateConnection
        assert ordinary.connection.execute("SELECT 1").fetchone()[0] == 1


def test_owned_guard_retains_no_business_objects_and_pool_resources_close(resident_engine):
    engine, pool = resident_engine
    with QueryReads.snapshot(engine) as reads:
        connection = reads.connection
        guard = connection._owned_read_guard
        read_ref, connection_ref = weakref.ref(reads), weakref.ref(connection)
        reads._report_snapshot_cache["synthetic_business_object"] = object()
    del reads
    assert read_ref() is None
    assert all(item is not engine and item is not engine.store and item is not connection
               for item in gc.get_referents(guard))
    for _ in range(6):
        with QueryReads.snapshot(engine) as reads:
            assert reads.connection is connection
    del reads
    assert pool._total == 1
    pool.close()
    assert pool._total == 0 and not pool._idle
    del connection
    assert connection_ref() is None


def test_owned_revalidation_still_rejects_structure_and_identity_drift(resident_engine):
    engine, pool = resident_engine
    with QueryReads.snapshot(engine) as reads:
        prior = reads.connection
    with sqlite3.connect(engine.store.path) as writer:
        writer.execute("CREATE TABLE synthetic_owned_drift(id INTEGER)")
    with pytest.raises(Exception, match="schema|结构|contract|fingerprint"):
        with QueryReads.snapshot(engine):
            pytest.fail("Owned schema drift reached business reads")
    assert pool._total == 0
    with sqlite3.connect(engine.store.path) as writer:
        writer.execute("DROP TABLE synthetic_owned_drift")
    with QueryReads.snapshot(engine) as reads:
        assert reads.connection is not prior
    engine.store.taxpayer_id = "91310000123456789B"
    with pytest.raises(Exception, match="database does not match"):
        with QueryReads.snapshot(engine):
            pytest.fail("Owned identity drift reached business reads")
    assert pool._total == 0


def test_owned_revalidation_rejects_changed_sqlite_configuration(resident_engine):
    engine, pool = resident_engine
    with QueryReads.snapshot(engine) as reads:
        prior = reads.connection
    prior.setconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE, False)
    with pytest.raises(RuntimeConfigurationError, match="safety settings"):
        with QueryReads.snapshot(engine):
            pytest.fail("Owned unsafe configuration reached business reads")
    assert pool._total == 0


@pytest.mark.parametrize("phase", ["before", "after", "sidecar"])
def test_owned_borrow_still_checks_real_private_permissions(
    resident_engine, monkeypatch, phase
):
    engine, pool = resident_engine
    with QueryReads.snapshot(engine):
        pass
    target = Path(str(engine.store.path) + "-wal") if phase == "sidecar" else engine.store.path
    assert target.is_file()
    if phase == "after":
        validate = engine.store.validate_connection

        def widen_after_validation(connection):
            validate(connection)
            _widen_file_permission(target)

        monkeypatch.setattr(engine.store, "validate_connection", widen_after_validation)
    else:
        _widen_file_permission(target)
    try:
        with pytest.raises(PrivatePathError):
            with QueryReads.snapshot(engine):
                pytest.fail("Owned unsafe private path reached business reads")
        assert pool._total == 0
    finally:
        ensure_private_file(target)
