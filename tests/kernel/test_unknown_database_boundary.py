"""Unknown or swapped files are rejected before changing persistent SQLite state."""

import hashlib
import sqlite3
from contextlib import closing
from functools import partial

import pytest

from ai_accounting.kernel import runtime
from ai_accounting.kernel.backup import run_backup_jobs
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.security import IdentityError, SecurityService
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


def unknown_database(path):
    with closing(sqlite3.connect(path)) as connection:
        connection.execute("CREATE TABLE unrelated_original(value INTEGER)")
        connection.execute("INSERT INTO unrelated_original VALUES(123)")
        connection.commit()
    return path


def delete_journal(path):
    with closing(sqlite3.connect(path)) as connection:
        assert connection.execute("PRAGMA journal_mode=DELETE").fetchone()[0] == "delete"


def file_hash(path):
    return hashlib.sha256(path.read_bytes()).digest()


def assert_untouched(path, expected_hash):
    assert file_hash(path) == expected_hash
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    for suffix in ("-wal", "-shm", "-journal"):
        assert not path.with_name(path.name + suffix).exists()


def open_only(factory):
    with factory():
        pass


@pytest.mark.parametrize(
    "entry", ["catalog_constructor", "catalog_connection", "store", "security", "backup_worker"]
)
def test_production_writers_reject_unknown_database_without_changing_it(tmp_path, entry):
    registry = default_registry()
    if entry in {"catalog_connection", "security"}:
        catalog = Catalog(tmp_path, registry)
        security = SecurityService(catalog.path) if entry == "security" else None
        path = catalog.path
        delete_journal(path)
        replacement = unknown_database(tmp_path / "replacement.sqlite")
        replacement.replace(path)
        invoke = (
            partial(security.authorize, "synthetic-invalid-token")
            if security is not None
            else partial(open_only, catalog.connection)
        )
    else:
        path = unknown_database(
            tmp_path / ("catalog.sqlite" if entry == "catalog_constructor" else "company.sqlite")
        )
        if entry == "catalog_constructor":
            invoke = partial(Catalog, tmp_path, registry)
        elif entry == "store":
            store = Store(path, registry, "company", "database")
            invoke = partial(open_only, store.connection)
        else:
            invoke = partial(run_backup_jobs, path)
    before = file_hash(path)
    with pytest.raises(KernelError) as error:
        invoke()
    assert error.value.code in {"schema_fingerprint_mismatch", "schema_version_unsupported"}
    assert_untouched(path, before)
    with closing(sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)) as connection:
        assert connection.execute("SELECT value FROM unrelated_original").fetchone()[0] == 123


@pytest.mark.parametrize("wrong_field", ["company_id", "database_id"])
def test_valid_schema_wrong_company_identity_is_not_reconfigured(tmp_path, wrong_field):
    registry = default_registry()
    actual = Store.create(tmp_path / "company.sqlite", registry, "company", "taxpayer", "database")
    delete_journal(actual.path)
    before = file_hash(actual.path)
    bound = Store(
        actual.path,
        registry,
        "wrong-company" if wrong_field == "company_id" else "company",
        "wrong-database" if wrong_field == "database_id" else "database",
    )
    with pytest.raises(KernelError) as error:
        open_only(bound.connection)
    assert error.value.code == "company_mismatch"
    assert_untouched(actual.path, before)


def test_security_checks_catalog_instance_before_mutating_replacement(tmp_path):
    registry = default_registry()
    first, second = Catalog(tmp_path / "first", registry), Catalog(tmp_path / "second", registry)
    security = SecurityService(first.path)
    delete_journal(first.path)
    delete_journal(second.path)
    before = file_hash(second.path)
    second.path.replace(first.path)
    with pytest.raises(IdentityError) as error:
        security.authorize("synthetic-invalid-token")
    assert error.value.code == "OWNER_SECURITY_TARGET_MISMATCH"
    assert_untouched(first.path, before)


@pytest.mark.parametrize("change", ["unknown", "different_company", "deleted"])
def test_file_change_after_read_probe_is_checked_on_actual_write_handle(
    tmp_path, monkeypatch, change
):
    registry = default_registry()
    store = Store.create(tmp_path / "company.sqlite", registry, "company", "taxpayer", "database")
    delete_journal(store.path)
    replacement = None
    before = None
    if change == "unknown":
        replacement = unknown_database(tmp_path / "replacement.sqlite")
    elif change == "different_company":
        replacement = Store.create(
            tmp_path / "replacement.sqlite", registry, "other-company", "taxpayer", "database"
        ).path
        delete_journal(replacement)
    if replacement is not None:
        before = file_hash(replacement)
    actual_connect = sqlite3.connect
    replaced = []

    def swap_at_write_open(database, *args, **kwargs):
        if database == store.path.as_uri() + "?mode=rw":
            assert not replaced
            replaced.append(True)
            if replacement is None:
                store.path.unlink()
            else:
                replacement.replace(store.path)
        return actual_connect(database, *args, **kwargs)

    monkeypatch.setattr(runtime.sqlite3, "connect", swap_at_write_open)
    with pytest.raises((KernelError, sqlite3.OperationalError)) as error:
        open_only(store.connection)
    assert replaced, (
        "the swap must occur after the read-only probe and before the actual write open"
    )
    if change == "deleted":
        assert isinstance(error.value, sqlite3.OperationalError)
        assert not store.path.exists(), "mode=rw must never recreate a removed company database"
    else:
        assert isinstance(error.value, KernelError)
        assert error.value.code == (
            "company_mismatch" if change == "different_company" else "schema_version_unsupported"
        )
        assert_untouched(store.path, before)


def test_verified_existing_company_still_enables_wal_and_commits(tmp_path):
    store = Store.create(
        tmp_path / "company.sqlite", default_registry(), "company", "taxpayer", "database"
    )
    delete_journal(store.path)
    engine = Engine(store)
    result = engine.register_evidence(
        b"synthetic boundary proof", "text/plain", "boundary proof", request_id="proof"
    )
    with store.connection(read_only=True) as connection:
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert (
            connection.execute(
                "SELECT content FROM evidence WHERE digest=?", (bytes.fromhex(result["digest"]),)
            ).fetchone()[0]
            == b"synthetic boundary proof"
        )
