from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import pytest

from ai_accounting import backup
from ai_accounting.backup import (
    BackupError,
    BackupPrecondition,
    BackupRequest,
    DatabaseDumpMetadata,
    EvidenceSnapshot,
    create_stopped_backup,
    select_retention_prune_candidates,
    verify_backup,
)

_SOURCE_SYSTEM_IDENTIFIER = "7612345678901234567"


class FakeDumpAdapter:
    def __init__(self, archive: bytes = b"PGDMP\x00data", *, valid: bool = True) -> None:
        self.archive = archive
        self.valid = valid

    def dump(self, destination: Path, metadata: DatabaseDumpMetadata) -> None:
        assert metadata.archive_format == "pg_dump_custom"
        destination.write_bytes(self.archive)

    def list_archive(self, archive: Path) -> tuple[str, ...]:
        assert archive.read_bytes() == self.archive
        return ("TABLE public.organizations",) if self.valid else ()


def _clock(day: int) -> datetime:
    return datetime(2026, 8, day, 4, 5, 6, tzinfo=UTC)


def _request(
    evidence_root: Path, source: Path, *, backup_id: str = "backup-20260811"
) -> BackupRequest:
    content = source.read_bytes()
    return BackupRequest(
        backup_id=backup_id,
        purpose="daily",
        precondition=BackupPrecondition(service_stopped=True, active_business_connections=0),
        evidence_root=evidence_root,
        evidence=(
            EvidenceSnapshot(
                evidence_id="0c6f0e54-0556-45ba-b029-1c07dd30d617",
                sha256=hashlib.sha256(content).hexdigest(),
                size_bytes=len(content),
                storage_path=source,
            ),
        ),
        database=DatabaseDumpMetadata(
            schema_revision="0002_pilot_events",
            source_system_identifier=_SOURCE_SYSTEM_IDENTIFIER,
        ),
    )


def _source(tmp_path: Path, content: bytes = b"invoice-bytes") -> tuple[Path, Path]:
    evidence_root = tmp_path / "evidence-store"
    source = evidence_root / "ab" / "invoice.bin"
    source.parent.mkdir(parents=True)
    source.write_bytes(content)
    return evidence_root, source


def test_backup_is_deterministic_complete_and_does_not_leak_source_paths_or_secrets(
    tmp_path: Path,
) -> None:
    evidence_root, source = _source(tmp_path)
    request = _request(evidence_root, source)
    first = create_stopped_backup(
        tmp_path / "media-one", request, FakeDumpAdapter(), clock=lambda: _clock(11)
    )
    second = create_stopped_backup(
        tmp_path / "media-two", request, FakeDumpAdapter(), clock=lambda: _clock(11)
    )

    assert first.backup_directory.name == "backup-20260811.complete"
    assert not (tmp_path / "media-one" / "backup-20260811.partial").exists()
    first_manifest = (first.backup_directory / "manifest.json").read_bytes()
    assert first_manifest == (second.backup_directory / "manifest.json").read_bytes()
    assert first.manifest_sha256 == second.manifest_sha256
    assert first.source_system_identifier == _SOURCE_SYSTEM_IDENTIFIER
    text = first_manifest.decode("utf-8")
    assert str(source) not in text
    assert str(evidence_root) not in text
    assert "postgresql" not in text
    assert "finance_backup" not in text
    assert verify_backup(tmp_path / "media-one", first.backup_directory) == first


def test_backup_rejects_nonstopped_service_before_creating_a_partial_directory(
    tmp_path: Path,
) -> None:
    evidence_root, source = _source(tmp_path)
    request = replace(
        _request(evidence_root, source),
        precondition=BackupPrecondition(service_stopped=False, active_business_connections=0),
    )

    with pytest.raises(BackupError, match="BACKUP_SERVICE_NOT_STOPPED"):
        create_stopped_backup(tmp_path / "media", request, FakeDumpAdapter())
    assert not (tmp_path / "media" / "backup-20260811.partial").exists()


def test_backup_rejects_missing_tampered_and_path_escaping_evidence(tmp_path: Path) -> None:
    evidence_root, source = _source(tmp_path)
    request = _request(evidence_root, source)
    source.unlink()
    with pytest.raises(BackupError, match="BACKUP_EVIDENCE_SOURCE_UNAVAILABLE"):
        create_stopped_backup(tmp_path / "missing", request, FakeDumpAdapter())

    evidence_root, source = _source(tmp_path / "tampered")
    request = _request(evidence_root, source)
    source.write_bytes(b"changed")
    with pytest.raises(BackupError, match="BACKUP_EVIDENCE_SOURCE_CHANGED"):
        create_stopped_backup(tmp_path / "tampered-media", request, FakeDumpAdapter())

    outside = tmp_path / "outside.bin"
    outside.write_bytes(b"invoice-bytes")
    escaped = replace(
        _request(evidence_root, source),
        evidence=(replace(request.evidence[0], storage_path=outside),),
    )
    with pytest.raises(BackupError, match="BACKUP_EVIDENCE_SOURCE_UNAVAILABLE"):
        create_stopped_backup(tmp_path / "escape-media", escaped, FakeDumpAdapter())


def test_backup_rejects_copy_that_changes_after_safe_write(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    evidence_root, source = _source(tmp_path)
    request = _request(evidence_root, source)
    original_copy = backup._copy_regular_file_in_root

    def altered_copy(*args: object, **kwargs: object) -> tuple[str, int]:
        digest, size = original_copy(*args, **kwargs)  # type: ignore[arg-type]
        return "0" * 64, size

    monkeypatch.setattr(backup, "_copy_regular_file_in_root", altered_copy)
    with pytest.raises(BackupError, match="BACKUP_EVIDENCE_COPY_MISMATCH"):
        create_stopped_backup(tmp_path / "media", request, FakeDumpAdapter())
    assert (tmp_path / "media" / "backup-20260811.partial").is_dir()


def test_verify_rejects_manifest_corruption_and_database_tampering(tmp_path: Path) -> None:
    evidence_root, source = _source(tmp_path)
    result = create_stopped_backup(
        tmp_path / "media",
        _request(evidence_root, source),
        FakeDumpAdapter(),
        clock=lambda: _clock(11),
    )
    (result.backup_directory / "manifest.json").write_bytes(b"not-json")
    with pytest.raises(BackupError, match="BACKUP_MANIFEST_HASH_MISMATCH"):
        verify_backup(tmp_path / "media", result.backup_directory)

    result = create_stopped_backup(
        tmp_path / "media-two",
        _request(evidence_root, source),
        FakeDumpAdapter(),
        clock=lambda: _clock(11),
    )
    (result.backup_directory / "database.dump").write_bytes(b"tampered")
    with pytest.raises(BackupError, match="BACKUP_DATABASE_ARCHIVE_HASH_MISMATCH"):
        verify_backup(tmp_path / "media-two", result.backup_directory)


def test_retention_only_returns_verified_old_complete_backups_after_new_success(
    tmp_path: Path,
) -> None:
    evidence_root, source = _source(tmp_path)
    root = tmp_path / "media"
    old = create_stopped_backup(
        root,
        _request(evidence_root, source, backup_id="old-valid"),
        FakeDumpAdapter(),
        clock=lambda: _clock(1),
    )
    corrupted = create_stopped_backup(
        root,
        _request(evidence_root, source, backup_id="old-corrupt"),
        FakeDumpAdapter(),
        clock=lambda: _clock(2),
    )
    (corrupted.backup_directory / "database.dump").write_bytes(b"tampered")
    (root / "manual.partial").mkdir()
    newest = create_stopped_backup(
        root,
        _request(evidence_root, source, backup_id="newest"),
        FakeDumpAdapter(),
        clock=lambda: _clock(31),
    )

    assert select_retention_prune_candidates(
        root,
        newest.backup_directory,
        clock=lambda: datetime(2026, 9, 1, 4, 5, 6, tzinfo=UTC),
    ) == (old.backup_directory,)
    assert corrupted.backup_directory.exists()
    assert (root / "manual.partial").exists()


def test_retention_refuses_unverified_or_partial_new_backup(tmp_path: Path) -> None:
    evidence_root, source = _source(tmp_path)
    root = tmp_path / "media"
    (root / "unverified.partial").mkdir(parents=True)
    with pytest.raises(BackupError, match="BACKUP_NOT_COMPLETE"):
        select_retention_prune_candidates(
            root, root / "unverified.partial", clock=lambda: _clock(31)
        )

    valid = create_stopped_backup(
        root,
        _request(evidence_root, source, backup_id="valid"),
        FakeDumpAdapter(),
        clock=lambda: _clock(31),
    )
    (valid.backup_directory / "manifest.sha256").write_bytes(("0" * 64 + "\n").encode("ascii"))
    with pytest.raises(BackupError, match="BACKUP_MANIFEST_HASH_MISMATCH"):
        select_retention_prune_candidates(root, valid.backup_directory, clock=lambda: _clock(31))


def test_dump_archive_must_be_listable_and_partial_is_never_published(tmp_path: Path) -> None:
    evidence_root, source = _source(tmp_path)
    with pytest.raises(BackupError, match="BACKUP_DATABASE_ARCHIVE_INVALID"):
        create_stopped_backup(
            tmp_path / "media",
            _request(evidence_root, source),
            FakeDumpAdapter(valid=False),
        )
    assert (tmp_path / "media" / "backup-20260811.partial").is_dir()


def test_manifest_path_escape_is_rejected_even_with_a_matching_sidecar(tmp_path: Path) -> None:
    evidence_root, source = _source(tmp_path)
    root = tmp_path / "media"
    result = create_stopped_backup(root, _request(evidence_root, source), FakeDumpAdapter())
    manifest_path = result.backup_directory / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["evidence"][0]["path"] = "../database.dump"
    content = json.dumps(
        manifest, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    manifest_path.write_bytes(content)
    (result.backup_directory / "manifest.sha256").write_text(
        hashlib.sha256(content).hexdigest() + "\n", encoding="ascii"
    )

    with pytest.raises(BackupError, match="BACKUP_MANIFEST_INVALID"):
        verify_backup(root, result.backup_directory)
