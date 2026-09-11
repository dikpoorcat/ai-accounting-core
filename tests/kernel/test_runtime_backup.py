from __future__ import annotations

import hashlib
import json
import sqlite3
import stat
import zipfile
from pathlib import Path

import pytest

from ai_accounting.kernel import backup, runtime
from ai_accounting.kernel.schema import initialize
from ai_accounting.kernel.service import default_registry

TAXPAYER = "91330100MA00000001"
COMPANY = "company-a"
DATABASE = "database-a"


def add_evidence(path: Path, content: bytes) -> None:
    connection = runtime.connect(path)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (hashlib.sha256(content).digest(), content, "application/pdf", "原始票据.pdf"),
        )
        connection.commit()
    finally:
        connection.close()


@pytest.fixture
def company(tmp_path: Path) -> Path:
    path = tmp_path / "company.sqlite"
    connection = runtime.connect(path)
    try:
        initialize(connection, default_registry(), COMPANY, TAXPAYER, DATABASE)
    finally:
        connection.close()
    add_evidence(path, b"original invoice bytes")
    return path


def make_archive(path: Path, entries: dict[str, bytes | str]) -> None:
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries.items():
            archive.writestr(name, data)


def archive_entries(path: str | Path) -> dict[str, bytes]:
    with zipfile.ZipFile(path) as archive:
        return {entry.filename: archive.read(entry) for entry in archive.infolist()}


def test_runtime_is_patched_durable_and_strict(company: Path) -> None:
    assert sqlite3.sqlite_version_info >= (3, 51, 3)
    connection = runtime.connect(company)
    try:
        assert connection.isolation_level is None
        for name, value in {
            "journal_mode": "wal",
            "synchronous": 2,
            "foreign_keys": 1,
            "recursive_triggers": 1,
            "read_uncommitted": 0,
        }.items():
            assert connection.execute(f"PRAGMA {name}").fetchone()[0] == value
        assert connection.getconfig(sqlite3.SQLITE_DBCONFIG_DEFENSIVE)
        assert connection.getlimit(sqlite3.SQLITE_LIMIT_ATTACHED) == 0
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute("UPDATE state SET accounting='not-an-integer' WHERE id=1")
    finally:
        connection.close()


def test_old_runtime_is_rejected_with_working_bootstrap_instructions(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 45, 1))
    with pytest.raises(runtime.RuntimeConfigurationError, match="kernel-runtime.ps1"):
        runtime.connect(tmp_path / "must-not-be-created.sqlite")
    assert not list(tmp_path.iterdir())


def test_read_snapshot_and_next_write_transaction_have_distinct_epochs(company) -> None:
    reader = runtime.connect(company, read_only=True)
    writer = runtime.connect(company)
    try:
        reader.execute("BEGIN")
        assert reader.execute("SELECT accounting FROM state").fetchone()[0] == 0
        writer.execute("BEGIN IMMEDIATE")
        writer.execute("UPDATE state SET accounting=1 WHERE id=1")
        writer.commit()
        assert reader.execute("SELECT accounting FROM state").fetchone()[0] == 0
        reader.rollback()
        assert reader.execute("SELECT accounting FROM state").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            reader.execute("UPDATE state SET accounting=2 WHERE id=1")
    finally:
        reader.close()
        writer.close()


def test_online_backup_includes_committed_wal_and_is_standalone(company, tmp_path) -> None:
    live = runtime.connect(company)
    try:
        live.execute("PRAGMA wal_autocheckpoint=0")
        live.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (hashlib.sha256(b"in WAL").digest(), b"in WAL", "text/plain", "receipt"),
        )
        assert Path(str(company) + "-wal").stat().st_size > 0
        target = tmp_path / "snapshot.sqlite"
        result = backup.backup_to_file(company, target, expected_company_id=COMPANY)
        assert result["evidence_count"] == 2
        assert backup.verify_file(target)["evidence_count"] == 2
        assert not Path(str(target) + "-wal").exists()
        assert not Path(str(target) + "-shm").exists()
        with sqlite3.connect(target) as restored:
            assert restored.execute("PRAGMA journal_mode").fetchone()[0] == "delete"
    finally:
        live.close()


def test_backup_pins_snapshot_before_concurrent_evidence_is_received(
    company, tmp_path, monkeypatch
):
    original = backup._identity
    received = False

    def identity_then_receive(connection, **options):
        nonlocal received
        result = original(connection, **options)
        if not received:
            received = True
            add_evidence(company, b"received after backup snapshot")
        return result

    monkeypatch.setattr(backup, "_identity", identity_then_receive)
    result = backup.backup_to_file(company, tmp_path / "before-receipt.sqlite")
    assert result["evidence_count"] == 1
    assert backup.verify_file(company)["evidence_count"] == 2


@pytest.mark.parametrize(
    "field,value",
    [
        ("expected_company_id", "other-company"),
        ("expected_taxpayer_id", "91330100MA00000002"),
        ("expected_database_id", "other-database"),
    ],
)
def test_cross_company_identity_is_rejected(company, field, value) -> None:
    with pytest.raises(backup.BackupError, match="identity mismatch"):
        backup.verify_file(company, **{field: value})


def test_evidence_corruption_is_detected_before_backup_is_published(company, tmp_path) -> None:
    connection = runtime.connect(company)
    try:
        connection.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (hashlib.sha256(b"real").digest(), b"forged", "text/plain", "corrupt"),
        )
    finally:
        connection.close()
    with pytest.raises(backup.BackupError, match="Evidence"):
        backup.backup_to_file(company, tmp_path / "bad.sqlite")
    assert not (tmp_path / "bad.sqlite").exists()


def test_foreign_key_corruption_is_detected(company) -> None:
    # Deliberately simulate an externally damaged file; no business API does this.
    connection = sqlite3.connect(company, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO fact_evidence VALUES(?,?)", ("missing", b"x" * 32))
    finally:
        connection.close()
    with pytest.raises(backup.BackupError, match="foreign-key"):
        backup.verify_file(company)


def test_close_manifest_corruption_is_detected(company) -> None:
    connection = runtime.connect(company)
    try:
        connection.execute("INSERT INTO period_close VALUES(?,?,?)", (24320, "{}", b"x" * 32))
    finally:
        connection.close()
    with pytest.raises(backup.BackupError, match="Period-close"):
        backup.verify_file(company)


def test_initial_portable_round_trip_and_rollover_are_verified(company, tmp_path) -> None:
    output = tmp_path / "backups"
    initial = backup.create_portable(company, output, request_id="first")
    current = output / f"{TAXPAYER}.finance-company.zip"
    previous = output / f"{TAXPAYER}.previous.finance-company.zip"
    assert Path(initial["path"]) == current
    assert not previous.exists()
    restored = tmp_path / "restored.sqlite"
    result = backup.restore_portable(current, restored, expected_company_id=COMPANY)
    assert result["evidence_count"] == 1
    assert backup.verify_file(restored)["identity"]["database_id"] == DATABASE
    add_evidence(company, b"later invoice")
    second = backup.create_portable(company, output, rollover=True, request_id="second")
    previous_bytes = previous.read_bytes()
    assert second["evidence_count"] == 2
    assert backup.verify_portable(previous)["evidence_count"] == 1
    assert backup.create_portable(company, output, rollover=True, request_id="second")[
        "idempotent_replay"
    ]
    assert previous.read_bytes() == previous_bytes
    with pytest.raises(backup.BackupError, match="rollover"):
        backup.create_portable(company, output)


@pytest.mark.parametrize("existing", [b"", b"existing accounting data"])
def test_restore_refuses_even_empty_existing_targets(company, tmp_path, existing) -> None:
    archive = backup.create_portable(company, tmp_path / "backups")["path"]
    target = tmp_path / "already-there.sqlite"
    target.write_bytes(existing)
    with pytest.raises(backup.BackupError, match="must not exist"):
        backup.restore_portable(archive, target)
    assert target.read_bytes() == existing


def test_restore_refuses_orphan_sidecars_and_cross_company_archives(company, tmp_path) -> None:
    archive = backup.create_portable(company, tmp_path / "backups")["path"]
    target = tmp_path / "new.sqlite"
    sidecar = Path(str(target) + "-wal")
    sidecar.write_bytes(b"unrecovered transactions")
    with pytest.raises(backup.BackupError, match="sidecars"):
        backup.restore_portable(archive, target)
    assert sidecar.read_bytes() == b"unrecovered transactions"
    with pytest.raises(backup.BackupError, match="identity mismatch"):
        backup.restore_portable(archive, tmp_path / "other.sqlite", expected_company_id="other")
    assert not (tmp_path / "other.sqlite").exists()


def test_zip_path_and_symlink_members_are_rejected_without_extraction(company, tmp_path) -> None:
    archive = backup.create_portable(company, tmp_path / "backups")["path"]
    entries = archive_entries(archive)
    traversal = tmp_path / "traversal.zip"
    make_archive(traversal, {"manifest.json": entries["manifest.json"], "../escape.sqlite": b"x"})
    with pytest.raises(backup.BackupError, match="only manifest"):
        backup.verify_portable(traversal)
    assert not (tmp_path.parent / "escape.sqlite").exists()
    symlink = tmp_path / "symlink.zip"
    with zipfile.ZipFile(symlink, "w") as package:
        package.writestr("manifest.json", entries["manifest.json"])
        link = zipfile.ZipInfo("company.sqlite")
        link.create_system = 3
        link.external_attr = (stat.S_IFLNK | 0o777) << 16
        package.writestr(link, "elsewhere")
    with pytest.raises(backup.BackupError, match="unsupported member"):
        backup.verify_portable(symlink)


def test_archive_digest_manifest_and_size_are_all_verified(company, tmp_path) -> None:
    archive = backup.create_portable(company, tmp_path / "backups")["path"]
    entries = archive_entries(archive)
    forged = tmp_path / "forged.zip"
    make_archive(forged, {**entries, "company.sqlite": entries["company.sqlite"] + b"changed"})
    with pytest.raises(backup.BackupError, match="digest mismatch"):
        backup.verify_portable(forged)
    manifest = json.loads(entries["manifest.json"])
    manifest["identity"]["company_id"] = "another-company"
    make_archive(forged, {**entries, "manifest.json": json.dumps(manifest)})
    with pytest.raises(backup.BackupError, match="manifest does not match"):
        backup.verify_portable(forged)
    with pytest.raises(backup.BackupError, match="size limit"):
        backup.verify_portable(archive, max_database_bytes=1)


def test_snapshot_refuses_existing_targets_and_cleans_up_failed_snapshot(company, tmp_path) -> None:
    existing = tmp_path / "existing.sqlite"
    existing.write_bytes(b"keep")
    with pytest.raises(backup.BackupError, match="must not exist"):
        backup.backup_to_file(company, existing)
    assert existing.read_bytes() == b"keep"
    with pytest.raises(backup.BackupError, match="timed out"):
        backup.backup_to_file(company, tmp_path / "timeout.sqlite", timeout_seconds=0)
    assert not (tmp_path / "timeout.sqlite").exists()
    assert not list(tmp_path.glob(".company-backup-*"))


def add_job(company: Path, directory: Path) -> None:
    connection = runtime.connect(company)
    try:
        connection.execute(
            "INSERT INTO jobs(id,kind,payload,status) VALUES(?,?,?,?)",
            (
                "close-backup",
                "portable_backup",
                json.dumps({"directory": str(directory)}),
                "pending",
            ),
        )
    finally:
        connection.close()


def job_state(company: Path) -> dict:
    connection = runtime.connect(company, read_only=True)
    try:
        return dict(connection.execute("SELECT * FROM jobs WHERE id='close-backup'").fetchone())
    finally:
        connection.close()


def test_failed_backup_job_retries_without_touching_accounting(company, tmp_path, monkeypatch):
    add_job(company, tmp_path / "jobs-backup")
    original = backup.create_portable

    def fail(*args, **kwargs):
        raise OSError("simulated unavailable backup disk")

    monkeypatch.setattr(backup, "create_portable", fail)
    assert backup.run_backup_jobs(company)[0]["status"] == "failed"
    assert job_state(company)["attempts"] == 1
    monkeypatch.setattr(backup, "create_portable", original)
    assert backup.run_backup_jobs(company)[0]["status"] == "succeeded"
    assert job_state(company)["attempts"] == 2
    connection = runtime.connect(company, read_only=True)
    try:
        assert tuple(
            connection.execute("SELECT accounting,material,management FROM state").fetchone()
        ) == (0, 0, 0)
    finally:
        connection.close()


def test_crash_after_publication_reuses_same_backup_without_rollover(
    company, tmp_path, monkeypatch
):
    output = tmp_path / "jobs-backup"
    add_job(company, output)
    original = backup.create_portable

    def publish_then_crash(*args, **kwargs):
        original(*args, **kwargs)
        raise SystemExit("simulated process crash after publication")

    monkeypatch.setattr(backup, "create_portable", publish_then_crash)
    with pytest.raises(SystemExit):
        backup.run_backup_jobs(company)
    assert job_state(company)["status"] == "running"
    published = output / f"{TAXPAYER}.finance-company.zip"
    original_bytes = published.read_bytes()
    monkeypatch.setattr(backup, "create_portable", original)
    result = backup.run_backup_jobs(company)[0]
    assert result["status"] == "succeeded"
    assert result["result"]["idempotent_replay"]
    assert published.read_bytes() == original_bytes
    assert not (output / f"{TAXPAYER}.previous.finance-company.zip").exists()
    assert backup.run_backup_jobs(company) == []


def test_second_worker_does_not_steal_an_active_job(company, tmp_path) -> None:
    add_job(company, tmp_path / "jobs-backup")
    with backup._worker_lock(company) as acquired:
        assert acquired
        assert backup.run_backup_jobs(company) == []
    assert job_state(company)["attempts"] == 0
