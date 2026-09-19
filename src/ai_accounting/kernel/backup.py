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
from .permissions import (
    PrivatePathError,
    adopt_private_file,
    create_private_file,
    ensure_private_file,
    private_temporary_directory,
    reject_reparse_path,
)
from .runtime import connect, require_supported_runtime
from .schema_bundle import DATABASE_FORMAT_KEYS, production_bundle, valid_database_format
from .types import YearMonth
from .versions import database_format, upgrade, verify_schema

FORMAT = "ai-accounting-kernel/company-backup"
FORMAT_VERSION = 2
DATABASE_MEMBER = "company.sqlite"
MANIFEST_MEMBER = "manifest.json"
MAX_MANIFEST_BYTES = 64 * 1024
DEFAULT_MAX_DATABASE_BYTES = 32 * 1024**3


class BackupError(ValueError):
    """A backup failed verification or would replace an unapproved target."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


_MANIFEST_KEYS = frozenset(
    {
        "format",
        "format_version",
        "database_format",
        "identity",
        "database_member",
        "database_bytes",
        "database_sha256",
        "evidence_count",
        "latest_closed_period",
        "created_at",
        "request_id",
    }
)
_DATABASE_FORMAT_KEYS = DATABASE_FORMAT_KEYS
_IDENTITY_KEYS = frozenset({"company_id", "taxpayer_id", "database_id"})


def _resolve_bundle(value):
    return production_bundle() if value is None else value


def _error(code: str, message: str) -> BackupError:
    return BackupError(code, message)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(path: Path) -> str:
    with path.open("rb") as handle:
        return hashlib.file_digest(handle, "sha256").hexdigest()


def _identity(connection: sqlite3.Connection) -> dict[str, str]:
    tables = {row[1]: row for row in connection.execute("PRAGMA table_list")}
    for name in ("identity", "evidence", "period_close"):
        if name not in tables or tables[name][2] != "table" or tables[name][5] != 1:
            raise _error("backup_content_invalid", f"Required STRICT table is missing: {name}")
    rows = connection.execute(
        "SELECT id,company_id,taxpayer_id,database_id FROM identity"
    ).fetchall()
    if len(rows) != 1 or rows[0]["id"] != 1:
        raise _error(
            "backup_content_invalid", "Company identity must contain exactly one singleton row"
        )
    identity = dict(rows[0])
    identity.pop("id")
    if any(
        not isinstance(identity[field], str) or not identity[field].strip()
        for field in ("company_id", "taxpayer_id", "database_id")
    ):
        raise _error("backup_content_invalid", "Company identity is incomplete")
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
            raise _error("backup_identity_mismatch", f"Company identity mismatch: {field}")


def _verify_connection(
    connection: sqlite3.Connection,
    bundle,
    *,
    allow_previous: bool,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
) -> dict[str, Any]:
    format_value = database_format(
        connection, bundle=bundle, kind="company", allow_previous=allow_previous
    )
    identity = _identity(connection)
    _check_identity(
        identity,
        expected_company_id=expected_company_id,
        expected_taxpayer_id=expected_taxpayer_id,
        expected_database_id=expected_database_id,
    )
    verifier = bundle.company_verifiers.get(format_value["version"])
    if verifier is None:
        raise _error(
            "backup_schema_unsupported",
            "No content verifier is installed for this company database version",
        )
    try:
        verification = verifier(connection, bundle)
    except KernelError as exc:
        raise _error("backup_content_invalid", f"{exc.code}: {exc}") from exc
    try:
        evidence_count = verification["counts"]["evidence"]
    except (KeyError, TypeError) as exc:
        raise _error(
            "backup_content_invalid", "Company verifier returned an invalid result"
        ) from exc
    if type(evidence_count) is not int or evidence_count < 0:
        raise _error("backup_content_invalid", "Company evidence count is invalid")
    latest_ordinal = connection.execute("SELECT max(period) FROM period_close").fetchone()[0]
    latest_closed_period = (
        str(YearMonth.from_ordinal(latest_ordinal)) if latest_ordinal is not None else None
    )
    return {
        "identity": identity,
        "database_format": format_value,
        "evidence_count": evidence_count,
        "latest_closed_period": latest_closed_period,
        "verification": verification,
    }


def verify_file(
    path: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    _bundle=None,
    _allow_previous: bool = False,
) -> dict[str, Any]:
    """Verify structure, saved accounting sources and derived data without upgrading."""
    bundle = _resolve_bundle(_bundle)
    database = reject_reparse_path(path)
    if not database.is_file():
        raise _error("backup_content_invalid", "Company database does not exist")
    connection = None
    try:

        def validate(candidate):
            verify_schema(candidate, bundle=bundle, kind="company", allow_previous=_allow_previous)

        connection = connect(database, read_only=True, validator=validate)
        connection.execute("BEGIN")
        return _verify_connection(
            connection,
            bundle,
            allow_previous=_allow_previous,
            expected_company_id=expected_company_id,
            expected_taxpayer_id=expected_taxpayer_id,
            expected_database_id=expected_database_id,
        )
    except BackupError:
        raise
    except KernelError as exc:
        raise _error("backup_schema_unsupported", f"{exc.code}: {exc}") from exc
    except sqlite3.Error as exc:
        raise _error(
            "backup_content_invalid", "The company file is not a valid supported SQLite database"
        ) from exc
    finally:
        if connection is not None:
            connection.rollback()
            connection.close()


def _publish_new(temporary: Path, destination: Path) -> None:
    """Publish a completed file without ever replacing an existing destination."""
    ensure_private_file(temporary)
    if os.name == "nt":
        # Windows rename fails if the destination exists; POSIX rename replaces it.
        os.rename(temporary, destination)
    else:
        os.link(temporary, destination)
        temporary.unlink()
    ensure_private_file(destination)
    _sync_file(destination)
    _sync_directory(destination.parent)


def _sync_file(path: Path) -> None:
    # Windows _commit (used by os.fsync) requires a writable file descriptor.
    with path.open("r+b") as handle:
        os.fsync(handle.fileno())


def _sync_directory(path: Path) -> None:
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def backup_to_file(
    source: str | Path,
    target: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    timeout_seconds: float = 120.0,
    _bundle=None,
) -> dict[str, Any]:
    """Create and verify one standalone snapshot; the target must not exist."""
    bundle = _resolve_bundle(_bundle)
    require_supported_runtime()
    source_path, target_path = reject_reparse_path(source), reject_reparse_path(target)
    if source_path == target_path or target_path.exists():
        raise _error("backup_target_exists", "Backup target must not exist")
    if not source_path.is_file():
        raise _error("backup_content_invalid", "Source company database does not exist")
    target_path.parent.mkdir(parents=True, exist_ok=True)
    identity_checks = {
        "expected_company_id": expected_company_id,
        "expected_taxpayer_id": expected_taxpayer_id,
        "expected_database_id": expected_database_id,
    }
    with private_temporary_directory(target_path.parent, prefix=".company-backup-") as directory:
        temporary = directory / DATABASE_MEMBER
        create_private_file(temporary)

        def validate(candidate):
            verify_schema(candidate, bundle=bundle, kind="company")

        try:
            source_connection = connect(source_path, read_only=True, validator=validate)
        except KernelError as exc:
            raise _error("backup_schema_unsupported", f"{exc.code}: {exc}") from exc
        destination = None
        try:
            destination = sqlite3.connect(temporary, isolation_level=None)
            source_connection.execute("BEGIN")
            _verify_connection(source_connection, bundle, allow_previous=False, **identity_checks)
            destination.execute("PRAGMA synchronous=FULL")
            started = time.monotonic()

            def progress(_status: int, _remaining: int, _total: int) -> None:
                if time.monotonic() - started >= timeout_seconds:
                    raise _error("backup_content_invalid", "SQLite backup timed out")

            source_connection.backup(destination, pages=256, progress=progress, sleep=0.01)
            # A portable artifact must be complete without its own WAL sidecar.
            if destination.execute("PRAGMA journal_mode=DELETE").fetchone()[0] != "delete":
                raise _error(
                    "backup_content_invalid",
                    "Could not finalize the standalone database snapshot",
                )
        finally:
            if destination is not None:
                destination.close()
            source_connection.rollback()
            source_connection.close()
        result = verify_file(temporary, _bundle=bundle, **identity_checks)
        _sync_file(temporary)
        try:
            _publish_new(temporary, target_path)
        except FileExistsError as exc:
            raise _error("backup_target_exists", "Backup target must not exist") from exc
    return {**result, "path": str(target_path), "sha256": _digest(target_path)}


def _taxpayer_filename(taxpayer_id: str) -> str:
    if re.fullmatch(r"[0-9A-Z]{18}", taxpayer_id) is None:
        raise _error(
            "backup_identity_mismatch",
            "Portable company filenames require an 18-character taxpayer ID",
        )
    return f"{taxpayer_id}.finance-company.zip"


def _manifest_invalid(message: str) -> BackupError:
    return _error("backup_manifest_invalid", message)


def _validate_manifest(manifest: Any, *, member_size: int) -> dict[str, Any]:
    if not isinstance(manifest, dict):
        raise _manifest_invalid("Portable manifest must be a JSON object")
    if (
        manifest.get("format") != FORMAT
        or type(manifest.get("format_version")) is not int
        or manifest["format_version"] != FORMAT_VERSION
    ):
        raise _error("backup_format_unsupported", "Unsupported portable backup format")
    if set(manifest) != _MANIFEST_KEYS:
        raise _manifest_invalid("Portable manifest fields do not match the version 2 contract")

    format_value = manifest["database_format"]
    if not valid_database_format(format_value) or format_value["kind"] != "company":
        raise _manifest_invalid("Portable database format metadata is invalid")

    identity = manifest["identity"]
    if not isinstance(identity, dict) or set(identity) != _IDENTITY_KEYS:
        raise _manifest_invalid("Portable company identity is invalid")
    if any(
        not isinstance(identity[field], str) or not identity[field].strip() for field in identity
    ):
        raise _manifest_invalid("Portable company identity is incomplete")
    if re.fullmatch(r"[0-9A-Z]{18}", identity["taxpayer_id"]) is None:
        raise _manifest_invalid("Portable taxpayer identity is invalid")

    if manifest["database_member"] != DATABASE_MEMBER:
        raise _manifest_invalid("Unexpected portable database path")
    if (
        type(manifest["database_bytes"]) is not int
        or manifest["database_bytes"] <= 0
        or manifest["database_bytes"] != member_size
    ):
        raise _manifest_invalid("Portable database size is invalid")
    if (
        not isinstance(manifest["database_sha256"], str)
        or re.fullmatch(r"[a-f0-9]{64}", manifest["database_sha256"]) is None
    ):
        raise _manifest_invalid("Portable database digest is invalid")
    if type(manifest["evidence_count"]) is not int or manifest["evidence_count"] < 0:
        raise _manifest_invalid("Portable evidence count is invalid")
    latest = manifest["latest_closed_period"]
    if latest is not None and (
        not isinstance(latest, str) or re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", latest) is None
    ):
        raise _manifest_invalid("Portable latest closed period is invalid")
    try:
        created = datetime.fromisoformat(manifest["created_at"])
    except (TypeError, ValueError) as exc:
        raise _manifest_invalid("Portable creation time is invalid") from exc
    if created.tzinfo is None or created.utcoffset() != UTC.utcoffset(created):
        raise _manifest_invalid("Portable creation time must be UTC")
    request_id = manifest["request_id"]
    if request_id is not None and (not isinstance(request_id, str) or not request_id):
        raise _manifest_invalid("Portable request identity is invalid")
    return manifest


def _assert_supported_format(format_value: dict[str, Any], bundle) -> None:
    if format_value["family"] != bundle.family:
        raise _error("backup_format_unsupported", "Portable backup belongs to another family")
    if format_value["status"] != bundle.status:
        raise _error(
            "backup_schema_unsupported", "Portable backup has an incompatible release status"
        )
    version = format_value["version"]
    current = bundle.current_versions["company"]
    if bundle.status == "draft" and version != current:
        raise _error(
            "backup_schema_unsupported", "Draft backups require the exact active draft contract"
        )
    if bundle.status == "released" and version > current:
        raise _error("backup_schema_unsupported", "Portable backup is newer than this package")
    contract = bundle.contracts["company"].get(version)
    if (
        contract is None
        or contract["status"] != format_value["status"]
        or contract["sha256"] != format_value["fingerprint"]
        or version not in bundle.company_verifiers
    ):
        raise _error(
            "backup_schema_unsupported", "Portable backup schema is not supported by this package"
        )


def _read_manifest(archive: zipfile.ZipFile, maximum: int, bundle) -> dict[str, Any]:
    entries = archive.infolist()
    if len(entries) != 2 or {entry.filename for entry in entries} != {
        DATABASE_MEMBER,
        MANIFEST_MEMBER,
    }:
        raise _manifest_invalid(
            "Portable archive must contain only manifest.json and company.sqlite"
        )
    for entry in entries:
        if entry.is_dir() or stat.S_ISLNK(entry.external_attr >> 16) or entry.flag_bits & 1:
            raise _manifest_invalid("Portable archive contains an unsupported member")
    if archive.getinfo(MANIFEST_MEMBER).file_size > MAX_MANIFEST_BYTES:
        raise _manifest_invalid("Portable manifest is too large")
    if archive.getinfo(DATABASE_MEMBER).file_size > maximum:
        raise _manifest_invalid("Portable database exceeds the configured size limit")
    try:
        manifest = json.loads(archive.read(MANIFEST_MEMBER))
    except (ValueError, UnicodeError) as exc:
        raise _manifest_invalid("Portable manifest is invalid JSON") from exc
    result = _validate_manifest(manifest, member_size=archive.getinfo(DATABASE_MEMBER).file_size)
    _assert_supported_format(result["database_format"], bundle)
    return result


def _unpack_verified(
    path: Path,
    directory: Path,
    *,
    max_database_bytes: int,
    bundle,
    upgrade_to_current: bool = False,
    **identity_checks: Any,
) -> tuple[dict[str, Any], Path]:
    try:
        with zipfile.ZipFile(path) as archive:
            manifest = _read_manifest(archive, max_database_bytes, bundle)
            database = directory / DATABASE_MEMBER
            actual_size = 0
            create_private_file(database)
            with archive.open(DATABASE_MEMBER) as source, database.open("wb") as destination:
                while chunk := source.read(1024 * 1024):
                    actual_size += len(chunk)
                    if actual_size > max_database_bytes:
                        raise _manifest_invalid(
                            "Portable database exceeds the configured size limit"
                        )
                    destination.write(chunk)
                destination.flush()
                os.fsync(destination.fileno())
        if _digest(database) != manifest["database_sha256"]:
            raise _error("backup_content_invalid", "Portable database digest mismatch")
        result = verify_file(
            database,
            _bundle=bundle,
            _allow_previous=True,
            **identity_checks,
        )
        if any(
            manifest.get(field) != result[field]
            for field in (
                "database_format",
                "identity",
                "evidence_count",
                "latest_closed_period",
            )
        ):
            raise _error(
                "backup_content_invalid",
                "Portable manifest does not match the contained company database",
            )
        _taxpayer_filename(result["identity"]["taxpayer_id"])
        source_format = result["database_format"]
        if upgrade_to_current and source_format["version"] != bundle.current_versions["company"]:

            def validate_source(connection):
                verify_schema(
                    connection,
                    bundle=bundle,
                    kind="company",
                    allow_previous=True,
                )

            connection = connect(database, validator=validate_source)
            try:
                upgrade(connection, bundle=bundle, kind="company")
                checkpoint = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                if checkpoint is None or checkpoint[0] != 0 or checkpoint[1] != checkpoint[2]:
                    raise _error(
                        "backup_content_invalid",
                        "Restored company database could not be finalized",
                    )
            except KernelError as exc:
                raise _error("backup_schema_unsupported", f"{exc.code}: {exc}") from exc
            finally:
                connection.close()
            result = verify_file(database, _bundle=bundle, **identity_checks)
            result["source_database_format"] = source_format
            _sync_file(database)
        return {**result, "manifest": manifest, "path": str(path)}, database
    except PrivatePathError:
        raise
    except (zipfile.BadZipFile, RuntimeError, OSError) as exc:
        raise _error("backup_content_invalid", "Portable archive cannot be read") from exc


def verify_portable(
    path: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    max_database_bytes: int = DEFAULT_MAX_DATABASE_BYTES,
    _bundle=None,
) -> dict[str, Any]:
    """Verify member paths, archive hashes, company identity and database contents."""
    bundle = _resolve_bundle(_bundle)
    require_supported_runtime()
    with private_temporary_directory(
        Path(tempfile.gettempdir()), prefix="company-verify-"
    ) as directory:
        result, _database = _unpack_verified(
            reject_reparse_path(path),
            directory,
            max_database_bytes=max_database_bytes,
            bundle=bundle,
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
    _bundle=None,
) -> dict[str, Any]:
    """Publish a verified company ZIP; rollover retains a verified previous ZIP.

    request_id makes publication retryable if the process exits after publishing
    but before its durable job can record success.
    """
    bundle = _resolve_bundle(_bundle)
    output = reject_reparse_path(directory)
    output.mkdir(parents=True, exist_ok=True)
    source_result = verify_file(source, _bundle=bundle)
    identity = source_result["identity"]
    checks = {
        f"expected_{key}": identity[key] for key in ("company_id", "taxpayer_id", "database_id")
    }
    checks["_bundle"] = bundle
    final = output / _taxpayer_filename(identity["taxpayer_id"])
    existing = None
    if final.exists():
        ensure_private_file(final)
        existing = verify_portable(final, **checks)
    if existing is not None:
        if request_id is not None and existing["manifest"].get("request_id") == request_id:
            return {**existing, "idempotent_replay": True}
        if not rollover:
            raise _error(
                "backup_target_exists",
                "Current company backup already exists; rollover was not requested",
            )
    with private_temporary_directory(output, prefix=".company-package-") as staging:
        snapshot = backup_to_file(source, staging / DATABASE_MEMBER, **checks)
        manifest = {
            "format": FORMAT,
            "format_version": FORMAT_VERSION,
            "database_format": snapshot["database_format"],
            "identity": snapshot["identity"],
            "evidence_count": snapshot["evidence_count"],
            "latest_closed_period": snapshot["latest_closed_period"],
            "database_member": DATABASE_MEMBER,
            "database_bytes": (staging / DATABASE_MEMBER).stat().st_size,
            "database_sha256": snapshot["sha256"],
            "created_at": datetime.now(UTC).isoformat(),
            "request_id": request_id,
        }
        candidate = staging / "package.zip"
        with zipfile.ZipFile(candidate, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            archive.writestr(MANIFEST_MEMBER, _json(manifest))
            archive.write(staging / DATABASE_MEMBER, DATABASE_MEMBER)
        adopt_private_file(candidate)
        verified = verify_portable(candidate, **checks)
        _sync_file(candidate)
        if existing is not None:
            previous = output / f"{identity['taxpayer_id']}.previous.finance-company.zip"
            previous_stage = staging / "previous.zip"
            if previous.exists():
                ensure_private_file(previous)
            shutil.copyfile(final, previous_stage)
            adopt_private_file(previous_stage)
            _sync_file(previous_stage)
            os.replace(previous_stage, previous)
            ensure_private_file(previous)
            _sync_file(previous)
            _sync_directory(output)
            os.replace(candidate, final)
            ensure_private_file(final)
            _sync_file(final)
            _sync_directory(output)
        else:
            try:
                _publish_new(candidate, final)
            except FileExistsError as exc:
                raise _error(
                    "backup_target_exists", "Company backup was published concurrently"
                ) from exc
    return {**verified, "path": str(final), "idempotent_replay": False}


def restore_portable(
    archive: str | Path,
    target: str | Path,
    *,
    expected_company_id: str | None = None,
    expected_taxpayer_id: str | None = None,
    expected_database_id: str | None = None,
    max_database_bytes: int = DEFAULT_MAX_DATABASE_BYTES,
    _bundle=None,
) -> dict[str, Any]:
    """Restore into an absent company file, never merge or replace an old file."""
    bundle = _resolve_bundle(_bundle)
    require_supported_runtime()
    destination = reject_reparse_path(target)
    if destination.exists() or any(
        reject_reparse_path(Path(str(destination) + suffix)).exists()
        for suffix in ("-wal", "-shm", "-journal")
    ):
        raise _error(
            "restore_target_exists", "Restore target and its SQLite sidecars must not exist"
        )
    destination.parent.mkdir(parents=True, exist_ok=True)
    with private_temporary_directory(destination.parent, prefix=".company-restore-") as temporary:
        result, database = _unpack_verified(
            reject_reparse_path(archive),
            temporary,
            max_database_bytes=max_database_bytes,
            bundle=bundle,
            upgrade_to_current=True,
            expected_company_id=expected_company_id,
            expected_taxpayer_id=expected_taxpayer_id,
            expected_database_id=expected_database_id,
        )
        try:
            _publish_new(database, destination)
        except FileExistsError as exc:
            raise _error("restore_target_exists", "Restore target must not exist") from exc
    return {**result, "path": str(destination)}


@contextmanager
def _worker_lock(database: Path):
    """Keep one backup worker per file; OS locks automatically release on crash."""
    lock_path = Path(str(database) + ".backup-worker.lock")
    ensure_private_file(lock_path, create=True)
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


def run_backup_jobs(database: str | Path, *, limit: int = 1, _bundle=None) -> list[dict[str, Any]]:
    """Run pending/failed jobs and recover interrupted running backup jobs.

    The separate OS worker lock covers the slow packaging work while SQLite's
    write transaction covers only claiming a job and recording its result.
    Backup failure changes no accounting state and never undoes a closed month.
    Each job is attempted at most once in this invocation.
    """
    if type(limit) is not int or not 1 <= limit <= 100:
        raise ValueError("limit must be positive")
    bundle = _resolve_bundle(_bundle)
    path = reject_reparse_path(database)
    if not path.is_file():
        raise _error("backup_content_invalid", "Source company database does not exist")
    outcomes: list[dict[str, Any]] = []
    attempted: list[str] = []
    with _worker_lock(path) as acquired:
        if not acquired:
            return outcomes

        def validate(candidate):
            verify_schema(candidate, bundle=bundle, kind="company")

        connection = connect(path, validator=validate)
        try:
            verify_schema(connection, bundle=bundle, kind="company")
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
                        raise _error(
                            "backup_manifest_invalid",
                            "Backup job requires an output directory",
                        )
                    rollover = payload.get("rollover", False)
                    if type(rollover) is not bool:
                        raise _error(
                            "backup_manifest_invalid", "Backup job rollover must be boolean"
                        )
                    result = create_portable(
                        path,
                        payload["directory"],
                        rollover=rollover,
                        request_id=job["id"],
                        _bundle=bundle,
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
