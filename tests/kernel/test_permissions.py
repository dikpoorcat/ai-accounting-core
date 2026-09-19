from __future__ import annotations

import hashlib
import os
import shutil
import stat
from pathlib import Path

import pytest

from ai_accounting.kernel import backup, runtime
from ai_accounting.kernel.catalog import Catalog
from ai_accounting.kernel.daemon import (
    ServiceClient,
    _metadata_for_root,
    default_root,
    ensure_service,
    instance_lock,
    run,
)
from ai_accounting.kernel.permissions import (
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    PrivatePathError,
    assert_private_directory,
    assert_private_file,
    create_private_file,
    ensure_private_directory,
    ensure_private_file,
)
from ai_accounting.kernel.schema import initialize
from ai_accounting.kernel.service import default_registry

COMPANY = "permission-company"
TAXPAYER = "91330100MA00000001"
DATABASE = "permission-database"


def company_file(tmp_path: Path) -> Path:
    path = tmp_path / "company.sqlite"
    connection = runtime.connect(path)
    try:
        initialize(connection, default_registry(), COMPANY, TAXPAYER, DATABASE)
        connection.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (hashlib.sha256(b"synthetic").digest(), b"synthetic", "text/plain", "x"),
        )
    finally:
        connection.close()
    return path


def test_private_directory_file_and_lock_are_created_before_use(tmp_path):
    root = ensure_private_directory(tmp_path / "owned")
    file = create_private_file(root / "state.bin")
    file.write_bytes(b"x" * 100_000)
    assert assert_private_directory(root) == root
    assert assert_private_file(file) == file
    with instance_lock(root) as acquired:
        assert acquired
        assert_private_file(root / ".resident.lock")


def test_existing_owner_file_is_tightened_but_links_are_rejected(tmp_path):
    root = ensure_private_directory(tmp_path / "owned")
    file = create_private_file(root / "state.bin")
    file.chmod(0o666)
    ensure_private_file(file)
    assert_private_file(file)
    link = root / "linked.bin"
    try:
        link.symlink_to(file)
    except OSError:
        pytest.skip("platform does not permit synthetic symlinks")
    with pytest.raises(PrivatePathError):
        ensure_private_file(link)


def test_runtime_and_catalog_reject_linked_parent_before_resolving_it(tmp_path):
    target = ensure_private_directory(tmp_path / "owned")
    link = tmp_path / "linked-root"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("platform does not permit synthetic directory symlinks")
    with pytest.raises(PrivatePathError):
        runtime.connect(link / "company.sqlite")
    with pytest.raises(PrivatePathError):
        Catalog(link, default_registry())


def test_daemon_entry_points_reject_linked_root_before_service_access(tmp_path, monkeypatch):
    target = ensure_private_directory(tmp_path / "owned-daemon")
    link = tmp_path / "linked-daemon"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("platform does not permit synthetic directory symlinks")

    monkeypatch.setenv("FINANCE_DATA_ROOT", str(link))
    with pytest.raises(PrivatePathError):
        default_root()
    with pytest.raises(PrivatePathError):
        with instance_lock(link):
            pass
    with pytest.raises(PrivatePathError):
        _metadata_for_root(link)
    with pytest.raises(PrivatePathError):
        ensure_service(link, timeout=0)
    with pytest.raises(PrivatePathError):
        ServiceClient(link, metadata={"catalog_id": "synthetic"}, credential_store=object())
    with pytest.raises(PrivatePathError):
        run(link)


def test_runtime_secures_main_database_and_new_sqlite_sidecars(tmp_path):
    database = tmp_path / "company.sqlite"
    connection = runtime.connect(database)
    try:
        connection.execute("CREATE TABLE sample(value TEXT)")
        connection.execute("INSERT INTO sample VALUES('synthetic')")
        assert_private_file(database)
        assert_private_file(Path(str(database) + "-wal"))
        assert_private_file(Path(str(database) + "-shm"))
    finally:
        connection.close()


def test_runtime_creates_private_wal_and_shm_before_sqlite_opens(tmp_path, monkeypatch):
    database = tmp_path / "company.sqlite"
    opened = []
    original = runtime.sqlite3.connect

    def observe(*args, **kwargs):
        assert_private_file(Path(str(database) + "-wal"))
        assert_private_file(Path(str(database) + "-shm"))
        opened.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.sqlite3, "connect", observe)
    connection = runtime.connect(database)
    connection.close()
    assert opened == [True]


def test_runtime_rejects_sidecar_link_before_sqlite_opens(tmp_path, monkeypatch):
    database = company_file(tmp_path)
    sidecar = Path(str(database) + "-wal")
    target = tmp_path / "outside-wal"
    target.write_bytes(b"synthetic")
    try:
        sidecar.symlink_to(target)
    except OSError:
        pytest.skip("platform does not permit synthetic file symlinks")
    opened = []
    original = runtime.sqlite3.connect

    def observe(*args, **kwargs):
        opened.append(True)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime.sqlite3, "connect", observe)
    with pytest.raises(PrivatePathError):
        runtime.connect(database, read_only=True)
    assert opened == []


@pytest.mark.parametrize(
    "operation", ["verify", "backup_source", "backup_target", "package", "restore"]
)
def test_backup_paths_reject_linked_sources_and_targets_before_normalization(tmp_path, operation):
    owned = ensure_private_directory(tmp_path / "owned")
    source = company_file(owned)
    package = backup.create_portable(source, tmp_path / "packages")
    link = tmp_path / "linked"
    try:
        link.symlink_to(owned, target_is_directory=True)
    except OSError:
        pytest.skip("platform does not permit synthetic directory symlinks")
    calls = {
        "verify": lambda: backup.verify_file(link / "company.sqlite"),
        "backup_source": lambda: backup.backup_to_file(
            link / "company.sqlite", tmp_path / "copy.sqlite"
        ),
        "backup_target": lambda: backup.backup_to_file(source, link / "copy.sqlite"),
        "package": lambda: backup.create_portable(source, link / "backups"),
        "restore": lambda: backup.restore_portable(package["path"], link / "restored.sqlite"),
    }
    with pytest.raises(PrivatePathError):
        calls[operation]()
    assert not (owned / "copy.sqlite").exists()
    assert not (owned / "restored.sqlite").exists()
    assert not (owned / "backups").exists()


def test_external_backup_parent_is_unchanged_while_packages_and_restore_are_private(tmp_path):
    source = company_file(tmp_path)
    external = tmp_path / "shared-backups"
    external.mkdir()
    if os.name == "nt":
        with pytest.raises(PrivatePathError):
            assert_private_directory(external)
    else:
        external.chmod(0o755)
    before = stat.S_IMODE(external.stat().st_mode)

    current = Path(backup.create_portable(source, external, request_id="first")["path"])
    connection = runtime.connect(source)
    try:
        connection.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (hashlib.sha256(b"later").digest(), b"later", "text/plain", "y"),
        )
    finally:
        connection.close()
    backup.create_portable(source, external, request_id="second", rollover=True)
    previous = external / f"{TAXPAYER}.previous.finance-company.zip"
    assert stat.S_IMODE(external.stat().st_mode) == before
    assert_private_file(current)
    assert_private_file(previous)

    shared_input = tmp_path / "shared-input.zip"
    shutil.copyfile(current, shared_input)
    restored = tmp_path / "restored.sqlite"
    backup.restore_portable(shared_input, restored, expected_company_id=COMPANY)
    assert_private_file(restored)


@pytest.mark.skipif(os.name == "nt", reason="POSIX mode contract")
def test_posix_modes_are_exact(tmp_path):
    directory = ensure_private_directory(tmp_path / "owned")
    file = create_private_file(directory / "state")
    assert stat.S_IMODE(directory.stat().st_mode) == PRIVATE_DIRECTORY_MODE
    assert stat.S_IMODE(file.stat().st_mode) == PRIVATE_FILE_MODE
