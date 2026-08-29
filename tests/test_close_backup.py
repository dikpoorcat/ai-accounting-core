from __future__ import annotations

import os
import uuid
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from ai_accounting.backup import BackupError, PortableBackupVerification
from ai_accounting.close_backup import CloseBackupError, CloseBackupRuntime, CloseBackupService
from ai_accounting.company_schemas import ConfigureCloseBackupRequest
from ai_accounting.config import Settings
from ai_accounting.database import Base
from ai_accounting.identity import ExecutionContext, ExecutorKind
from ai_accounting.models import (
    AccountingPeriodCloseBackup,
    CatalogMetadata,
    CloseBackupLocationVersion,
    CompanyRegistry,
)


class _Publisher:
    def durable_directory_preflight(self, root: Path) -> None:
        assert root.is_dir()


class _ReplacingPublisher:
    def durable_directory_preflight(self, root: Path) -> None:
        assert root.is_dir()

    def replace_file(self, replacement: Path, current: Path, root: Path) -> None:
        assert replacement.parent == root
        assert current.parent == root
        os.replace(replacement, current)


def _context() -> ExecutionContext:
    return ExecutionContext(
        org_id=uuid.uuid4(),
        owner_account_id=uuid.uuid4(),
        owner_session_id=uuid.uuid4(),
        owner_credential_version=3,
        executor_kind=ExecutorKind.AI_AGENT,
        executor_name="close-backup-test",
        executor_version="1.0.0",
        request_correlation_id=uuid.uuid4(),
        catalog_instance_id=uuid.uuid4(),
    )


def _settings(tmp_path: Path) -> Settings:
    storage = tmp_path / "storage"
    evidence = storage / "evidence"
    storage.mkdir()
    evidence.mkdir()
    return Settings(
        finance_environment="development",
        database_url="postgresql+psycopg://runtime:secret@localhost/finance_catalog",
        finance_company_database_url=(
            "postgresql+psycopg://runtime:secret@localhost/finance"
        ),
        finance_storage_dir=storage,
        finance_evidence_dir=evidence,
    )


@pytest.fixture
def catalog_session(tmp_path: Path) -> Session:
    engine = create_engine(f"sqlite+pysqlite:///{tmp_path / 'catalog.db'}")
    Base.metadata.create_all(
        engine,
        tables=[
            CatalogMetadata.__table__,
            CompanyRegistry.__table__,
            CloseBackupLocationVersion.__table__,
            AccountingPeriodCloseBackup.__table__,
        ],
    )
    with Session(engine) as session:
        session.add(CatalogMetadata(singleton_key=1))
        session.commit()
        yield session
    engine.dispose()


def test_owner_configures_append_only_close_backup_location(
    tmp_path: Path,
    catalog_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ai_accounting.close_backup.WindowsWriteThroughPublisher", _Publisher)
    service = CloseBackupService(
        catalog_session,
        context=_context(),
        settings=_settings(tmp_path),
    )
    requested = tmp_path / "owner-backups"
    request = ConfigureCloseBackupRequest(
        backup_directory=str(requested),
        idempotency_key="choose-owner-backup-root",
        confirmation_note="负责人确认今后关账自动备份到此目录。",
    )

    first = service.configure(request)
    replay = service.configure(request)
    catalog_session.commit()

    versions = catalog_session.scalars(select(CloseBackupLocationVersion)).all()
    assert requested.is_dir()
    assert len(versions) == 1
    assert first["configured"] is True
    assert replay["replayed"] is True
    with pytest.raises(
        CloseBackupError,
        match="IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST",
    ):
        service.configure(
            request.model_copy(update={"backup_directory": str(tmp_path / "different")})
        )


def test_committed_close_backup_failure_is_audited_and_same_close_retries(
    tmp_path: Path,
    catalog_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ai_accounting.close_backup.WindowsWriteThroughPublisher", _Publisher)
    service = CloseBackupService(
        catalog_session,
        context=_context(),
        settings=_settings(tmp_path),
    )
    backup_root = tmp_path / "close-backups"
    configured = service.configure(
        ConfigureCloseBackupRequest(
            backup_directory=str(backup_root),
            idempotency_key="configure-close-backups",
            confirmation_note="负责人确认。",
        )
    )
    location = catalog_session.scalar(select(CloseBackupLocationVersion))
    assert location is not None
    registry = CompanyRegistry(
        org_id=uuid.uuid4(),
        database_name=f"finance_company_{uuid.uuid4().hex}",
        database_identity=uuid.uuid4(),
        status="active",
        display_name="自动备份测试公司",
        taxpayer_identification_number="91330108MABXE0HA3F",
        profile_effective_from=date(2022, 8, 31),
        filing_cycle="quarterly",
        urban_maintenance_rate=Decimal("0.07"),
        is_primary=True,
    )
    catalog_session.add(registry)
    catalog_session.flush()
    del configured
    close_id = uuid.uuid4()
    period_id = uuid.uuid4()
    calls = 0

    def create_archive(**_kwargs: object) -> PortableBackupVerification:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise CloseBackupError("BACKUP_DATABASE_DUMP_FAILED")
        archive = backup_root / "company.finance-company.zip"
        archive.write_bytes(b"verified-portable")
        return PortableBackupVerification(
            archive_file=archive,
            archive_sha256="a" * 64,
            backup_id="close-backup",
            manifest_sha256="b" * 64,
            database_sha256="c" * 64,
            database_size_bytes=1,
            evidence_count=0,
            artifact_type="company",
            org_id=str(registry.org_id),
            database_identity=str(registry.database_identity),
            purpose="daily",
        )

    monkeypatch.setattr(service, "_create_archive", create_archive)
    runtime_value = CloseBackupRuntime(
        location=location,
        backup_root=backup_root,
        pg_bin_dir=tmp_path,
    )

    failed = service.backup_committed_close(
        registry=registry,
        close_id=close_id,
        period_id=period_id,
        period_month="2026-08",
        runtime=runtime_value,
    )
    completed = service.backup_committed_close(
        registry=registry,
        close_id=close_id,
        period_id=period_id,
        period_month="2026-08",
        runtime=runtime_value,
    )
    replay = service.backup_committed_close(
        registry=registry,
        close_id=close_id,
        period_id=period_id,
        period_month="2026-08",
        runtime=runtime_value,
    )

    assert failed["status"] == "failed"
    assert failed["error_code"] == "BACKUP_DATABASE_DUMP_FAILED"
    assert completed["status"] == "completed"
    assert completed["attempt_count"] == 2
    assert replay["replayed"] is True
    assert calls == 2


def test_close_backup_atomically_replaces_and_prunes_same_company_archives(
    tmp_path: Path,
    catalog_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = CompanyRegistry(
        org_id=uuid.uuid4(),
        database_name=f"finance_company_{uuid.uuid4().hex}",
        database_identity=uuid.uuid4(),
        status="active",
        display_name="单份备份测试公司",
        taxpayer_identification_number="91330108MABXE0HA3F",
        profile_effective_from=date(2022, 8, 31),
        filing_cycle="quarterly",
        urban_maintenance_rate=Decimal("0.07"),
        is_primary=True,
    )
    service = CloseBackupService(
        catalog_session,
        context=_context(),
        settings=_settings(tmp_path),
    )
    current = tmp_path / "91330108MABXE0HA3F.finance-company.zip"
    replacement = tmp_path / ".replacement"
    legacy = tmp_path / "legacy.finance-company.zip"
    other = tmp_path / "other.finance-company.zip"
    damaged = tmp_path / "damaged.finance-company.zip"
    current.write_bytes(b"old")
    replacement.write_bytes(b"new")
    legacy.write_bytes(b"legacy")
    other.write_bytes(b"other")
    damaged.write_bytes(b"damaged")

    def verification(path: Path) -> PortableBackupVerification:
        content = path.read_bytes()
        if content == b"damaged":
            raise BackupError("BACKUP_PORTABLE_INVALID")
        same_company = content != b"other"
        return PortableBackupVerification(
            archive_file=path.resolve(strict=True),
            archive_sha256=content.hex().ljust(64, "0"),
            backup_id="close-backup",
            manifest_sha256="b" * 64,
            database_sha256="c" * 64,
            database_size_bytes=1,
            evidence_count=0,
            artifact_type="company",
            org_id=str(registry.org_id if same_company else uuid.uuid4()),
            database_identity=str(registry.database_identity),
            purpose="daily",
        )

    monkeypatch.setattr(
        "ai_accounting.close_backup.WindowsWriteThroughPublisher",
        _ReplacingPublisher,
    )
    monkeypatch.setattr(
        "ai_accounting.close_backup.verify_portable_backup_archive",
        verification,
    )
    published = service._publish_single_company_archive(
        portable=verification(replacement),
        archive_file=current,
        registry=registry,
        backup_root=tmp_path,
    )

    assert published.archive_file == current
    assert current.read_bytes() == b"new"
    assert not replacement.exists()
    assert not legacy.exists()
    assert other.read_bytes() == b"other"
    assert damaged.read_bytes() == b"damaged"


def test_close_backup_publish_failure_preserves_previous_archive(
    tmp_path: Path,
    catalog_session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    registry = CompanyRegistry(
        org_id=uuid.uuid4(),
        database_name=f"finance_company_{uuid.uuid4().hex}",
        database_identity=uuid.uuid4(),
        status="active",
        display_name="替换失败测试公司",
        taxpayer_identification_number="91330108MABXE0HA3F",
        profile_effective_from=date(2022, 8, 31),
        filing_cycle="quarterly",
        urban_maintenance_rate=Decimal("0.07"),
        is_primary=True,
    )
    service = CloseBackupService(
        catalog_session,
        context=_context(),
        settings=_settings(tmp_path),
    )
    current = tmp_path / "91330108MABXE0HA3F.finance-company.zip"
    replacement = tmp_path / ".replacement"
    current.write_bytes(b"old")
    replacement.write_bytes(b"new")

    def verification(path: Path) -> PortableBackupVerification:
        return PortableBackupVerification(
            archive_file=path.resolve(strict=True),
            archive_sha256="a" * 64,
            backup_id="close-backup",
            manifest_sha256="b" * 64,
            database_sha256="c" * 64,
            database_size_bytes=1,
            evidence_count=0,
            artifact_type="company",
            org_id=str(registry.org_id),
            database_identity=str(registry.database_identity),
            purpose="daily",
        )

    class _FailingPublisher:
        def replace_file(self, replacement: Path, current: Path, root: Path) -> None:
            raise BackupError("BACKUP_PORTABLE_REPLACE_FAILED")

    monkeypatch.setattr(
        "ai_accounting.close_backup.WindowsWriteThroughPublisher",
        _FailingPublisher,
    )
    monkeypatch.setattr(
        "ai_accounting.close_backup.verify_portable_backup_archive",
        verification,
    )
    with pytest.raises(BackupError, match="BACKUP_PORTABLE_REPLACE_FAILED"):
        service._publish_single_company_archive(
            portable=verification(replacement),
            archive_file=current,
            registry=registry,
            backup_root=tmp_path,
        )

    assert current.read_bytes() == b"old"
    assert replacement.read_bytes() == b"new"
