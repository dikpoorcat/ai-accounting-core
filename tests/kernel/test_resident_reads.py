"""Resident-only read connections remain bounded and validate every borrow."""

import os
import shutil
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from threading import Event

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.permissions import PrivatePathError, ensure_private_file
from ai_accounting.kernel.resident_reads import ResidentReadPool
from ai_accounting.kernel.service import LocalService


@pytest.fixture
def resident(tmp_path):
    service = LocalService(tmp_path / "synthetic", enable_read_pool=True)
    first = service.catalog.create_company("91310000123456789A", "合成甲")
    second = service.catalog.create_company("91310000123456789B", "合成乙")
    try:
        yield service, first, second
    finally:
        service.close()


def test_service_dashboard_pool_reuses_connection_and_sees_new_commits(resident):
    service, first, second = resident
    store = service.engine(first["id"], dashboard_read=True).store
    assert service.read_pool._total == 0  # catalog bind did not open another company file
    with store.connection(read_only=True) as connection:
        old_connection = connection
        connection.execute("BEGIN")
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 0
    Entities(service.engine(first["id"])).register_entity(
        "person", {}, source="synthetic", request_id="new-person"
    )
    with store.connection(read_only=True) as connection:
        assert connection is old_connection
        connection.execute("BEGIN")
        assert connection.execute("SELECT count(*) FROM entity").fetchone()[0] == 1
    assert service.engine(first["id"]).store.read_pool is None
    assert service.read_pool._total == 1

    other = service.engine(second["id"], dashboard_read=True).store
    with other.connection(read_only=True) as connection:
        assert connection is not old_connection
        assert connection.execute("SELECT taxpayer_id FROM identity").fetchone()[0] == second[
            "taxpayer_id"
        ]
    assert service.read_pool._total == 2


def test_pool_reuses_across_threads_and_evicts_other_company(resident):
    service, first, second = resident
    pool = ResidentReadPool(maximum=1, wait_seconds=2)
    first_store = service.catalog.bind(first["id"], read_pool=pool)
    second_store = service.catalog.bind(second["id"], read_pool=pool)
    holding, release = Event(), Event()

    def worker():
        with first_store.connection(read_only=True) as connection:
            holding.set()
            assert release.wait(2)
            return connection

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            first_future = executor.submit(worker)
            assert holding.wait(2)
            waiting = executor.submit(lambda: _read_identity(first_store))
            release.set()
            old = first_future.result()
            assert waiting.result() == first["taxpayer_id"]
        with first_store.connection(read_only=True) as connection:
            assert connection is old
        with second_store.connection(read_only=True) as connection:
            assert connection is not old
            assert connection.execute("SELECT taxpayer_id FROM identity").fetchone()[0] == second[
                "taxpayer_id"
            ]
        assert pool._total == 1
    finally:
        pool.close()


def test_pool_wait_is_bounded_when_all_connections_are_busy(resident):
    service, first, _ = resident
    pool = ResidentReadPool(maximum=1, wait_seconds=0.05)
    store = service.catalog.bind(first["id"], read_pool=pool)
    try:
        with store.connection(read_only=True):
            with ThreadPoolExecutor(max_workers=1) as executor:
                blocked = executor.submit(_read_identity, store)
                with pytest.raises(KernelError, match="只读连接均在使用"):
                    blocked.result()
        assert _read_identity(store) == first["taxpayer_id"]
        assert pool._total == 1
    finally:
        pool.close()


def _read_identity(store):
    with store.connection(read_only=True) as connection:
        return connection.execute("SELECT taxpayer_id FROM identity").fetchone()[0]


def test_pool_discards_failed_transaction_and_changed_sqlite_flags(resident):
    service, first, _ = resident
    store = service.engine(first["id"], dashboard_read=True).store
    with pytest.raises(RuntimeError, match="synthetic"):
        with store.connection(read_only=True) as connection:
            broken = connection
            connection.execute("BEGIN")
            raise RuntimeError("synthetic")
    assert service.read_pool._total == 0
    with store.connection(read_only=True) as connection:
        assert connection is not broken
        safe = connection
    safe.execute("PRAGMA foreign_keys=OFF")
    with pytest.raises(Exception, match="foreign_keys"):
        with store.connection(read_only=True):
            pass
    assert service.read_pool._total == 0
    assert _read_identity(store) == first["taxpayer_id"]


def test_pool_clears_read_instrumentation_before_reuse(resident):
    service, first, _ = resident
    store = service.engine(first["id"], dashboard_read=True).store
    traces, progress = [], []

    with store.connection(read_only=True) as connection:
        original = connection
        connection.set_trace_callback(traces.append)
        connection.set_progress_handler(lambda: progress.append(True) or 0, 1)
        connection.execute("SELECT taxpayer_id FROM identity").fetchone()
    assert traces and progress
    trace_count, progress_count = len(traces), len(progress)

    with store.connection(read_only=True) as connection:
        assert connection is original
        connection.execute("SELECT taxpayer_id FROM identity").fetchone()
    assert len(traces) == trace_count
    assert len(progress) == progress_count


def test_pool_rechecks_file_identity_structure_permissions_and_taxpayer(
    resident, monkeypatch
):
    from ai_accounting.kernel import resident_reads

    service, first, _ = resident
    store = service.engine(first["id"], dashboard_read=True).store
    with store.connection(read_only=True) as connection:
        old = connection
    replacement = store.path.with_name("replacement.sqlite")
    shutil.copy2(store.path, replacement)
    ensure_private_file(replacement)
    # Windows prevents replacement while SQLite owns the file. Simulate a
    # handle lost outside the pool, then verify the retained slot is rejected.
    old.close()
    os.replace(replacement, store.path)
    with store.connection(read_only=True) as connection:
        assert connection is not old

    with sqlite3.connect(store.path) as connection:
        connection.execute("CREATE TABLE synthetic_drift(x INTEGER)")
    with pytest.raises(Exception, match="schema|结构|contract|fingerprint"):
        with store.connection(read_only=True):
            pass
    assert service.read_pool._total == 0
    with sqlite3.connect(store.path) as connection:
        connection.execute("DROP TABLE synthetic_drift")

    def unsafe(_path):
        raise PrivatePathError("synthetic unsafe permissions")

    monkeypatch.setattr(resident_reads, "assert_private_file", unsafe)
    with pytest.raises(PrivatePathError, match="unsafe permissions"):
        with store.connection(read_only=True):
            pass
    monkeypatch.undo()

    with service.catalog.connection() as connection:
        connection.execute(
            "UPDATE company SET taxpayer_id=? WHERE id=?",
            ("91310000123456789C", first["id"]),
        )
    rebound = service.engine(first["id"], dashboard_read=True).store
    with pytest.raises(KernelError, match="database does not match"):
        with rebound.connection(read_only=True):
            pass
