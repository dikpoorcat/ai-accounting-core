from __future__ import annotations

import ctypes
import hashlib
import json
import os
import sqlite3
import stat
import threading
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

from ai_accounting.kernel import backup, runtime
from ai_accounting.kernel.schema import initialize
from ai_accounting.kernel.schema_bundle import production_bundle

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
        initialize(connection, production_bundle(), COMPANY, TAXPAYER, DATABASE)
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


def windows_reader_without_delete_share(path: Path):
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel.CreateFileW
    create_file.argtypes = [
        ctypes.c_wchar_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
        ctypes.c_ulong,
        ctypes.c_ulong,
        ctypes.c_void_p,
    ]
    create_file.restype = ctypes.c_void_p
    handle = create_file(str(path), 0x80000000, 3, None, 3, 0, None)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError(ctypes.get_last_error())
    close_handle = kernel.CloseHandle
    close_handle.argtypes = [ctypes.c_void_p]
    closed = threading.Event()

    def release():
        if not closed.is_set():
            close_handle(handle)
            closed.set()

    return release


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
    with pytest.raises(backup.BackupError, match="content_integrity_failed") as failure:
        backup.backup_to_file(company, tmp_path / "bad.sqlite")
    assert failure.value.__cause__.details["component"] == "evidence"
    assert not (tmp_path / "bad.sqlite").exists()


def test_foreign_key_corruption_is_detected(company) -> None:
    # Deliberately simulate an externally damaged file; no business API does this.
    connection = sqlite3.connect(company, isolation_level=None)
    try:
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("INSERT INTO fact_evidence VALUES(?,?)", ("missing", b"x" * 32))
    finally:
        connection.close()
    with pytest.raises(backup.BackupError, match="content_integrity_failed") as failure:
        backup.verify_file(company)
    assert failure.value.__cause__.details["reason"] == "foreign_key_check_failed"


def test_close_manifest_corruption_is_detected(company) -> None:
    from ai_accounting.kernel.frozen_material import MATERIAL_COVERAGE_RULE_DIGEST

    connection = runtime.connect(company)
    try:
        connection.execute("INSERT INTO period_close VALUES(?,?,?)", (24320, "{}", b"x" * 32))
        connection.execute(
            "INSERT INTO material_close_rule VALUES(?,?)",
            (24320, MATERIAL_COVERAGE_RULE_DIGEST),
        )
    finally:
        connection.close()
    with pytest.raises(backup.BackupError, match="content_integrity_failed") as failure:
        backup.verify_file(company)
    assert failure.value.__cause__.details["reason"] == "storage_root_digest_mismatch"


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


def test_portable_creation_verifies_only_the_packaged_snapshot_content(company, tmp_path) -> None:
    bundle = production_bundle()
    version = bundle.current_versions["company"]
    actual_verifier = bundle.company_verifiers[version]
    calls = []

    def counted(connection, active_bundle):
        calls.append(connection.execute("PRAGMA database_list").fetchone()[2])
        return actual_verifier(connection, active_bundle)

    observed = replace(bundle, company_verifiers={**bundle.company_verifiers, version: counted})
    archive = Path(backup.create_portable(company, tmp_path / "portable", _bundle=observed)["path"])
    assert len(calls) == 1
    assert calls[0] != str(company)
    assert backup.verify_portable(archive, _bundle=observed)["evidence_count"] == 1
    assert len(calls) == 2
    snapshot = tmp_path / "standalone.sqlite"
    assert backup.backup_to_file(company, snapshot, _bundle=observed)["evidence_count"] == 1
    assert len(calls) == 4  # Public standalone backup retains both content checks.
    restored = tmp_path / "restored.sqlite"
    assert backup.restore_portable(archive, restored, _bundle=observed)["evidence_count"] == 1
    assert len(calls) == 5  # External restore independently checks the archive.


def test_portable_creation_rejects_stable_source_damage_and_corrupt_replay(
    company, tmp_path
) -> None:
    output = tmp_path / "portable"
    archive = Path(backup.create_portable(company, output, request_id="first")["path"])
    original = archive.read_bytes()
    connection = runtime.connect(company)
    try:
        connection.execute(
            "INSERT INTO evidence VALUES(?,?,?,?)",
            (hashlib.sha256(b"real").digest(), b"forged", "text/plain", "damaged"),
        )
    finally:
        connection.close()
    with pytest.raises(backup.BackupError, match="content_integrity_failed"):
        backup.create_portable(company, output, rollover=True, request_id="first")
    assert archive.read_bytes() == original
    with pytest.raises(backup.BackupError, match="content_integrity_failed"):
        backup.create_portable(company, tmp_path / "new-portable")
    assert not list((tmp_path / "new-portable").glob("*.zip"))


def test_portable_creation_rejects_tampered_snapshot_and_zip(company, tmp_path, monkeypatch):
    original_copy = backup._copy_sqlite_snapshot

    def damage_snapshot(source_connection, temporary, *, timeout_seconds):
        original_copy(source_connection, temporary, timeout_seconds=timeout_seconds)
        data = temporary.read_bytes()
        expected = b"original invoice bytes"
        assert expected in data
        temporary.write_bytes(data.replace(expected, b"x" * len(expected), 1))

    monkeypatch.setattr(backup, "_copy_sqlite_snapshot", damage_snapshot)
    damaged_output = tmp_path / "damaged-snapshot"
    with pytest.raises(backup.BackupError, match="content_integrity_failed"):
        backup.create_portable(company, damaged_output)
    assert not list(damaged_output.glob("*.zip"))
    monkeypatch.setattr(backup, "_copy_sqlite_snapshot", original_copy)

    original_verify = backup.verify_portable

    def damage_zip(path, **kwargs):
        if Path(path).name == "package.zip":
            entries = archive_entries(path)
            database = bytearray(entries["company.sqlite"])
            database[-1] ^= 1
            make_archive(
                path, {"manifest.json": entries["manifest.json"], "company.sqlite": database}
            )
        return original_verify(path, **kwargs)

    monkeypatch.setattr(backup, "verify_portable", damage_zip)
    damaged_output = tmp_path / "damaged-zip"
    with pytest.raises(backup.BackupError, match="digest mismatch"):
        backup.create_portable(company, damaged_output)
    assert not list(damaged_output.glob("*.zip"))


def test_portable_creation_rejects_wrong_captured_company_identity(company, tmp_path, monkeypatch):
    other = tmp_path / "other.sqlite"
    connection = runtime.connect(other)
    try:
        initialize(
            connection, production_bundle(), "other-company", "91330100MA00000002", "other-db"
        )
    finally:
        connection.close()
    original_capture = backup._capture_portable_snapshot

    def copy_other(_source, target, bundle, **checks):
        return original_capture(other, target, bundle, **checks)

    monkeypatch.setattr(backup, "_capture_portable_snapshot", copy_other)
    output = tmp_path / "wrong-company"
    with pytest.raises(backup.BackupError, match="identity mismatch"):
        backup.create_portable(company, output)
    assert not list(output.glob("*.zip"))


def test_portable_creation_pins_wal_snapshot_during_concurrent_commit(
    company, tmp_path, monkeypatch
):
    original_copy = backup._copy_sqlite_snapshot
    committed = False

    def commit_after_source_header(source_connection, temporary, *, timeout_seconds):
        nonlocal committed
        if not committed:
            committed = True
            add_evidence(company, b"committed after source header")
        original_copy(source_connection, temporary, timeout_seconds=timeout_seconds)

    monkeypatch.setattr(backup, "_copy_sqlite_snapshot", commit_after_source_header)
    archive = backup.create_portable(company, tmp_path / "portable")["path"]
    assert committed
    assert backup.verify_portable(archive)["evidence_count"] == 1
    assert backup.verify_file(company)["evidence_count"] == 2


def test_portable_rollover_interruption_preserves_verified_current(company, tmp_path, monkeypatch):
    output = tmp_path / "portable"
    current = Path(backup.create_portable(company, output, request_id="first")["path"])
    original = current.read_bytes()
    add_evidence(company, b"later invoice")
    original_replace = backup.os.replace

    def deny_final(source, destination):
        if Path(source).name == "package.zip" and Path(destination) == current:
            raise PermissionError("simulated Windows denial during final replacement")
        return original_replace(source, destination)

    monkeypatch.setattr(backup.os, "replace", deny_final)
    with pytest.raises(PermissionError, match="simulated Windows denial"):
        backup.create_portable(company, output, rollover=True, request_id="second")
    previous = output / f"{TAXPAYER}.previous.finance-company.zip"
    assert current.read_bytes() == original
    assert not previous.exists()
    assert backup.verify_portable(current)["evidence_count"] == 1
    monkeypatch.setattr(backup.os, "replace", original_replace)
    assert (
        backup.create_portable(company, output, rollover=True, request_id="second")[
            "evidence_count"
        ]
        == 2
    )
    assert backup.verify_portable(previous)["evidence_count"] == 1


def test_third_rollover_failure_keeps_both_older_verified_archives(company, tmp_path, monkeypatch):
    output = tmp_path / "portable"
    current = Path(backup.create_portable(company, output, request_id="first")["path"])
    add_evidence(company, b"second invoice")
    backup.create_portable(company, output, rollover=True, request_id="second")
    previous = output / f"{TAXPAYER}.previous.finance-company.zip"
    old_current = current.read_bytes()
    old_previous = previous.read_bytes()
    add_evidence(company, b"third invoice")
    original_replace = backup.os.replace

    def deny_final(source, destination):
        if Path(source).name == "package.zip" and Path(destination) == current:
            raise PermissionError("simulated final replacement denial")
        return original_replace(source, destination)

    monkeypatch.setattr(backup.os, "replace", deny_final)
    with pytest.raises(PermissionError, match="simulated final replacement denial"):
        backup.create_portable(company, output, rollover=True, request_id="third")
    assert current.read_bytes() == old_current
    assert previous.read_bytes() == old_previous
    assert backup.verify_portable(current)["evidence_count"] == 2
    assert backup.verify_portable(previous)["evidence_count"] == 1
    monkeypatch.setattr(backup.os, "replace", original_replace)
    backup.create_portable(company, output, rollover=True, request_id="third")
    assert backup.verify_portable(current)["evidence_count"] == 3
    assert previous.read_bytes() == old_current


def test_portable_publication_refuses_simultaneous_same_company_writer(company, tmp_path):
    output = tmp_path / "portable"
    output.mkdir()
    with backup._worker_lock(output / f".{TAXPAYER}.publication") as acquired:
        assert acquired
        with pytest.raises(backup.BackupError) as failure:
            backup.create_portable(company, output, request_id="first")
        assert failure.value.code == "backup_target_changed"
    assert not list(output.glob("*.zip"))


def test_portable_publication_validates_taxpayer_before_creating_lock(
    company, tmp_path, monkeypatch
):
    output = tmp_path / "portable"
    actual_header = backup._file_header

    def invalid_header(source, bundle):
        header = actual_header(source, bundle)
        return {**header, "identity": {**header["identity"], "taxpayer_id": "../other"}}

    monkeypatch.setattr(backup, "_file_header", invalid_header)
    with pytest.raises(backup.BackupError) as failure:
        backup.create_portable(company, output, request_id="first")
    assert failure.value.code == "backup_identity_mismatch"
    assert not list(output.iterdir())


@pytest.mark.parametrize("interrupt_at", ["before_previous", "after_previous"])
def test_rollover_marker_recovers_committed_current(company, tmp_path, monkeypatch, interrupt_at):
    output = tmp_path / "portable"
    current = Path(backup.create_portable(company, output, request_id="first")["path"])
    old_current = current.read_bytes()
    add_evidence(company, b"second invoice")
    previous = output / f"{TAXPAYER}.previous.finance-company.zip"
    marker, pending = backup._rollover_paths(output, TAXPAYER)
    if interrupt_at == "before_previous":
        original_replace = backup.os.replace

        def deny_previous(source, destination):
            if Path(source) == pending and Path(destination) == previous:
                raise PermissionError("simulated interruption before previous")
            return original_replace(source, destination)

        monkeypatch.setattr(backup.os, "replace", deny_previous)
    else:
        original_clear = backup._clear_rollover

        def deny_clear(*_args):
            raise RuntimeError("simulated interruption after previous")

        monkeypatch.setattr(backup, "_clear_rollover", deny_clear)
    with pytest.raises((PermissionError, RuntimeError), match="simulated interruption"):
        backup.create_portable(company, output, rollover=True, request_id="second")
    assert marker.exists()
    assert backup.verify_portable(current)["evidence_count"] == 2
    if interrupt_at == "before_previous":
        assert pending.exists() and not previous.exists()
        monkeypatch.setattr(backup.os, "replace", original_replace)
    else:
        assert not pending.exists() and previous.read_bytes() == old_current
        monkeypatch.setattr(backup, "_clear_rollover", original_clear)
    assert backup.create_portable(company, output, rollover=True, request_id="second")[
        "idempotent_replay"
    ]
    assert not marker.exists() and not pending.exists()
    assert previous.read_bytes() == old_current


@pytest.mark.parametrize(
    "damage", ["marker", "marker_bool", "marker_float", "pending", "orphan"]
)
def test_rollover_recovery_rejects_damaged_marker_or_foreign_pending(
    company, tmp_path, monkeypatch, damage
):
    output = tmp_path / "portable"
    current = Path(backup.create_portable(company, output, request_id="first")["path"])
    old_current = current.read_bytes()
    add_evidence(company, b"second invoice")
    original_replace = backup.os.replace

    def deny_final(source, destination):
        if Path(source).name == "package.zip" and Path(destination) == current:
            raise PermissionError("simulated final denial")
        return original_replace(source, destination)

    monkeypatch.setattr(backup.os, "replace", deny_final)
    with pytest.raises(PermissionError, match="simulated final denial"):
        backup.create_portable(company, output, rollover=True, request_id="second")
    monkeypatch.setattr(backup.os, "replace", original_replace)
    marker, pending = backup._rollover_paths(output, TAXPAYER)
    if damage.startswith("marker"):
        state = json.loads(marker.read_text(encoding="utf-8"))
        if damage == "marker":
            state["path"] = str(tmp_path / "outside.zip")
        else:
            state["version"] = True if damage == "marker_bool" else 1.0
        marker.write_text(json.dumps(state))
    elif damage == "pending":
        pending.write_bytes(b"foreign archive")
    else:
        marker.unlink()
    with pytest.raises(backup.BackupError) as failure:
        backup.create_portable(company, output, rollover=True, request_id="second")
    assert failure.value.code == "backup_target_changed"
    assert marker.exists() is (damage != "orphan")
    assert pending.exists()
    assert current.read_bytes() == old_current
    assert backup.verify_portable(current)["evidence_count"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_portable_rollover_waits_for_short_windows_reader(company, tmp_path, monkeypatch):
    output = tmp_path / "portable"
    current = Path(backup.create_portable(company, output, request_id="first")["path"])
    add_evidence(company, b"second invoice")
    release = windows_reader_without_delete_share(current)
    original_replace = backup.os.replace
    denied = []
    timers = []

    def observed_replace(source, destination):
        try:
            return original_replace(source, destination)
        except PermissionError as exc:
            if Path(source).name == "package.zip" and Path(destination) == current:
                denied.append(exc.winerror)
                if len(denied) == 1:
                    timer = threading.Timer(0.1, release)
                    timer.start()
                    timers.append(timer)
            raise

    monkeypatch.setattr(backup.os, "replace", observed_replace)
    try:
        backup.create_portable(company, output, rollover=True, request_id="second")
    finally:
        release()
        for timer in timers:
            timer.join()
    assert denied and set(denied) <= {5, 32}
    assert backup.verify_portable(current)["evidence_count"] == 2


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_portable_rollover_timeout_preserves_current_and_previous(company, tmp_path, monkeypatch):
    output = tmp_path / "portable"
    current = Path(backup.create_portable(company, output, request_id="first")["path"])
    add_evidence(company, b"second invoice")
    backup.create_portable(company, output, rollover=True, request_id="second")
    previous = output / f"{TAXPAYER}.previous.finance-company.zip"
    old_current = current.read_bytes()
    old_previous = previous.read_bytes()
    add_evidence(company, b"third invoice")
    release = windows_reader_without_delete_share(current)
    default_wait = backup._ROLLOVER_WAIT_SECONDS
    monkeypatch.setattr(backup, "_ROLLOVER_WAIT_SECONDS", 0.2)
    try:
        with pytest.raises(PermissionError) as failure:
            backup.create_portable(company, output, rollover=True, request_id="third")
        assert failure.value.winerror in (5, 32)
    finally:
        release()
        monkeypatch.setattr(backup, "_ROLLOVER_WAIT_SECONDS", default_wait)
    assert current.read_bytes() == old_current
    assert previous.read_bytes() == old_previous
    assert backup.verify_portable(current)["evidence_count"] == 2
    assert backup.verify_portable(previous)["evidence_count"] == 1
    backup.create_portable(company, output, rollover=True, request_id="third")
    assert backup.verify_portable(current)["evidence_count"] == 3
    assert previous.read_bytes() == old_current


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_portable_rollover_rejects_changed_candidate_during_windows_wait(
    company, tmp_path, monkeypatch
):
    output = tmp_path / "portable"
    current = Path(backup.create_portable(company, output, request_id="first")["path"])
    old_current = current.read_bytes()
    add_evidence(company, b"second invoice")
    release = windows_reader_without_delete_share(current)
    original_replace = backup.os.replace
    denied = []

    def change_candidate_after_denial(source, destination):
        try:
            return original_replace(source, destination)
        except PermissionError as exc:
            if Path(source).name == "package.zip" and Path(destination) == current:
                denied.append(exc.winerror)
                with Path(source).open("ab") as handle:
                    handle.write(b"changed after verification")
                release()
            raise

    monkeypatch.setattr(backup.os, "replace", change_candidate_after_denial)
    try:
        with pytest.raises(backup.BackupError) as failure:
            backup.create_portable(company, output, rollover=True, request_id="second")
        assert failure.value.code == "backup_target_changed"
    finally:
        release()
    assert denied == [5]
    assert current.read_bytes() == old_current
    assert backup.verify_portable(current)["evidence_count"] == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows sharing semantics")
def test_portable_rollover_rejects_changed_target_identity_during_windows_wait(
    company, tmp_path, monkeypatch
):
    output = tmp_path / "portable"
    current = Path(backup.create_portable(company, output, request_id="first")["path"])
    old_current = current.read_bytes()
    add_evidence(company, b"second invoice")
    release = windows_reader_without_delete_share(current)
    original_replace = backup.os.replace
    denied = []

    def change_target_after_denial(source, destination):
        try:
            return original_replace(source, destination)
        except PermissionError as exc:
            if Path(source).name == "package.zip" and Path(destination) == current:
                denied.append(exc.winerror)
                info = current.stat()
                os.utime(current, ns=(info.st_atime_ns, info.st_mtime_ns + 1_000_000_000))
                release()
            raise

    monkeypatch.setattr(backup.os, "replace", change_target_after_denial)
    try:
        with pytest.raises(backup.BackupError) as failure:
            backup.create_portable(company, output, rollover=True, request_id="second")
        assert failure.value.code == "backup_target_changed"
    finally:
        release()
    assert denied == [5]
    assert current.read_bytes() == old_current
    assert backup.verify_portable(current)["evidence_count"] == 1


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
    with pytest.raises(backup.BackupError, match="size is invalid") as changed:
        backup.verify_portable(forged)
    assert changed.value.code == "backup_manifest_invalid"
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
