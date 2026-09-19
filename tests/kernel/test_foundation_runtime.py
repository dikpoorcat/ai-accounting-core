"""Fresh-root, snapshot and recovery boundaries use synthetic private files only."""

import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from ai_accounting.kernel import backup, daemon, runtime
from ai_accounting.kernel.catalog import Catalog, ensure_catalog
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.permissions import (
    PrivatePathError,
    assert_private_file,
    create_private_file,
    ensure_private_directory,
)
from ai_accounting.kernel.runtime import connect, initialize_file
from ai_accounting.kernel.schema import initialize
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.security.windows import write_protected_json
from ai_accounting.kernel.storage import Store
from ai_accounting.kernel.versions import verify_schema

TAXPAYER = "91310000123456789A"


def test_sidecar_prepare_retries_sqlite_last_connection_removal(tmp_path, monkeypatch):
    catalog = Catalog(tmp_path / "root")
    keeper = connect(catalog.path)
    keeper.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, False)
    keeper.execute("SELECT count(*) FROM company").fetchone()
    create = runtime.create_private_file
    raced = []

    def create_after_last_close(path):
        try:
            return create(path)
        except FileExistsError:
            if not raced:
                keeper.close()
                assert not path.exists()
                raced.append(path)
            raise

    def never_tighten(_path):
        raise AssertionError("unknown sidecar permissions must not be modified")

    monkeypatch.setattr(runtime, "create_private_file", create_after_last_close)
    monkeypatch.setattr(runtime, "ensure_private_file", never_tighten)
    try:
        prepared, created = runtime._prepare_sqlite_sidecars(catalog.path, set(), tighten=False)
        assert len(raced) == 1
        assert len(prepared) == len(created) == 2
        for path in prepared:
            assert_private_file(path)
    finally:
        keeper.close()


def test_sidecar_prepare_retries_removal_after_create_before_stat(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.sqlite"
    create = runtime.create_private_file
    raced = []

    def disappearing_create(path):
        result = create(path)
        if not raced:
            path.unlink()
            raced.append(path)
        return result

    monkeypatch.setattr(runtime, "create_private_file", disappearing_create)
    prepared, created = runtime._prepare_sqlite_sidecars(database, set(), tighten=False)
    assert len(raced) == 1
    assert len(prepared) == len(created) == 2


def test_sidecar_prepare_does_not_retry_existing_permission_failure(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.sqlite"
    candidate = create_private_file(Path(str(database) + "-wal"))
    checked = []

    def reject_existing(path):
        checked.append(path)
        raise PrivatePathError(path)

    monkeypatch.setattr(runtime, "assert_private_file", reject_existing)
    with pytest.raises(PrivatePathError):
        runtime._prepare_sqlite_sidecars(database, set(), tighten=False)
    assert checked == [candidate]


def test_sidecar_prepare_disappearance_retry_is_bounded(tmp_path, monkeypatch):
    database = tmp_path / "synthetic.sqlite"
    attempts = []

    def disappearing_create(path):
        attempts.append(path)
        raise FileNotFoundError(path)

    monkeypatch.setattr(runtime, "create_private_file", disappearing_create)
    with pytest.raises(FileNotFoundError):
        runtime._prepare_sqlite_sidecars(database, set(), tighten=False)
    assert len(attempts) == 5


def test_runtime_retains_private_sidecars_and_allows_explicit_checkpoint(tmp_path):
    catalog = Catalog(tmp_path / "root")
    identities = None
    for read_only in (False, True, False, True):
        with catalog.connection(read_only=read_only) as connection:
            assert connection.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE)
            assert connection.execute("PRAGMA wal_autocheckpoint").fetchone()[0] == 1000
            connection.execute("SELECT count(*) FROM company").fetchone()
            if not read_only:
                assert tuple(connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()) == (
                    0,
                    0,
                    0,
                )
            paths = [Path(str(catalog.path) + suffix) for suffix in ("-wal", "-shm")]
            current = [(path.stat().st_dev, path.stat().st_ino) for path in paths]
            if identities is None:
                identities = current
            assert current == identities
        for path, identity in zip(paths, identities, strict=True):
            assert_private_file(path)
            assert (path.stat().st_dev, path.stat().st_ino) == identity


@pytest.mark.parametrize(
    "retained",
    [
        ".service.json",
        ".resident.lock",
        "service.log",
        ".operations",
        "company.sqlite",
        "x/company.sqlite",
        "companies/kept.sqlite",
        "catalog.sqlite-wal",
    ],
)
def test_catalog_absent_with_retained_files_does_not_start_or_write(
    tmp_path, retained, monkeypatch
):
    root = tmp_path / "root"
    target = root / retained
    ensure_private_directory(target.parent, parents=True)
    create_private_file(target).write_bytes(b"retained source")
    before = {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()}
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(KernelError) as error:
        daemon.ensure_service(root)
    assert error.value.code == "database_root_unrecognized"
    assert {p.relative_to(root): p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    assert not (root / "catalog.sqlite").exists()


@pytest.mark.parametrize("shape", ["zero", "old"])
def test_empty_or_old_catalog_does_not_create_service_files(tmp_path, shape, monkeypatch):
    root = ensure_private_directory(tmp_path / "root")
    path = create_private_file(root / "catalog.sqlite")
    if shape == "old":
        with closing(sqlite3.connect(path)) as connection:
            connection.execute("CREATE TABLE old_marker(value TEXT)")
            connection.execute("INSERT INTO old_marker VALUES('retained')")
            connection.commit()
    before = path.read_bytes()
    monkeypatch.setattr(daemon.subprocess, "Popen", lambda *a, **k: pytest.fail("must not spawn"))
    with pytest.raises(KernelError):
        daemon.ensure_service(root)
    assert path.read_bytes() == before
    assert {p.name for p in root.iterdir()} == {"catalog.sqlite"}


@pytest.mark.parametrize("where", ["initialize", "validate"])
def test_failed_atomic_initialization_never_publishes_target(tmp_path, where):
    root = ensure_private_directory(tmp_path / "root")
    target = root / "company.sqlite"

    def initialize_candidate(connection):
        connection.execute("CREATE TABLE retained(value TEXT)")
        if where == "initialize":
            raise RuntimeError("injected create failure")

    def validate(connection):
        raise RuntimeError("injected validation failure")

    with pytest.raises(RuntimeError, match="injected"):
        initialize_file(target, initialize_candidate, validate)
    assert list(root.iterdir()) == []


def test_wal_header_zero_is_recognized_by_transactional_sql(tmp_path):
    path = create_private_file(tmp_path / "company.sqlite")
    bundle = production_bundle()
    with closing(connect(path)) as writer:
        writer.execute("PRAGMA wal_autocheckpoint=0")
        initialize(writer, bundle, "c", "t", "d")
        with path.open("rb") as handle:
            handle.seek(68)
            assert int.from_bytes(handle.read(4), "big") == 0
        with closing(
            connect(path, read_only=True, validator=lambda c: verify_schema(c, bundle=bundle))
        ) as reader:
            assert reader.execute("PRAGMA application_id").fetchone()[0] == bundle.application_id
            assert verify_schema(reader, bundle=bundle) == 0


@pytest.mark.parametrize("entry", ["stop", "ensure"])
def test_metadata_protocol_mismatch_cannot_contact_or_stop_service(tmp_path, monkeypatch, entry):
    catalog = Catalog(tmp_path / "root")
    with catalog.connection(read_only=True) as connection:
        instance = connection.execute("SELECT instance_id FROM catalog_identity").fetchone()[0]
    metadata = {
        "protocol": daemon.SERVICE_PROTOCOL - 1,
        "pid": 123,
        "port": 1234,
        "capability": "synthetic",
        "catalog_id": instance,
        "build_id": "old",
        "database_format": catalog.database_format(),
    }
    write_protected_json(catalog.root / ".service.json", metadata)
    monkeypatch.setattr(daemon, "_request", lambda *a, **k: pytest.fail("must not contact service"))
    with pytest.raises(KernelError) as error:
        (daemon.stop_service if entry == "stop" else daemon.ensure_service)(catalog.root)
    assert error.value.code == "service_identity_mismatch"
    assert not (catalog.root / ".resident.lock").exists()
    assert not (catalog.root / "service.log").exists()


@pytest.mark.parametrize("kind", ["company", "catalog"])
@pytest.mark.parametrize("damage", ["missing", "blank"])
def test_schema_verifier_requires_complete_identity_and_releases_own_snapshot(
    tmp_path, kind, damage
):
    bundle = production_bundle()
    if kind == "company":
        path = Store.create(tmp_path / "company.sqlite", bundle, "c", "t", "d").path
        table, column = "identity", "company_id"
    else:
        path = ensure_catalog(tmp_path / "root")
        table, column = "catalog_identity", "instance_id"
    with closing(sqlite3.connect(path, isolation_level=None)) as connection:
        triggers = connection.execute(
            "SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name=?", (table,)
        ).fetchall()
        connection.execute("BEGIN IMMEDIATE")
        for name, _ in triggers:
            connection.execute(f'DROP TRIGGER "{name}"')
        connection.execute(
            f"DELETE FROM {table}" if damage == "missing" else f"UPDATE {table} SET {column}=' '"
        )
        for _, sql in triggers:
            connection.execute(sql)
        connection.commit()
        trace = []
        connection.set_trace_callback(trace.append)
        with pytest.raises(KernelError) as error:
            verify_schema(connection, bundle=bundle, kind=kind)
        assert error.value.code == "schema_identity_mismatch"
        assert not connection.in_transaction
        assert trace[0] == "BEGIN" and trace[-1] == "ROLLBACK"


def test_schema_verifier_keeps_existing_transaction(tmp_path):
    bundle = production_bundle()
    store = Store.create(tmp_path / "company.sqlite", bundle, "c", "t", "d")
    with store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute("UPDATE state SET management=management+1")
        assert verify_schema(connection, bundle=bundle) == 0
        assert connection.in_transaction
        assert connection.execute("SELECT management FROM state").fetchone()[0] == 1
        connection.rollback()


def test_restore_uses_scheduled_snapshot_even_if_original_changes_at_parser_open(
    tmp_path, monkeypatch
):
    source = Catalog(tmp_path / "source")
    company = source.create_company(TAXPAYER, "合成公司")
    archive = Path(backup.create_portable(company["path"], tmp_path / "backups")["path"])
    expected = archive.read_bytes()
    original_restore = backup.restore_portable
    observed = []

    def swap_original(snapshot, destination, **kwargs):
        observed.append(Path(snapshot))
        assert Path(snapshot) != archive
        assert Path(snapshot).read_bytes() == expected
        archive.write_bytes(b"changed after scheduled snapshot")
        return original_restore(snapshot, destination, **kwargs)

    monkeypatch.setattr(backup, "restore_portable", swap_original)
    target = Catalog(tmp_path / "target")
    restored = target.restore_company(str(archive), taxpayer_id=TAXPAYER, name="合成公司")
    assert observed and restored["id"] == company["id"]
    assert target.operations()[0]["status"] == "succeeded"
    assert backup.verify_file(restored["path"])["identity"]["company_id"] == company["id"]


def test_changed_restore_source_never_leaves_adoptable_staging(tmp_path):
    source = Catalog(tmp_path / "source")
    company = source.create_company(TAXPAYER, "合成公司")
    archive = Path(backup.create_portable(company["path"], tmp_path / "backups")["path"])

    def change_after_schedule(point):
        if point == "operation_recorded":
            archive.write_bytes(b"different archive")

    target = Catalog(tmp_path / "target", fault=change_after_schedule)
    with pytest.raises(KernelError) as error:
        target.restore_company(str(archive), taxpayer_id=TAXPAYER, name="合成公司")
    assert error.value.code == "restore_source_changed"
    operation = target.operations()[0]
    assert not (target.root / ".operations" / f"{operation['id']}.sqlite").exists()
    assert not (target.root / TAXPAYER / "company.sqlite").exists()
    with pytest.raises(KernelError):
        target._resume(operation["id"])
    assert target.companies() == []


def test_verifier_meta_structure_history_identity_share_one_wal_snapshot(tmp_path):
    bundle = production_bundle()
    store = Store.create(tmp_path / "company.sqlite", bundle, "c", "t", "d")
    swapped = []

    class Reader(sqlite3.Connection):
        def execute(self, sql, *args):
            result = super().execute(sql, *args)
            if sql.startswith("SELECT id,family,kind,status") and not swapped:
                swapped.append(True)
                with closing(sqlite3.connect(store.path)) as writer:
                    trigger = writer.execute(
                        "SELECT sql FROM sqlite_schema WHERE name='immutable_identity_UPDATE'"
                    ).fetchone()[0]
                    writer.execute("DROP TRIGGER immutable_identity_UPDATE")
                    writer.execute("UPDATE identity SET company_id=' '")
                    writer.execute(trigger)
                    writer.commit()
            return result

    with closing(sqlite3.connect(store.path, factory=Reader, isolation_level=None)) as reader:
        assert verify_schema(reader, bundle=bundle) == 0
        assert swapped and not reader.in_transaction
        with pytest.raises(KernelError) as error:
            verify_schema(reader, bundle=bundle)
        assert error.value.code == "schema_identity_mismatch"


@pytest.mark.parametrize("kind", ["create", "restore"])
def test_same_operation_is_exclusive_and_other_company_is_not_blocked(tmp_path, kind):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    archive = None
    if kind == "restore":
        source = Catalog(tmp_path / "source")
        company = source.create_company(TAXPAYER, "合成公司")
        archive = backup.create_portable(company["path"], tmp_path / "backups")["path"]
    paused, release = Event(), Event()

    def pause(point):
        if point == "file_prepared" and not paused.is_set():
            paused.set()
            assert release.wait(10)

    target = Catalog(tmp_path / "target", fault=pause)

    def operation():
        if kind == "create":
            return target.create_company(TAXPAYER, "合成公司")
        return target.restore_company(archive, taxpayer_id=TAXPAYER, name="合成公司")

    with ThreadPoolExecutor(max_workers=1) as executor:
        first = executor.submit(operation)
        try:
            assert paused.wait(10)
            with pytest.raises(KernelError) as error:
                operation()
            assert error.value.code == "company_operation_busy"
            other = target.create_company("91310000123456789B", "另一合成公司")
            assert other["taxpayer_id"] == "91310000123456789B"
        finally:
            release.set()
        result = first.result(timeout=10)
    assert operation()["id"] == result["id"]
    assert len(target.companies()) == 2
    assert all(row["status"] == "succeeded" for row in target.operations())


@pytest.mark.parametrize("entry", ["metadata", "health"])
@pytest.mark.parametrize(
    "damage",
    [
        "bool_version",
        "float_version",
        "status_list",
        "extra_key",
        "float_protocol",
        "array",
        "null",
    ],
)
def test_service_rejects_malformed_descriptors_without_shutdown(
    tmp_path, monkeypatch, entry, damage
):
    from copy import deepcopy

    catalog = Catalog(tmp_path / "root")
    with catalog.connection(read_only=True) as connection:
        instance = connection.execute("SELECT instance_id FROM catalog_identity").fetchone()[0]
    metadata = {
        "protocol": daemon.SERVICE_PROTOCOL,
        "pid": 123,
        "port": 1234,
        "capability": "synthetic",
        "catalog_id": instance,
        "build_id": "build",
        "database_format": catalog.database_format(),
    }
    candidate = deepcopy(metadata)
    if damage == "array":
        candidate = []
    elif damage == "null":
        candidate = None
    elif damage == "float_protocol":
        candidate["protocol"] = float(daemon.SERVICE_PROTOCOL)
    elif damage == "extra_key":
        candidate["database_format"]["unexpected"] = 1
    else:
        key, value = {
            "bool_version": ("version", False),
            "float_version": ("version", 0.0),
            "status_list": ("status", []),
        }[damage]
        candidate["database_format"][key] = value
    write_protected_json(catalog.root / ".service.json", metadata)
    if entry == "metadata":
        monkeypatch.setattr(daemon, "read_protected_json", lambda _path: candidate)
    calls = []

    def request(_metadata, path, *args, **kwargs):
        calls.append(path)
        assert entry == "health" and path == "/api/health"
        return candidate

    monkeypatch.setattr(daemon, "_request", request)
    with pytest.raises(KernelError) as error:
        daemon.stop_service(catalog.root)
    assert error.value.code == "service_identity_mismatch"
    assert "/api/shutdown" not in calls


@pytest.mark.parametrize("payload", [b"[]", b"null", b"true", b"{broken"])
def test_service_json_errors_use_stable_protocol_error(monkeypatch, payload):
    from io import BytesIO

    class Opener:
        def open(self, *args, **kwargs):
            return BytesIO(payload)

    monkeypatch.setattr(daemon.urllib.request, "build_opener", lambda *a: Opener())
    with pytest.raises(KernelError) as error:
        daemon._request({"capability": "synthetic", "port": 1234}, "/api/health")
    assert error.value.code == "service_protocol_error"


def test_service_command_keeps_valid_list_response(monkeypatch):
    from io import BytesIO

    class Opener:
        def open(self, *args, **kwargs):
            return BytesIO(b'[{"company_id":"synthetic"}]')

    monkeypatch.setattr(daemon.urllib.request, "build_opener", lambda *a: Opener())
    assert daemon._request(
        {"capability": "synthetic", "port": 1234}, "/api/command", {"command": "companies"}
    ) == [{"company_id": "synthetic"}]
