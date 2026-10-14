"""Real SQLite writes exercise migration ownership and cleanup failure precedence."""

import sqlite3
from contextlib import closing

import pytest
from test_remuneration_development_upgrade import create_archived_company

from ai_accounting.kernel import offline_development_upgrade as development
from ai_accounting.kernel.catalog import ensure_catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.development_contracts import (
    HISTORY_PRIOR_FINGERPRINT,
    HISTORY_TARGET_FINGERPRINT,
    TAX_POINT_TARGET_FINGERPRINT,
)
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.offline_development_upgrade import _root_upgrade_failure
from ai_accounting.kernel.permissions import ensure_private_directory
from ai_accounting.kernel.runtime import (
    connect,
    migration_connection,
    migration_write,
    verification_snapshot,
)
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import verify_schema


class BrokenConnection:
    def __init__(self, connection, *, begin=False, commit=False, rollback=False,
                 restore=False, close=False, committed_error=False,
                 ignore_off=False, ignore_on=False):
        self.connection = connection
        self.fail_begin, self.fail_commit, self.fail_rollback = begin, commit, rollback
        self.fail_restore, self.fail_close = restore, close
        self.committed_error = committed_error
        self.ignore_off, self.ignore_on = ignore_off, ignore_on
        self.close_calls = 0

    @property
    def in_transaction(self):
        return self.connection.in_transaction

    def execute(self, sql, *args):
        if (sql == "PRAGMA foreign_keys=OFF" and self.ignore_off) or (
            sql == "PRAGMA foreign_keys=ON" and self.ignore_on
        ):
            return self.connection.execute("SELECT 1")
        if sql == "BEGIN IMMEDIATE" and self.fail_begin:
            raise sqlite3.OperationalError("begin injection")
        if sql == "PRAGMA foreign_keys=ON" and self.fail_restore:
            raise sqlite3.OperationalError("restore injection")
        return self.connection.execute(sql, *args)

    def commit(self):
        if self.fail_commit:
            raise sqlite3.OperationalError("commit injection")
        self.connection.commit()
        if self.committed_error:
            raise sqlite3.OperationalError("commit outcome unavailable")

    def rollback(self):
        if self.fail_rollback:
            raise sqlite3.OperationalError("rollback injection")
        self.connection.rollback()

    def close(self):
        self.close_calls += 1
        self.connection.close()
        if self.fail_close:
            raise sqlite3.OperationalError("close injection")


@pytest.fixture
def database(tmp_path):
    path = tmp_path / "migration.sqlite"
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute("PRAGMA foreign_keys=ON")
    connection.execute("CREATE TABLE retained(id INTEGER PRIMARY KEY, amount INTEGER NOT NULL)")
    connection.execute("INSERT INTO retained VALUES(1,123)")
    yield path, connection
    connection.close()


def contents(path):
    with sqlite3.connect(path) as connection:
        return (connection.execute("SELECT * FROM retained").fetchall(),
                connection.execute("SELECT name FROM sqlite_schema WHERE name='added'").fetchall())


def change(connection):
    assert connection.in_transaction
    connection.execute("CREATE TABLE added(id INTEGER PRIMARY KEY)")
    connection.execute("INSERT INTO added VALUES(7)")


def test_success_keeps_checks_in_owned_write_before_commit(database):
    path, connection = database
    with migration_write(connection, foreign_keys_off=True) as state:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
        change(connection)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT * FROM retained").fetchall() == [(1, 123)]
    assert state["committed"] and state["outcome"] == "committed"
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert contents(path) == ([(1, 123)], [("added",)])


def test_existing_transaction_is_not_touched(database):
    _, connection = database
    connection.execute("BEGIN")
    connection.execute("INSERT INTO retained VALUES(2,456)")
    with pytest.raises(KernelError) as caught:
        with migration_write(connection, foreign_keys_off=True):
            pytest.fail("must not enter")
    assert caught.value.code == "migration_transaction_active"
    assert connection.in_transaction
    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert connection.execute("SELECT count(*) FROM retained").fetchone()[0] == 2
    connection.rollback()


@pytest.mark.parametrize("ignore_off", [True, False])
def test_foreign_key_switch_must_be_confirmed(database, ignore_off):
    path, raw = database
    connection = BrokenConnection(raw, ignore_off=ignore_off, ignore_on=not ignore_off)
    with pytest.raises(KernelError) as caught:
        with migration_write(connection, foreign_keys_off=True):
            change(connection)
    assert caught.value.code == "migration_integrity_failed"
    assert caught.value.migration_state["committed"] is (not ignore_off)
    assert contents(path)[1] == ([] if ignore_off else [("added",)])


def test_source_recheck_holds_write_lock_and_does_not_adopt_new_baseline(database):
    path, raw = database
    baseline = raw.execute("SELECT amount FROM retained WHERE id=1").fetchone()[0]
    writer = sqlite3.connect(path, isolation_level=None, timeout=0)

    def before_begin():
        writer.execute("UPDATE retained SET amount=124 WHERE id=1")

    try:
        with pytest.raises(KernelError, match="source changed"):
            with migration_write(raw, before_begin=before_begin):
                with pytest.raises(sqlite3.OperationalError, match="locked"):
                    writer.execute("UPDATE retained SET amount=125 WHERE id=1")
                if raw.execute("SELECT amount FROM retained WHERE id=1").fetchone()[0] != baseline:
                    raise KernelError("source_changed", "source changed")
                change(raw)
    finally:
        writer.close()
    assert contents(path) == ([(1, 124)], [])


@pytest.mark.parametrize("phase", ["before_begin", "begin", "body", "commit"])
def test_precommit_failures_preserve_rows_and_schema(database, phase):
    path, raw = database
    connection = BrokenConnection(raw, begin=phase == "begin", commit=phase == "commit")
    error = RuntimeError("domain rejection")

    def before_begin():
        if phase == "before_begin":
            raise error

    with pytest.raises((RuntimeError, sqlite3.OperationalError)) as caught:
        with migration_write(connection, foreign_keys_off=True, before_begin=before_begin):
            change(connection)
            if phase == "body":
                raise error
    assert caught.value.migration_state["committed"] is False
    assert caught.value.migration_state["outcome"] == (
        "not_started" if phase in {"before_begin", "begin"} else "rolled_back"
    )
    assert caught.value.migration_state["failure_phase"] == phase
    assert raw.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert contents(path) == ([(1, 123)], [])


def test_cleanup_never_replaces_original_and_discards_connection(database):
    path, raw = database
    connection = BrokenConnection(raw, rollback=True, restore=True, close=True)
    error = RuntimeError("original domain error")
    with pytest.raises(RuntimeError) as caught:
        with migration_connection(connection):
            with migration_write(connection, foreign_keys_off=True):
                change(connection)
                raise error
    assert caught.value is error
    assert error.migration_state["outcome"] == "unknown"
    assert error.migration_state["connection_discarded"] is True
    assert [item["phase"] for item in error.migration_state["cleanup_errors"]] == [
        "rollback", "foreign_keys_restore", "close",
    ]
    assert connection.close_calls == 1  # root scope never retries discarded cleanup
    with pytest.raises(sqlite3.ProgrammingError):
        raw.execute("SELECT 1")
    assert contents(path) == ([(1, 123)], [])


def test_restore_failure_after_commit_is_explicit_and_keeps_result(database):
    path, raw = database
    connection = BrokenConnection(raw, restore=True)
    result = {"status": "upgraded", "target_fingerprint": "verified-target"}
    with pytest.raises(sqlite3.OperationalError) as caught:
        with migration_write(connection, foreign_keys_off=True) as transaction:
            change(connection)
            transaction["result"] = result
    assert caught.value.migration_state["committed"] is True
    assert caught.value.migration_state["failure_phase"] == "foreign_keys_restore"
    assert caught.value.migration_state["connection_discarded"] is True
    assert caught.value.migration_result == result
    assert contents(path) == ([(1, 123)], [("added",)])
    failure = _root_upgrade_failure(
        caught.value, backup=path.parent, catalog_result="upgraded", results=[],
        row={"id": "current"}, backups={"current": "verified-backup"},
    )
    assert failure.code == "development_upgrade_partial"
    assert failure.details["completed_companies"][0]["status"] == "committed_cleanup_failed"
    assert "不能视作回滚" in str(failure)


def test_commit_error_with_finished_transaction_is_unknown_not_rolled_back(database):
    path, raw = database
    with pytest.raises(sqlite3.OperationalError) as caught:
        with migration_write(BrokenConnection(raw, committed_error=True)):
            change(raw)
    assert caught.value.migration_state["outcome"] == "unknown"
    assert caught.value.migration_state["committed"] is False
    assert caught.value.migration_state["connection_discarded"] is True
    assert contents(path) == ([(1, 123)], [("added",)])


def test_root_close_cannot_replace_domain_error(database):
    _, raw = database
    error = RuntimeError("domain error")
    with pytest.raises(RuntimeError) as caught:
        with migration_connection(BrokenConnection(raw, close=True)):
            raise error
    assert caught.value is error
    assert error.migration_connection_close_error == "OperationalError"


def test_source_snapshot_cleanup_preserves_source_error(database):
    path, raw = database
    connection = BrokenConnection(raw, rollback=True, close=True)
    error = RuntimeError("source verification failed")
    with pytest.raises(RuntimeError) as caught:
        with migration_connection(connection):
            with verification_snapshot(connection):
                raise error
    assert caught.value is error
    assert error.verification_connection_discarded is True
    assert [item["phase"] for item in error.verification_snapshot_cleanup_errors] == [
        "rollback", "close",
    ]
    assert connection.close_calls == 1
    assert contents(path) == ([(1, 123)], [])


def test_verification_does_not_rollback_callers_transaction(database):
    _, raw = database
    raw.execute("BEGIN")
    raw.execute("INSERT INTO retained VALUES(2,456)")
    with pytest.raises(RuntimeError):
        with verification_snapshot(raw):
            raise RuntimeError("owned elsewhere")
    assert raw.in_transaction
    assert raw.execute("SELECT count(*) FROM retained").fetchone()[0] == 2
    raw.rollback()


def test_owned_verification_rejects_unfinished_rollback(database):
    path, raw = database

    class NoRollback(BrokenConnection):
        def rollback(self):
            pass

    connection = NoRollback(raw)
    with pytest.raises(RuntimeError, match="rollback left an active transaction") as caught:
        with verification_snapshot(connection):
            raw.execute("INSERT INTO retained VALUES(2,456)")
    assert caught.value.verification_connection_discarded
    assert connection.close_calls == 1
    assert contents(path) == ([(1, 123)], [])


def test_root_reports_catalog_and_company_prefix_without_cross_database_rollback(tmp_path):
    error = RuntimeError("second company failed")
    error.migration_state = {"outcome": "rolled_back", "committed": False}
    previous = [{"company_id": "first", "status": "upgraded"}]
    failure = _root_upgrade_failure(
        error, backup=tmp_path, catalog_result="upgraded", results=previous,
        row={"id": "second"},
    )
    assert failure.code == "development_upgrade_partial"
    assert failure.details["completed_companies"] == previous
    assert failure.details["catalog"] == "upgraded"
    assert failure.details["migration_state"]["outcome"] == "rolled_back"
    assert "分别提交" in str(failure)


def test_catalog_postcommit_failure_is_reported_as_committed(tmp_path):
    error = RuntimeError("catalog cleanup failed")
    error.migration_state = {"outcome": "committed", "committed": True}
    failure = _root_upgrade_failure(error, backup=tmp_path, catalog_result=None, results=[])
    assert failure.code == "development_upgrade_partial"
    assert failure.details["catalog"] == "committed_cleanup_failed"
    assert failure.details["completed_companies"] == []


def test_verified_skip_is_not_reported_as_committed_upgrade(tmp_path):
    error = RuntimeError("close failed after verified skip")
    failure = _root_upgrade_failure(
        error, backup=tmp_path, catalog_result="verified_skip",
        results=[{"company_id": "unchanged", "status": "verified_skip"}],
        row={"id": "unchanged"},
    )
    assert failure.code == "development_upgrade_failed"
    assert "当前数据库已提交" not in str(failure)
    assert failure.details["completed_companies"][0]["status"] == "verified_skip"


def test_real_root_retains_first_commit_rolls_back_second_and_retries_without_reapplying(tmp_path):
    target = production_bundle()
    source = development.source_bundle(target, HISTORY_PRIOR_FINGERPRINT)
    root = ensure_private_directory(tmp_path / "two-company-upgrade")
    ensure_catalog(root)
    companies = []
    originals = {}
    expected_transitions = [
        (HISTORY_PRIOR_FINGERPRINT, HISTORY_TARGET_FINGERPRINT),
        (HISTORY_TARGET_FINGERPRINT, TAX_POINT_TARGET_FINGERPRINT),
    ]
    for number in (1, 2):
        company_id, database_id = f"synthetic-co-{number}", f"synthetic-db-{number}"
        taxpayer_id = f"91310000123456789{number}"
        path = ensure_private_directory(root / taxpayer_id) / "company.sqlite"
        create_archived_company(
            path, source, taxpayer_id=taxpayer_id,
            company_id=company_id, database_id=database_id,
        )
        engine = Engine(Store(path, source, company_id, database_id))
        engine.register_evidence(
            f"synthetic original evidence {number}".encode(), "text/plain", "original.txt",
            request_id="original-evidence",
        )
        with closing(connect(root / "catalog.sqlite")) as catalog:
            catalog.execute(
                "INSERT INTO company(id,taxpayer_id,name,path,database_id) VALUES(?,?,?,?,?)",
                (company_id, taxpayer_id, f"synthetic {number}", str(path), database_id),
            )
        with closing(connect(path, read_only=True)) as connection:
            originals[company_id] = tuple(connection.iterdump())
        companies.append((company_id, path))

    commits = []

    def fail_second_company(point):
        if point == "before_commit":
            commits.append(point)
            if len(commits) == 2:
                raise RuntimeError("second company before commit")

    with pytest.raises(KernelError) as failed:
        development.upgrade_root(root, bundle=target, fault=fail_second_company)
    first_id, first_path = companies[0]
    second_id, second_path = companies[1]
    details = failed.value.details
    assert failed.value.code == "development_upgrade_partial"
    assert [(item["company_id"], item["status"]) for item in details["completed_companies"]] == [
        (first_id, "upgraded"),
    ]
    assert details["failed_company_id"] == second_id
    assert details["catalog"] == "verified_skip"
    assert details["migration_state"]["outcome"] == "rolled_back"
    assert details["migration_state"]["committed"] is False
    with closing(connect(first_path, read_only=True)) as connection:
        verify_schema(connection, bundle=target)
        first_committed = tuple(connection.iterdump())
        assert first_committed != originals[first_id]
        assert [tuple(value.hex() for value in row) for row in connection.execute(
            "SELECT source_fingerprint,target_fingerprint FROM schema_draft_history "
            "ORDER BY sequence"
        )] == expected_transitions
        assert connection.execute("SELECT name FROM evidence").fetchone()[0] == "original.txt"
    with closing(connect(second_path, read_only=True)) as connection:
        verify_schema(connection, bundle=source)
        assert tuple(connection.iterdump()) == originals[second_id]

    retry = development.upgrade_root(root, bundle=target)
    assert [(item["company_id"], item["status"]) for item in retry["companies"]] == [
        (first_id, "verified_skip"), (second_id, "upgraded"),
    ]
    with closing(connect(first_path, read_only=True)) as connection:
        assert tuple(connection.iterdump()) == first_committed
    with closing(connect(second_path, read_only=True)) as connection:
        verify_schema(connection, bundle=target)
        assert [tuple(value.hex() for value in row) for row in connection.execute(
            "SELECT source_fingerprint,target_fingerprint FROM schema_draft_history "
            "ORDER BY sequence"
        )] == expected_transitions
        assert connection.execute("SELECT name FROM evidence").fetchone()[0] == "original.txt"
