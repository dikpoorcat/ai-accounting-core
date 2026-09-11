"""Verified, self-contained backups of the new SQLite company format.

Evidence bytes live inside the database, so the SQLite Online Backup API captures
the accounting state and its evidence in the same snapshot. No live ``.sqlite``
file is copied, and no archive member is extracted using a caller-supplied path.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
import zipfile
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .contracts import KernelError
from .runtime import connect, require_supported_runtime
from .versions import verify_schema

FORMAT = "ai-accounting-kernel/company-backup"
FORMAT_VERSION = 1
DATABASE_MEMBER = "company.sqlite"
MANIFEST_MEMBER = "manifest.json"
MAX_MANIFEST_BYTES = 64 * 1024
DEFAULT_MAX_DATABASE_BYTES = 32 * 1024**3


class BackupError(ValueError):
    """A backup failed verification or would replace an unapproved target."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _identity(connection: sqlite3.Connection, *, _registry=None) -> dict[str, Any]:
    try:
        verify_schema(connection, registry=_registry, allow_previous=True)
    except KernelError as exc:
        raise BackupError(str(exc)) from exc
    tables = {row[1]: row for row in connection.execute("PRAGMA table_list")}
    for name in ("identity", "evidence", "period_close"):
        if name not in tables or tables[name][2] != "table" or tables[name][5] != 1:
            raise BackupError(f"Required STRICT table is missing: {name}")
    rows = connection.execute(
        "SELECT id,company_id,taxpayer_id,database_id,schema_version FROM identity"
    ).fetchall()
    if len(rows) != 1 or rows[0]["id"] != 1:
        raise BackupError("Company identity must contain exactly one singleton row")
    identity = dict(rows[0])
    identity.pop("id")
    if any(
        not isinstance(identity[field], str) or not identity[field].strip()
        for field in ("company_id", "taxpayer_id", "database_id")
    ):
        raise BackupError("Company identity is incomplete")
    return identity


def _check_identity(
    identity: dict[str, Any],
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
) -> None:
    for field, expected in (
        ("company_id", expected_company_id),
        ("taxpayer_id", expected_taxpayer_id),
        ("database_id", expected_database_id),
    ):
        if expected is not None and identity[field] != expected:
            raise BackupError(f"Company identity mismatch: {field}")


def verify_file(
    path: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    _registry=None,
) -> dict[str, Any]:
    """Verify SQLite structure, foreign keys, company identity and all evidence."""
    database = Path(path).resolve()
    if not database.is_file():
        raise BackupError("Company database does not exist")
    connection = None
    try:
        connection = connect(database, read_only=True)
        connection.execute("BEGIN")
        identity = _identity(connection, _registry=_registry)
        _check_identity(
            identity,
            expected_company_id=expected_company_id,
            expected_taxpayer_id=expected_taxpayer_id,
            expected_database_id=expected_database_id,
        )
        checks = connection.execute("PRAGMA integrity_check").fetchall()
        if len(checks) != 1 or checks[0][0] != "ok":
            raise BackupError("SQLite integrity check failed")
        if connection.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise BackupError("SQLite foreign-key check failed")
        evidence_count = 0
        for row in connection.execute("SELECT digest,content FROM evidence"):
            if hashlib.sha256(row["content"]).digest() != row["digest"]:
                raise BackupError("Evidence content does not match its digest")
            evidence_count += 1
        closes = connection.execute("SELECT period,manifest,digest FROM period_close")
        latest_closed_period = None
        for row in closes:
            if hashlib.sha256(row["manifest"].encode("utf-8")).digest() != row["digest"]:
                raise BackupError("Period-close manifest does not match its digest")
            latest_closed_period = max(latest_closed_period or 0, row["period"])
        return {
            "identity": identity,
            "evidence_count": evidence_count,
            "latest_closed_period": latest_closed_period,
        }
    except sqlite3.Error as exc:
        raise BackupError("The company file is not a valid supported SQLite database") from exc
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def _publish_new(temporary: Path, destination: Path) -> None:
    """Publish a completed file without ever replacing an existing destination."""
    if os.name == "nt":
        # Windows rename fails if the destination exists; POSIX rename replaces it.
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)
        temporary.unlink()


def _sync_file(path: Path) -> None:
    # Windows _commit (used by os.fsync) requires a writable file descriptor.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def backup_to_file(
    source: str | Path,
    target: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    timeout_seconds: float = 120.0,
    _registry=None,
) -> dict[str, Any]:
    """Create and verify one standalone snapshot; the target must not exist."""
    require_supported_runtime()
    source_path, target_path = Path(source).resolve(), Path(target).resolve()
    if source_path == target_path or target_path.exists():
        raise BackupError("Backup target must not exist")
    if not source_path.is_file():
        raise BackupError("Source company database does not exist")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    identity_checks = {
        "expected_company_id": expected_company_id,
        "expected_taxpayer_id": expected_taxpayer_id,
        "expected_database_id": expected_database_id,
    }
    with tempfile.TemporaryDirectory(
        prefix=".company-backup-", dir=target_path.parent
    ) as directory:
        temporary = Path(directory) / DATABASE_MEMBER
        source_connection = connect(source_path, read_only=True)
        destination = sqlite3.connect(temporary, isolation_level=None)
        try:
            source_connection.execute("BEGIN")
            _check_identity(_identity(source_connection, _registry=_registry), **identity_checks)
            destination.execute("PRAGMA synchronous=FULL")
            started = time.monotonic()

            def progress(_status: int, _remaining: int, _total: int) -> None:
                if time.monotonic() - started >= timeout_seconds:
                    raise BackupError("SQLite backup timed out")

            source_connection.backup(destination, pages=256, progress=progress, sleep=0.01)
            # A portable artifact must be complete without its own WAL sidecar.
            if destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise BackupError("Could not finalize the standalone database snapshot")
        finally:
            destination.close()
            source_connection.rollback()
            source_connection.close()
        result = verify_file(temporary, _registry=_registry, **identity_checks)
        _sync_file(temporary)
        try:
            _publish_new(temporary, target_path)
        except FileExistsError as exc:
            raise BackupError("Backup target must not exist") from exc
    return {**result, "path": str(target_path), "sha256": _digest(target_path)}


def _taxpayer_filename(taxpayer_id: str) -> str:
    if re.fullmatch(r"[0-9A-Z]{18}", taxpayer_id) is None:
        raise BackupError("Portable company filenames require an 18-character taxpayer ID")
    return f"{taxpayer_id}.finance-company.zip"


def _read_manifest(archive: zipfile.ZipFile, maximum: int) -> dict[str, Any]:
    entries = archive.infolist()
    if len(entries) != 2 or {entry.filename for entry in entries} != {
        DATABASE_MEMBER,
        MANIFEST_MEMBER,
    }:
        raise BackupError("Portable archive must contain only manifest.json and company.sqlite")
    for entry in entries:
        if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
            raise BackupError("Portable archive contains an unsupported member")
    if archive.getinfo(MANIFEST_MEMBER).file_size > MAX_MANIFEST_BYTES:
        raise BackupError("Portable manifest is too large")
    if archive.getinfo(DATABASE_MEMBER).file_size > maximum:
        raise BackupError("Portable database exceeds the configured size limit")
    try:
        manifest = json.loads(archive.read(MANIFEST_MEMBER))
    except (ValueError, UnicodeError) as exc:
        raise BackupError("Portable manifest is invalid JSON") from exc
    if not isinstance(manifest, dict) or (
        manifest.get("format") != FORMAT or manifest.get("format_version") != FORMAT_VERSION
    ):
        raise BackupError("Unsupported portable backup format")
    if manifest.get("database_member") != DATABASE_MEMBER:
        raise BackupError("Unexpected portable database path")
    if (
        not isinstance(manifest.get("database_sha256"), str)
        or re.fullmatch(r"[a-f0-9]{64}", manifest["database_sha256"]) is None
    ):
        raise BackupError("Portable database digest is invalid")
    return manifest


def _unpack_verified(
    path: Path,
    directory: Path,
    *,
    max_database_bytes: int,
    **identity_checks: Any,
) -> tuple[dict[str, Any], Path]:
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = _read_manifest(archive, max_database_bytes)
            database = directory / DATABASE_MEMBER
            actual_size = 0
            with archive.open(DATABASE_MEMBER) as source, database.open("xb") as destination:
                while chunk := source.read(1024 * 1024):
                    actual_size += len(chunk)
                    if actual_size > max_database_bytes:
                        raise BackupError("Portable database exceeds the configured size limit")
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        if _digest(database) != manifest["database_sha256"]:
            raise BackupError("Portable database digest mismatch")
        result = verify_file(database, **identity_checks)
        if any(manifest.get(field) != result[field] for field in result):
            raise BackupError("Portable manifest does not match the contained company database")
        _taxpayer_filename(result["identity"]["taxpayer_id"])
        return {**result, "manifest": manifest, "path": str(path)}, database
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise BackupError("Portable archive cannot be read") from exc


def verify_portable(
    path: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    max_database_bytes: int = DEFAULT_MAX_DATABASE_BYTES,
    _registry=None,
) -> dict[str, Any]:
    """Verify member paths, archive hashes, company identity and database contents."""
    require_supported_runtime()
    with tempfile.TemporaryDirectory(prefix="company-verify-") as directory:
        result, _database = _unpack_verified(
            Path(path).resolve(),
            Path(directory),
            max_database_bytes=max_database_bytes,
            _registry=_registry,
            expected_company_id=expected_company_id,
            expected_taxpayer_id=expected_taxpayer_id,
            expected_database_id=expected_database_id,
        )
        return result


def create_portable(
    source: str | Path,
    directory: str | Path,
    *,
    rollover: bool = False,
    request_id: str | None = None,
    _registry=None,
) -> dict[str, Any]:
    """Publish a verified company ZIP; rollover retains a verified previous ZIP.

    request_id makes publication retryable if the process exits after publishing
    but before its durable job can record success.
    """
    output = Path(directory).resolve()
    output.mkdir(parents=True, exist_ok=True)
    source_connection = connect(source, read_only=True)
    try:
        identity = _identity(source_connection, _registry=_registry)
    finally:
        source_connection.close()
    checks = {
        f"expected_{key}": identity[key] for key in ("company_id", "taxpayer_id", "database_id")
    }
    checks["_registry"] = _registry
    final = output / _taxpayer_filename(identity["taxpayer_id"])
    existing = verify_portable(final, **checks) if final.exists() else None
    if existing is not None:
        if request_id is not None and existing["manifest"].get("request_id") == request_id:
            return {**existing, "idempotent_replay": True}
        if not rollover:
            raise BackupError("Current company backup already exists; rollover was not requested")
    with tempfile.TemporaryDirectory(prefix=".company-package-", dir=output) as temporary:
        staging = Path(temporary)
        snapshot = backup_to_file(source, staging / DATABASE_MEMBER, **checks)
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "identity": snapshot["identity"],
            "evidence_count": snapshot["evidence_count"],
            "latest_closed_period": snapshot["latest_closed_period"],
            "database_member": DATABASE_MEMBER,
            "database_sha256": snapshot["sha256"],
            "created_at": datetime.now(UTC).isoformat(),
            "request_id": request_id,
        }
        candidate = staging / "package.zip"
        with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_MEMBER, _json(manifest))
            archive.write(staging / DATABASE_MEMBER, DATABASE_MEMBER)
        verified = verify_portable(candidate, **checks)
        _sync_file(candidate)
        if existing is not None:
            previous = output / f"{identity['taxpayer_id']}.previous.finance-company.zip"
            previous_stage = staging / "previous.zip"
            shutil.copyfile(final, previous_stage)
            _sync_file(previous_stage)
            os.replace(previous_stage, previous)
            os.replace(candidate, final)
        else:
            try:
                _publish_new(candidate, final)
            except FileExistsError as exc:
                raise BackupError("Company backup was published concurrently") from exc
    return {**verified, "path": str(final), "idempotent_replay": False}


def restore_portable(
    archive: str | Path,
    target: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    max_database_bytes: int = DEFAULT_MAX_DATABASE_BYTES,
    _registry=None,
) -> dict[str, Any]:
    """Restore into an absent company file, never merge or replace an old file."""
    require_supported_runtime()
    destination = Path(target).resolve()
    if destination.exists() or any(
        Path(str(destination) + suffix).exists() for suffix in ("-wal", "-shm", "-journal")
    ):
        raise BackupError("Restore target and its SQLite sidecars must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(
        prefix=".company-restore-", dir=destination.parent
    ) as temporary:
        result, database = _unpack_verified(
            Path(archive).resolve(),
            Path(temporary),
            max_database_bytes=max_database_bytes,
            _registry=_registry,
            expected_company_id=expected_company_id,
            expected_taxpayer_id=expected_taxpayer_id,
            expected_database_id=expected_database_id,
        )
        try:
            _publish_new(database, destination)
        except FileExistsError as exc:
            raise BackupError("Restore target must not exist") from exc
    return {**result, "path": str(destination)}


@contextmanager
def _worker_lock(database: Path):
    """Keep one backup worker per file; OS locks automatically release on crash."""
    lock_path = Path(str(database) + ".backup-worker.lock")
    with lock_path.open("a+b") as lock:
        if lock.tell() == 0:
            lock.write(b"\0")
            lock.flush()
        lock.seek(0)
        try:
            if os.name == "nt":
                import msvcrt

                msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            yield False
            return
        try:
            yield True
        finally:
            lock.seek(0)
            if os.name == "nt":
                msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def run_backup_jobs(database: str | Path, *, limit: int = 1) -> list[dict[str, Any]]:
    """Run pending/failed jobs and recover interrupted running backup jobs.

    The separate OS worker lock covers the slow packaging work while SQLite's
    write transaction covers only claiming a job and recording its result.
    Backup failure changes no accounting state and never undoes a closed month.
    Each job is attempted at most once in this invocation.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be positive")
    path = Path(database).resolve()
    if not path.is_file():
        raise BackupError("Source company database does not exist")
    outcomes: list[dict[str, Any]] = []
    attempted: list[str] = []
    with _worker_lock(path) as acquired:
        if not acquired:
            return outcomes
        connection = connect(path, validator=verify_schema)
        try:
            verify_schema(connection)
            for _ in range(limit):
                connection.execute("BEGIN IMMEDIATE")
                try:
                    exclude = (
                        f"AND id NOT IN ({','.join('?' for _ in attempted)})" if attempted else ""
                    )
                    job = connection.execute(
                        "SELECT id,payload FROM jobs WHERE kind='portable_backup' "
                        "AND status IN ('pending','running','failed') AND attempts<3 "
                        f"{exclude} ORDER BY attempts,id LIMIT 1",
                        attempted,
                    ).fetchone()
                    if job is None:
                        connection.rollback()
                        break
                    connection.execute(
                        "UPDATE jobs SET status='running',attempts=attempts+1,last_error=NULL "
                        "WHERE id=?",
                        (job["id"],),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                attempted.append(job["id"])
                try:
                    payload = json.loads(job["payload"])
                    if not isinstance(payload, dict) or not isinstance(
                        payload.get("directory"), str
                    ):
                        raise BackupError("Backup job requires an output directory")
                    rollover = payload.get("rollover", False)
                    if type(rollover) is not bool:
                        raise BackupError("Backup job rollover must be boolean")
                    result = create_portable(
                        path, payload["directory"], rollover=rollover, request_id=job["id"]
                    )
                    status, error = "succeeded", None
                except Exception as exc:
                    result = None
                    status, error = "failed", f"{type(exc).__name__}: {exc}"[:500]
                connection.execute("BEGIN IMMEDIATE")
                try:
                    connection.execute(
                        "UPDATE jobs SET status=?,last_error=?,result=? WHERE id=?",
                        (status, error, _json(result) if result is not None else None, job["id"]),
                    )
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                outcomes.append(
                    {"id": job["id"], "status": status, "result": result, "error": error}
                )
        finally:
            connection.close()
    return outcomes
