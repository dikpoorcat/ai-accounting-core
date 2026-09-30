"""A split brief is accepted only if its company database did not change."""

import sqlite3
from contextlib import contextmanager

import pytest

from ai_accounting.kernel.brief_parallel import BriefReadGuard
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.resident_reads import ResidentReadPool


@pytest.fixture
def company_store(tmp_path):
    catalog = Catalog(tmp_path / "synthetic-parallel-guard")
    company = catalog.create_company("91310000123456789A", "合成只读守卫企业")
    return catalog.bind(company["id"])


@pytest.fixture
def pooled_store(company_store):
    pool = ResidentReadPool(maximum=1)
    company_store.read_pool = pool
    try:
        yield company_store, pool
    finally:
        pool.close()


def _change_state(store, field, *, commit):
    assert field in {"accounting", "read_repair_revision"}
    with store.connection() as writer:
        writer.execute("BEGIN IMMEDIATE")
        writer.execute(f"UPDATE state SET {field}={field}+1 WHERE id=1")
        if commit:
            writer.commit()
        else:
            writer.rollback()


def test_guard_detects_another_connection_commit_without_pin_transaction(company_store):
    with BriefReadGuard(company_store) as guard:
        assert not guard.connection.in_transaction
        assert guard.unchanged()
        _change_state(company_store, "accounting", commit=True)
        assert not guard.connection.in_transaction
        assert not guard.unchanged()


def test_guard_accepts_rolled_back_other_connection_write(company_store):
    with BriefReadGuard(company_store) as guard:
        _change_state(company_store, "accounting", commit=False)
        assert guard.unchanged()
        assert not guard.connection.in_transaction


def test_guard_closes_every_data_version_cursor(company_store, monkeypatch):
    opened, closed = [], []
    connection_method = company_store.connection

    class TrackedCursor:
        def __init__(self, cursor):
            self.cursor = cursor
            opened.append(self)

        def fetchone(self):
            return self.cursor.fetchone()

        def close(self):
            closed.append(self)
            self.cursor.close()

    class TrackedConnection:
        def __init__(self, connection):
            self.connection = connection

        def execute(self, sql, *args):
            cursor = self.connection.execute(sql, *args)
            return TrackedCursor(cursor) if sql == "PRAGMA data_version" else cursor

        def __getattr__(self, name):
            return getattr(self.connection, name)

    @contextmanager
    def tracked_connection(*args, **kwargs):
        with connection_method(*args, **kwargs) as connection:
            yield TrackedConnection(connection)

    monkeypatch.setattr(company_store, "connection", tracked_connection)
    with BriefReadGuard(company_store) as guard:
        assert guard.unchanged()
        assert not guard.connection.in_transaction
        assert len(opened) == len(closed) == 2


@pytest.mark.parametrize("field", ["accounting", "read_repair_revision"])
def test_guard_rejects_business_or_read_repair_version_change(company_store, field):
    with BriefReadGuard(company_store) as guard:
        _change_state(company_store, field, commit=True)
        assert not guard.unchanged()


def test_guard_rejects_its_own_open_transaction(company_store):
    with BriefReadGuard(company_store) as guard:
        guard.connection.execute("BEGIN")
        try:
            assert not guard.unchanged()
        finally:
            guard.connection.rollback()


def test_guard_discards_a_failed_final_version_read(company_store, monkeypatch):
    with BriefReadGuard(company_store) as guard:

        def failed_pragma():
            raise sqlite3.OperationalError("synthetic guard read failure")

        monkeypatch.setattr(guard, "_version", failed_pragma)
        assert not guard.unchanged()


def test_guard_rejects_worker_or_guard_file_identity_change(company_store, monkeypatch):
    from ai_accounting.kernel import brief_parallel

    with BriefReadGuard(company_store) as guard:
        identity = guard.file_identity
        assert guard.worker_matches({"identity_before": identity, "identity_after": identity})
        changed = (identity[0], identity[1] + 1, identity[2])
        assert not guard.worker_matches({"identity_before": identity, "identity_after": changed})
        assert not guard.worker_matches({"identity_before": changed, "identity_after": identity})
        monkeypatch.setattr(brief_parallel, "_file_identity", lambda _path: changed)
        assert not guard.unchanged()


def test_pooled_guard_reuses_connection_and_observes_later_commit(pooled_store):
    store, pool = pooled_store
    with BriefReadGuard(store) as first:
        connection = first.connection
        assert pool._total == 1 and not pool._idle
        assert first.unchanged()
    assert first.connection is None
    assert len(pool._idle) == 1
    _change_state(store, "accounting", commit=True)
    with BriefReadGuard(store) as second:
        assert second.connection is connection
        assert not connection.in_transaction
        assert second.unchanged()
        _change_state(store, "accounting", commit=True)
        assert not second.unchanged()
    assert len(pool._idle) == 1


def test_pooled_guard_discards_pinned_transaction_on_exit(pooled_store):
    store, pool = pooled_store
    with pytest.raises(RuntimeError, match="autocommit"):
        with BriefReadGuard(store) as guard:
            pinned = guard.connection
            pinned.execute("BEGIN")
            assert not guard.unchanged()
    assert guard.connection is None
    assert pool._total == 0 and not pool._idle
    with BriefReadGuard(store) as replacement:
        assert replacement.connection is not pinned
        assert replacement.unchanged()


def test_pooled_guard_forwards_failure_and_clears_entry_state(pooled_store, monkeypatch):
    store, pool = pooled_store
    guard = BriefReadGuard(store)

    def failed_version():
        raise ValueError("version failed")

    with monkeypatch.context() as patch:
        patch.setattr(guard, "_version", failed_version)
        with pytest.raises(ValueError, match="version failed"):
            with guard:
                pass
    assert guard.connection is None
    assert guard._connection_context is None
    assert guard.file_identity is None and guard.data_version is None
    assert pool._total == 0 and not pool._idle

    with pytest.raises(RuntimeError, match="body failed"):
        with BriefReadGuard(store) as guard:
            raise RuntimeError("body failed")
    assert guard.connection is None
    assert pool._total == 0 and not pool._idle


def test_pooled_guard_rejects_file_identity_change(pooled_store, monkeypatch):
    from ai_accounting.kernel import brief_parallel

    store, _ = pooled_store
    with BriefReadGuard(store) as guard:
        original = guard.file_identity
        monkeypatch.setattr(
            brief_parallel, "_file_identity",
            lambda _path: (original[0], original[1] + 1, original[2]),
        )
        assert not guard.unchanged()


def test_unpooled_guard_closes_connection(company_store):
    with BriefReadGuard(company_store) as guard:
        connection = guard.connection
        assert guard.unchanged()
    assert guard.connection is None
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        connection.execute("PRAGMA data_version")
