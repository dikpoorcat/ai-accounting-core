"""Deterministic, offline backup primitives for the private-pilot deployment.

This module intentionally does not start or stop services, read credentials, execute
``pg_dump``, or delete anything.  The deployment wrapper owns those side effects.  It
passes an already-stopped-service assertion and a narrowly shaped dump adapter here;
the core then makes a content-addressed, verifiable backup directory.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Protocol

from . import path_security
from .path_security import (
    PathSecurityError,
    ensure_directory_in_root,
    read_regular_file_in_root,
    write_new_regular_file_in_root,
)

MAX_MANIFEST_BYTES = 1_000_000
DEFAULT_MAX_EVIDENCE_BYTES = 100 * 1024 * 1024
DEFAULT_MAX_DATABASE_DUMP_BYTES = 20 * 1024 * 1024 * 1024
_BACKUP_ID_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}\Z")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
_REVISION_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,79}\Z")
_SYSTEM_IDENTIFIER_PATTERN = re.compile(r"[1-9][0-9]{0,19}\Z")
_MANIFEST_NAME = "manifest.json"
_MANIFEST_DIGEST_NAME = "manifest.sha256"
_DATABASE_ARCHIVE_NAME = "database.dump"


class BackupError(ValueError):
    """Stable diagnostic for an offline-backup contract violation."""


class DatabaseDumpAdapter(Protocol):
    """Side-effect boundary for a credential-owning database backup wrapper."""

    def dump(self, destination: Path, metadata: DatabaseDumpMetadata) -> None:
        """Write one PostgreSQL custom-format archive to ``destination``."""

    def list_archive(self, archive: Path) -> Sequence[str]:
        """Return the archive table-of-contents; raise if it is not restorable."""


class BackupPublisher(Protocol):
    """Publish a verified partial directory with deployment-specific durability."""

    def publish(self, partial: Path, complete: Path, root: Path) -> None:
        """Atomically publish ``partial`` or raise while leaving it unpublished."""


@dataclass(frozen=True)
class BackupPrecondition:
    """Facts checked by the deployment wrapper after formal services stopped."""

    service_stopped: bool
    active_business_connections: int


@dataclass(frozen=True)
class EvidenceSnapshot:
    """A single Evidence row projected by the caller's consistent database read."""

    evidence_id: str
    sha256: str
    size_bytes: int
    storage_path: Path


@dataclass(frozen=True)
class DatabaseDumpMetadata:
    """Non-secret metadata permitted in a backup manifest."""

    schema_revision: str
    source_system_identifier: str
    archive_format: str = "pg_dump_custom"


@dataclass(frozen=True)
class BackupRequest:
    """Inputs fixed before backup work starts; no database URL or account is accepted."""

    backup_id: str
    purpose: str
    precondition: BackupPrecondition
    evidence_root: Path
    evidence: tuple[EvidenceSnapshot, ...]
    database: DatabaseDumpMetadata


@dataclass(frozen=True)
class BackupVerification:
    """Successful verification result used as a retention-cleanup prerequisite."""

    backup_directory: Path
    backup_id: str
    created_at: datetime
    manifest_sha256: str
    evidence_count: int
    database_sha256: str
    database_size_bytes: int
    schema_revision: str
    source_system_identifier: str
    evidence: tuple[EvidenceSnapshot, ...]


def create_stopped_backup(
    backup_root: Path,
    request: BackupRequest,
    dump_adapter: DatabaseDumpAdapter,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    max_evidence_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES,
    max_database_dump_bytes: int = DEFAULT_MAX_DATABASE_DUMP_BYTES,
    publisher: BackupPublisher | None = None,
) -> BackupVerification:
    """Create a ``.partial`` backup and publish it only after full verification.

    Any failure deliberately leaves the partial directory in place for investigation.
    Callers must never treat a partial directory as a restorable backup.
    """
    _validate_request(request, max_evidence_bytes, max_database_dump_bytes)
    if not request.precondition.service_stopped:
        raise BackupError("BACKUP_SERVICE_NOT_STOPPED")
    if request.precondition.active_business_connections != 0:
        raise BackupError("BACKUP_ACTIVE_CONNECTIONS")

    try:
        root = ensure_directory_in_root(backup_root, backup_root)
    except PathSecurityError as exc:
        raise BackupError("BACKUP_STORAGE_UNAVAILABLE") from exc
    partial = root / f"{request.backup_id}.partial"
    complete = root / f"{request.backup_id}.complete"
    if complete.exists():
        raise BackupError("BACKUP_ALREADY_EXISTS")
    _create_partial_directory(partial, root)

    try:
        evidence_entries = _copy_evidence_snapshot(
            partial,
            request.evidence_root,
            request.evidence,
            max_evidence_bytes=max_evidence_bytes,
        )
        database_entry = _create_database_archive(
            partial,
            request.database,
            dump_adapter,
            max_database_dump_bytes=max_database_dump_bytes,
        )
        created_at = _utc_clock(clock)
        manifest = _canonical_json(
            {
                "backup_id": request.backup_id,
                "created_at": _format_datetime(created_at),
                "database": database_entry,
                "evidence": evidence_entries,
                "format_version": 1,
                "purpose": request.purpose,
                "status": "complete",
            }
        )
        write_new_regular_file_in_root(
            partial / _MANIFEST_NAME,
            root,
            manifest,
            max_bytes=MAX_MANIFEST_BYTES,
        )
        manifest_digest = hashlib.sha256(manifest).hexdigest()
        write_new_regular_file_in_root(
            partial / _MANIFEST_DIGEST_NAME,
            root,
            f"{manifest_digest}\n".encode("ascii"),
            max_bytes=65,
        )
        _flush_backup_tree(partial, root)
        verify_backup(
            root,
            partial,
            max_evidence_bytes=max_evidence_bytes,
            max_database_dump_bytes=max_database_dump_bytes,
            allow_partial=True,
        )
        if publisher is None:
            _publish_partial(partial, complete, root)
        else:
            publisher.publish(partial, complete, root)
        return verify_backup(
            root,
            complete,
            max_evidence_bytes=max_evidence_bytes,
            max_database_dump_bytes=max_database_dump_bytes,
        )
    except BackupError:
        raise
    except PathSecurityError as exc:
        raise BackupError("BACKUP_STORAGE_UNAVAILABLE") from exc


def verify_backup(
    backup_root: Path,
    backup_directory: Path,
    *,
    max_evidence_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES,
    max_database_dump_bytes: int = DEFAULT_MAX_DATABASE_DUMP_BYTES,
    allow_partial: bool = False,
) -> BackupVerification:
    """Verify every stored byte and the canonical manifest without restoring it."""
    if max_evidence_bytes <= 0 or max_database_dump_bytes <= 0:
        raise BackupError("BACKUP_SIZE_LIMIT_INVALID")
    root = _existing_root(backup_root)
    directory = _existing_backup_directory(backup_directory, root)
    is_partial = directory.name.endswith(".partial")
    if is_partial and not allow_partial:
        raise BackupError("BACKUP_NOT_COMPLETE")
    if not (is_partial or directory.name.endswith(".complete")):
        raise BackupError("BACKUP_DIRECTORY_NAME_INVALID")

    manifest_path, manifest_bytes = _read_backup_file(
        directory / _MANIFEST_NAME, root, MAX_MANIFEST_BYTES
    )
    del manifest_path
    _, digest_bytes = _read_backup_file(directory / _MANIFEST_DIGEST_NAME, root, 65)
    try:
        expected_manifest_digest = digest_bytes.decode("ascii", errors="strict").strip()
    except UnicodeDecodeError as exc:
        raise BackupError("BACKUP_MANIFEST_INVALID") from exc
    if not _SHA256_PATTERN.fullmatch(expected_manifest_digest):
        raise BackupError("BACKUP_MANIFEST_INVALID")
    actual_manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
    if actual_manifest_digest != expected_manifest_digest:
        raise BackupError("BACKUP_MANIFEST_HASH_MISMATCH")
    manifest = _parse_manifest(manifest_bytes)
    _assert_manifest_matches_directory(manifest, directory, is_partial)

    database = manifest["database"]
    database_digest, database_size = _hash_manifest_content(
        directory,
        root,
        database["path"],
        max_database_dump_bytes,
        "BACKUP_DATABASE_ARCHIVE_INVALID",
    )
    if database_digest != database["sha256"]:
        raise BackupError("BACKUP_DATABASE_ARCHIVE_HASH_MISMATCH")
    if database_size != database["size_bytes"]:
        raise BackupError("BACKUP_DATABASE_ARCHIVE_HASH_MISMATCH")

    evidence = manifest["evidence"]
    for entry in evidence:
        digest, size = _hash_manifest_content(
            directory,
            root,
            entry["path"],
            max_evidence_bytes,
            "BACKUP_EVIDENCE_COPY_UNAVAILABLE",
        )
        if size != entry["size_bytes"]:
            raise BackupError("BACKUP_EVIDENCE_COPY_MISMATCH")
        if digest != entry["sha256"]:
            raise BackupError("BACKUP_EVIDENCE_COPY_MISMATCH")

    return BackupVerification(
        backup_directory=directory,
        backup_id=manifest["backup_id"],
        created_at=_parse_datetime(manifest["created_at"]),
        manifest_sha256=actual_manifest_digest,
        evidence_count=len(evidence),
        database_sha256=database["sha256"],
        database_size_bytes=database["size_bytes"],
        schema_revision=database["schema_revision"],
        source_system_identifier=database["source_system_identifier"],
        evidence=tuple(
            EvidenceSnapshot(
                evidence_id=entry["evidence_id"],
                sha256=entry["sha256"],
                size_bytes=entry["size_bytes"],
                storage_path=directory / entry["path"],
            )
            for entry in evidence
        ),
    )


def select_retention_prune_candidates(
    backup_root: Path,
    newest_backup_directory: Path,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    retention_days: int = 30,
    max_evidence_bytes: int = DEFAULT_MAX_EVIDENCE_BYTES,
    max_database_dump_bytes: int = DEFAULT_MAX_DATABASE_DUMP_BYTES,
) -> tuple[Path, ...]:
    """Return only valid, old complete backups eligible for an external delete step.

    This function *never* deletes.  It first verifies the newly-created complete
    backup, then independently verifies each candidate.  Partial and corrupt backups
    are intentionally excluded rather than silently removed.
    """
    if retention_days <= 0:
        raise BackupError("BACKUP_RETENTION_DAYS_INVALID")
    root = _existing_root(backup_root)
    newest = verify_backup(
        root,
        newest_backup_directory,
        max_evidence_bytes=max_evidence_bytes,
        max_database_dump_bytes=max_database_dump_bytes,
    )
    cutoff = _utc_clock(clock) - timedelta(days=retention_days)
    candidates: list[tuple[datetime, Path]] = []
    for item in sorted(root.iterdir(), key=lambda path: path.name):
        if item == newest.backup_directory or not item.name.endswith(".complete"):
            continue
        try:
            verified = verify_backup(
                root,
                item,
                max_evidence_bytes=max_evidence_bytes,
                max_database_dump_bytes=max_database_dump_bytes,
            )
        except (BackupError, PathSecurityError):
            continue
        if verified.created_at < cutoff:
            candidates.append((verified.created_at, verified.backup_directory))
    return tuple(path for _, path in sorted(candidates, key=lambda pair: (pair[0], pair[1].name)))


def _validate_request(
    request: BackupRequest,
    max_evidence_bytes: int,
    max_database_dump_bytes: int,
) -> None:
    if not _BACKUP_ID_PATTERN.fullmatch(request.backup_id):
        raise BackupError("BACKUP_ID_INVALID")
    if request.purpose not in {"daily", "pre_upgrade"}:
        raise BackupError("BACKUP_PURPOSE_INVALID")
    if max_evidence_bytes <= 0 or max_database_dump_bytes <= 0:
        raise BackupError("BACKUP_SIZE_LIMIT_INVALID")
    if request.precondition.active_business_connections < 0:
        raise BackupError("BACKUP_ACTIVE_CONNECTIONS_INVALID")
    if (
        request.database.archive_format != "pg_dump_custom"
        or not _REVISION_PATTERN.fullmatch(request.database.schema_revision)
        or not _SYSTEM_IDENTIFIER_PATTERN.fullmatch(
            request.database.source_system_identifier
        )
        or int(request.database.source_system_identifier) > 2**64 - 1
    ):
        raise BackupError("BACKUP_DATABASE_METADATA_INVALID")

    seen_ids: set[str] = set()
    seen_hashes: set[str] = set()
    for evidence in request.evidence:
        if (
            not _is_canonical_uuid(evidence.evidence_id)
            or evidence.evidence_id in seen_ids
            or not _SHA256_PATTERN.fullmatch(evidence.sha256)
            or evidence.size_bytes < 0
            or evidence.size_bytes > max_evidence_bytes
        ):
            raise BackupError("BACKUP_EVIDENCE_SNAPSHOT_INVALID")
        if evidence.sha256 in seen_hashes:
            raise BackupError("BACKUP_EVIDENCE_SNAPSHOT_DUPLICATE")
        seen_ids.add(evidence.evidence_id)
        seen_hashes.add(evidence.sha256)


def _copy_evidence_snapshot(
    partial: Path,
    evidence_root: Path,
    evidence: tuple[EvidenceSnapshot, ...],
    *,
    max_evidence_bytes: int,
) -> list[dict[str, object]]:
    entries: list[dict[str, object]] = []
    try:
        destination_root = ensure_directory_in_root(partial / "evidence", partial)
    except PathSecurityError as exc:
        raise BackupError("BACKUP_EVIDENCE_COPY_UNAVAILABLE") from exc
    try:
        source_root = _existing_root(evidence_root)
    except BackupError as exc:
        raise BackupError("BACKUP_EVIDENCE_ROOT_UNAVAILABLE") from exc
    for snapshot in sorted(evidence, key=lambda item: (item.evidence_id, item.sha256)):
        try:
            _, source_digest, source_size = _hash_regular_file_in_root(
                snapshot.storage_path, source_root, max_evidence_bytes
            )
        except PathSecurityError as exc:
            raise BackupError("BACKUP_EVIDENCE_SOURCE_UNAVAILABLE") from exc
        if source_size != snapshot.size_bytes or source_digest != snapshot.sha256:
            raise BackupError("BACKUP_EVIDENCE_SOURCE_CHANGED")
        destination = destination_root / snapshot.sha256
        try:
            copied_digest, copied_size = _copy_regular_file_in_root(
                snapshot.storage_path,
                source_root,
                destination,
                partial,
                max_evidence_bytes,
            )
        except PathSecurityError as exc:
            raise BackupError("BACKUP_EVIDENCE_COPY_UNAVAILABLE") from exc
        if copied_size != snapshot.size_bytes or copied_digest != snapshot.sha256:
            raise BackupError("BACKUP_EVIDENCE_COPY_MISMATCH")
        entries.append(
            {
                "evidence_id": snapshot.evidence_id,
                "path": f"evidence/{snapshot.sha256}",
                "sha256": snapshot.sha256,
                "size_bytes": snapshot.size_bytes,
            }
        )
    return entries


def _create_database_archive(
    partial: Path,
    metadata: DatabaseDumpMetadata,
    dump_adapter: DatabaseDumpAdapter,
    *,
    max_database_dump_bytes: int,
) -> dict[str, object]:
    destination = partial / _DATABASE_ARCHIVE_NAME
    try:
        dump_adapter.dump(destination, metadata)
    except BackupError:
        raise
    except Exception as exc:
        raise BackupError("BACKUP_DATABASE_DUMP_FAILED") from exc
    try:
        _, archive_digest, archive_size = _hash_regular_file_in_root(
            destination, partial, max_database_dump_bytes
        )
        _fsync_regular_file_in_root(destination, partial)
        contents = dump_adapter.list_archive(destination)
    except PathSecurityError as exc:
        raise BackupError("BACKUP_DATABASE_ARCHIVE_INVALID") from exc
    except Exception as exc:
        raise BackupError("BACKUP_DATABASE_ARCHIVE_INVALID") from exc
    if not contents:
        raise BackupError("BACKUP_DATABASE_ARCHIVE_INVALID")
    try:
        _, archive_digest_after_list, archive_size_after_list = _hash_regular_file_in_root(
            destination, partial, max_database_dump_bytes
        )
    except PathSecurityError as exc:
        raise BackupError("BACKUP_DATABASE_ARCHIVE_INVALID") from exc
    if (archive_digest, archive_size) != (archive_digest_after_list, archive_size_after_list):
        raise BackupError("BACKUP_DATABASE_ARCHIVE_CHANGED")
    return {
        "archive_format": metadata.archive_format,
        "path": _DATABASE_ARCHIVE_NAME,
        "schema_revision": metadata.schema_revision,
        "source_system_identifier": metadata.source_system_identifier,
        "sha256": archive_digest,
        "size_bytes": archive_size,
    }


def _create_partial_directory(partial: Path, root: Path) -> None:
    if partial.exists():
        raise BackupError("BACKUP_PARTIAL_ALREADY_EXISTS")
    try:
        created = ensure_directory_in_root(partial, root)
    except PathSecurityError as exc:
        raise BackupError("BACKUP_STORAGE_UNAVAILABLE") from exc
    if created != partial.resolve(strict=True):
        raise BackupError("BACKUP_STORAGE_UNAVAILABLE")


def _publish_partial(partial: Path, complete: Path, root: Path) -> None:
    try:
        _existing_backup_directory(partial, root)
        if complete.exists():
            raise BackupError("BACKUP_ALREADY_EXISTS")
        _flush_backup_tree(partial, root)
        os.replace(partial, complete)
        _existing_backup_directory(complete, root)
        _flush_directory(root)
    except BackupError:
        raise
    except OSError as exc:
        raise BackupError("BACKUP_PUBLISH_FAILED") from exc


def _flush_backup_tree(partial: Path, root: Path) -> None:
    """Persist file contents first, then their containing directories where supported."""
    evidence_directory = partial / "evidence"
    if evidence_directory.exists():
        _flush_directory(evidence_directory)
    _flush_directory(partial)
    _flush_directory(root)


def _flush_directory(directory: Path) -> None:
    """Use directory fsync where Python exposes it; Windows rename durability is wrapper-owned."""
    if os.name == "nt":
        # Python cannot open a directory handle suitable for FlushFileBuffers on all
        # supported Windows filesystems.  The production Windows wrapper must use a
        # write-through move on the encrypted removable volume after this core returns.
        return
    try:
        descriptor = os.open(directory, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise BackupError("BACKUP_STORAGE_UNAVAILABLE") from exc


def _existing_root(root: Path) -> Path:
    try:
        candidate = Path(os.path.abspath(os.fspath(root)))
        if not candidate.is_dir() or candidate.is_symlink():
            raise BackupError("BACKUP_ROOT_UNAVAILABLE")
        return candidate.resolve(strict=True)
    except OSError as exc:
        raise BackupError("BACKUP_ROOT_UNAVAILABLE") from exc


def _existing_backup_directory(directory: Path, root: Path) -> Path:
    candidate = Path(os.path.abspath(os.fspath(directory)))
    try:
        candidate.relative_to(root)
        if candidate.parent != root:
            raise BackupError("BACKUP_DIRECTORY_NOT_ALLOWED")
        if not candidate.is_dir() or candidate.is_symlink():
            raise BackupError("BACKUP_DIRECTORY_INVALID")
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(root)
        return resolved
    except OSError as exc:
        raise BackupError("BACKUP_DIRECTORY_INVALID") from exc
    except ValueError as exc:
        raise BackupError("BACKUP_DIRECTORY_NOT_ALLOWED") from exc


def _read_backup_file(path: Path, root: Path, max_bytes: int) -> tuple[Path, bytes]:
    try:
        return read_regular_file_in_root(path, root, max_bytes=max_bytes)
    except PathSecurityError as exc:
        raise BackupError("BACKUP_MANIFEST_INVALID") from exc


def _hash_manifest_content(
    directory: Path,
    root: Path,
    relative_path: object,
    max_bytes: int,
    error_code: str,
) -> tuple[str, int]:
    if not isinstance(relative_path, str) or not relative_path or Path(relative_path).is_absolute():
        raise BackupError(error_code)
    path = directory / relative_path
    try:
        resolved, digest, size = _hash_regular_file_in_root(path, root, max_bytes)
        resolved.relative_to(directory)
    except (PathSecurityError, ValueError) as exc:
        raise BackupError(error_code) from exc
    return digest, size


def _hash_regular_file_in_root(path: Path, root: Path, max_bytes: int) -> tuple[Path, str, int]:
    """Hash a stable regular file in chunks, without materializing it in memory."""
    digest, size = _stream_regular_file_in_root(path, root, max_bytes, on_chunk=None)
    return path_security._absolute_without_resolving(path), digest, size


def _copy_regular_file_in_root(
    source: Path,
    source_root: Path,
    destination: Path,
    destination_root: Path,
    max_bytes: int,
) -> tuple[str, int]:
    """Copy through pinned file handles, then hash the written destination again."""
    destination_root, resolved_root, root_stat = path_security._pin_existing_root(destination_root)
    candidate = path_security._absolute_without_resolving(destination)
    try:
        candidate.relative_to(destination_root)
        parent = candidate.parent
        parent.resolve(strict=True).relative_to(resolved_root)
    except (OSError, ValueError) as exc:
        raise PathSecurityError("STORAGE_PATH_NOT_ALLOWED") from exc
    path_security._reject_reparse_points(candidate)
    flags = (
        os.O_WRONLY
        | os.O_CREAT
        | os.O_EXCL
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    try:
        descriptor = os.open(candidate, flags, 0o600)
    except OSError as exc:
        raise PathSecurityError("STORAGE_FILE_UNAVAILABLE") from exc
    try:
        opened = os.fstat(descriptor)
        path_security._assert_open_file_still_in_root(
            candidate, destination_root, resolved_root, root_stat, opened
        )
        with os.fdopen(descriptor, "wb", closefd=True) as output:
            descriptor = -1

            def write_chunk(chunk: bytes) -> None:
                output.write(chunk)

            source_digest, source_size = _stream_regular_file_in_root(
                source, source_root, max_bytes, on_chunk=write_chunk
            )
            output.flush()
            os.fsync(output.fileno())
            after_write = os.fstat(output.fileno())
        path_security._assert_open_file_still_in_root(
            candidate, destination_root, resolved_root, root_stat, after_write
        )
        if not path_security._same_file(opened, after_write) or after_write.st_size != source_size:
            raise PathSecurityError("STORAGE_FILE_CHANGED_DURING_WRITE")
        _, copied_digest, copied_size = _hash_regular_file_in_root(
            candidate, destination_root, max_bytes
        )
        if (copied_digest, copied_size) != (source_digest, source_size):
            raise PathSecurityError("STORAGE_FILE_CHANGED_DURING_WRITE")
        return copied_digest, copied_size
    except OSError as exc:
        raise PathSecurityError("STORAGE_FILE_UNAVAILABLE") from exc
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _stream_regular_file_in_root(
    path: Path,
    root: Path,
    max_bytes: int,
    *,
    on_chunk: Callable[[bytes], None] | None,
) -> tuple[str, int]:
    """Read a path through one pinned descriptor and return its SHA-256 and size."""
    if max_bytes <= 0:
        raise PathSecurityError("FILE_TOO_LARGE")
    allowed_root, resolved_root, root_stat = path_security._pin_existing_root(root)
    candidate = path_security._absolute_without_resolving(path)
    try:
        candidate.relative_to(allowed_root)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    except ValueError as exc:
        raise PathSecurityError("FILE_PATH_NOT_ALLOWED") from exc
    path_security._reject_reparse_points(candidate)
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(resolved, flags)
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    try:
        opened = os.fstat(descriptor)
        path_security._assert_open_file_still_in_root(
            candidate, allowed_root, resolved_root, root_stat, opened
        )
        if not stat.S_ISREG(opened.st_mode):
            raise PathSecurityError("FILE_NOT_REGULAR")
        digest = hashlib.sha256()
        size = 0
        while True:
            chunk = os.read(descriptor, min(1024 * 1024, max_bytes - size + 1))
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                raise PathSecurityError("FILE_TOO_LARGE")
            digest.update(chunk)
            if on_chunk is not None:
                on_chunk(chunk)
        after_read = os.fstat(descriptor)
        path_security._assert_open_file_still_in_root(
            candidate, allowed_root, resolved_root, root_stat, after_read
        )
        if not path_security._same_file(opened, after_read) or opened.st_size != after_read.st_size:
            raise PathSecurityError("FILE_CHANGED_DURING_READ")
        return digest.hexdigest(), size
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    finally:
        os.close(descriptor)


def _resolved_regular_file_in_root(path: Path, root: Path) -> Path:
    """Return only after applying the same root/reparse checks as streamed reads."""
    allowed_root, resolved_root, _ = path_security._pin_existing_root(root)
    candidate = path_security._absolute_without_resolving(path)
    try:
        candidate.relative_to(allowed_root)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    except ValueError as exc:
        raise PathSecurityError("FILE_PATH_NOT_ALLOWED") from exc
    path_security._reject_reparse_points(candidate)
    return resolved


def _fsync_regular_file_in_root(path: Path, root: Path) -> None:
    """Flush an adapter-produced archive after first pinning its secure path."""
    allowed_root, resolved_root, root_stat = path_security._pin_existing_root(root)
    candidate = path_security._absolute_without_resolving(path)
    try:
        candidate.relative_to(allowed_root)
        resolved = candidate.resolve(strict=True)
        resolved.relative_to(resolved_root)
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc
    except ValueError as exc:
        raise PathSecurityError("FILE_PATH_NOT_ALLOWED") from exc
    path_security._reject_reparse_points(candidate)
    try:
        descriptor = os.open(resolved, os.O_RDWR | getattr(os, "O_BINARY", 0))
        try:
            opened = os.fstat(descriptor)
            path_security._assert_open_file_still_in_root(
                candidate, allowed_root, resolved_root, root_stat, opened
            )
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    except OSError as exc:
        raise PathSecurityError("FILE_UNAVAILABLE") from exc


def _parse_manifest(content: bytes) -> dict[str, object]:
    try:
        parsed = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise BackupError("BACKUP_MANIFEST_INVALID") from exc
    if not isinstance(parsed, dict) or _canonical_json(parsed) != content:
        raise BackupError("BACKUP_MANIFEST_INVALID")
    required = {
        "backup_id",
        "created_at",
        "database",
        "evidence",
        "format_version",
        "purpose",
        "status",
    }
    if set(parsed) != required:
        raise BackupError("BACKUP_MANIFEST_INVALID")
    if (
        parsed["format_version"] != 1
        or parsed["purpose"] not in {"daily", "pre_upgrade"}
        or parsed["status"] != "complete"
        or not isinstance(parsed["backup_id"], str)
        or not _BACKUP_ID_PATTERN.fullmatch(parsed["backup_id"])
        or not isinstance(parsed["created_at"], str)
        or not isinstance(parsed["database"], dict)
        or not isinstance(parsed["evidence"], list)
    ):
        raise BackupError("BACKUP_MANIFEST_INVALID")
    _parse_datetime(parsed["created_at"])
    _validate_database_manifest(parsed["database"])
    _validate_evidence_manifest(parsed["evidence"])
    return parsed


def _validate_database_manifest(database: dict[object, object]) -> None:
    required = {
        "archive_format",
        "path",
        "schema_revision",
        "sha256",
        "size_bytes",
        "source_system_identifier",
    }
    if (
        set(database) != required
        or database["archive_format"] != "pg_dump_custom"
        or database["path"] != _DATABASE_ARCHIVE_NAME
        or not isinstance(database["schema_revision"], str)
        or not _REVISION_PATTERN.fullmatch(database["schema_revision"])
        or not isinstance(database["source_system_identifier"], str)
        or not _SYSTEM_IDENTIFIER_PATTERN.fullmatch(
            database["source_system_identifier"]
        )
        or int(database["source_system_identifier"]) > 2**64 - 1
        or not isinstance(database["sha256"], str)
        or not _SHA256_PATTERN.fullmatch(database["sha256"])
        or not isinstance(database["size_bytes"], int)
        or database["size_bytes"] < 0
    ):
        raise BackupError("BACKUP_MANIFEST_INVALID")


def _validate_evidence_manifest(evidence: list[object]) -> None:
    previous: tuple[str, str] | None = None
    seen_hashes: set[str] = set()
    for entry in evidence:
        if not isinstance(entry, dict) or set(entry) != {
            "evidence_id",
            "path",
            "sha256",
            "size_bytes",
        }:
            raise BackupError("BACKUP_MANIFEST_INVALID")
        evidence_id = entry["evidence_id"]
        sha256 = entry["sha256"]
        if (
            not isinstance(evidence_id, str)
            or not _is_canonical_uuid(evidence_id)
            or not isinstance(sha256, str)
            or not _SHA256_PATTERN.fullmatch(sha256)
            or entry["path"] != f"evidence/{sha256}"
            or not isinstance(entry["size_bytes"], int)
            or entry["size_bytes"] < 0
            or sha256 in seen_hashes
        ):
            raise BackupError("BACKUP_MANIFEST_INVALID")
        key = (evidence_id, sha256)
        if previous is not None and key <= previous:
            raise BackupError("BACKUP_MANIFEST_INVALID")
        previous = key
        seen_hashes.add(sha256)


def _assert_manifest_matches_directory(
    manifest: dict[str, object], directory: Path, is_partial: bool
) -> None:
    suffix = ".partial" if is_partial else ".complete"
    if directory.name != f"{manifest['backup_id']}{suffix}":
        raise BackupError("BACKUP_MANIFEST_DIRECTORY_MISMATCH")


def _canonical_json(value: object) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")


def _utc_clock(clock: Callable[[], datetime]) -> datetime:
    value = clock()
    if value.tzinfo is None:
        raise BackupError("BACKUP_CLOCK_INVALID")
    return value.astimezone(UTC).replace(microsecond=0)


def _format_datetime(value: datetime) -> str:
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise BackupError("BACKUP_MANIFEST_INVALID") from exc
    if parsed.tzinfo is None or _format_datetime(parsed) != value:
        raise BackupError("BACKUP_MANIFEST_INVALID")
    return parsed.astimezone(UTC)


def _is_canonical_uuid(value: object) -> bool:
    if not isinstance(value, str):
        return False
    try:
        return str(uuid.UUID(value)) == value
    except ValueError:
        return False
