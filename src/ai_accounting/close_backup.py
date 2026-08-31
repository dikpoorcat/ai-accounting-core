"""Owner-configured, company-only backup after a committed accounting close."""

from __future__ import annotations

import os
import re
import shutil
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from .accounting_periods import canonical_sha256
from .backup import (
    BackupError,
    PortableBackupVerification,
    create_portable_backup_archive,
    verify_portable_backup_archive,
)
from .backup_credentials import (
    BackupCredentialError,
    CredentialManagerConnectionProvider,
    WindowsFinanceBackupCredentialStore,
    WindowsProtectedPgPassProvider,
)
from .backup_integration import (
    BackupIntegrationError,
    PgDumpAdapter,
    PostgresEndpoint,
    create_integrated_online_backup,
)
from .company_schemas import ConfigureCloseBackupRequest
from .config import Settings
from .identity import ExecutionContext
from .models import (
    AccountingPeriodCloseBackup,
    CatalogMetadata,
    CloseBackupLocationVersion,
    CompanyRegistry,
    utcnow,
)
from .windows_backup import WindowsCurrentUserOnlyAclVerifier, WindowsWriteThroughPublisher

_SAFE_ERROR = re.compile(r"[A-Z][A-Z0-9_]{2,99}\Z")


class CloseBackupError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class CloseBackupRuntime:
    location: CloseBackupLocationVersion
    backup_root: Path
    pg_bin_dir: Path


class CloseBackupService:
    """Catalog-owned configuration and auditable post-commit backup coordinator."""

    def __init__(
        self,
        catalog_session: Session,
        *,
        context: ExecutionContext,
        settings: Settings,
    ) -> None:
        self.session = catalog_session
        self.context = context
        self.settings = settings

    def configure(self, request: ConfigureCloseBackupRequest) -> dict[str, Any]:
        if request.org_id != self.context.org_id:
            raise CloseBackupError("ACCOUNTING_PERIOD_CLOSE_BACKUP_IDENTITY_MISMATCH")
        payload_hash = canonical_sha256(request.model_dump(mode="json"))
        existing = self.session.scalar(
            select(CloseBackupLocationVersion).where(
                CloseBackupLocationVersion.org_id == self.context.org_id,
                CloseBackupLocationVersion.idempotency_key == request.idempotency_key
            )
        )
        if existing is not None:
            if existing.request_payload_hash != payload_hash:
                raise CloseBackupError(
                    "IDEMPOTENCY_KEY_REUSED_WITH_DIFFERENT_REQUEST"
                )
            return self._configuration_result(existing, replayed=True)

        # The catalog singleton is the serialization point even before a first
        # location version exists, avoiding competing version=1 inserts.
        if self.session.scalar(
            select(CatalogMetadata).where(CatalogMetadata.singleton_key == 1).with_for_update()
        ) is None:
            raise CloseBackupError("CATALOG_NOT_INITIALIZED")
        backup_root = self._prepare_backup_root(request.backup_directory)
        version = int(
            self.session.scalar(
                select(func.max(CloseBackupLocationVersion.version)).where(
                    CloseBackupLocationVersion.org_id == self.context.org_id
                )
            )
            or 0
        ) + 1
        item = CloseBackupLocationVersion(
            org_id=self.context.org_id,
            version=version,
            backup_directory=str(backup_root),
            idempotency_key=request.idempotency_key,
            request_payload_hash=payload_hash,
            confirmation_note=request.confirmation_note,
            owner_account_id=self.context.owner_account_id,
            owner_session_id=self.context.owner_session_id,
            owner_credential_version=self.context.owner_credential_version,
            executor_kind=self.context.executor_kind.value,
            executor_name=self.context.executor_name,
            executor_version=self.context.executor_version,
        )
        self.session.add(item)
        self.session.flush()
        return self._configuration_result(item, replayed=False)

    def get_configuration(self) -> dict[str, Any]:
        location = self._current_location()
        if location is None:
            return {
                "status": "ok",
                "configured": False,
                "automatic_backup_on_close": True,
                "readiness": "location_required",
            }
        return self._configuration_result(location, replayed=False)

    def require_ready(self) -> CloseBackupRuntime:
        location = self._current_location()
        if location is None:
            raise CloseBackupError("ACCOUNTING_PERIOD_CLOSE_BACKUP_LOCATION_REQUIRED")
        backup_root = self._validate_backup_root(location.backup_directory, probe=True)
        pg_bin_dir = self._postgres_bin_dir()
        try:
            credential = WindowsFinanceBackupCredentialStore().load_password()
        except BackupCredentialError as exc:
            raise CloseBackupError(str(exc)) from exc
        if credential is None:
            raise CloseBackupError("BACKUP_CREDENTIAL_REQUIRED")
        return CloseBackupRuntime(location, backup_root, pg_bin_dir)

    def backup_committed_close(
        self,
        *,
        registry: CompanyRegistry,
        close_id: uuid.UUID,
        period_id: uuid.UUID,
        period_month: str,
        runtime: CloseBackupRuntime,
    ) -> dict[str, Any]:
        if (
            registry.org_id != self.context.org_id
            or runtime.location.org_id != registry.org_id
        ):
            raise CloseBackupError("ACCOUNTING_PERIOD_CLOSE_BACKUP_IDENTITY_MISMATCH")
        attempt = self.session.scalar(
            select(AccountingPeriodCloseBackup)
            .where(
                AccountingPeriodCloseBackup.org_id == registry.org_id,
                AccountingPeriodCloseBackup.close_id == close_id,
            )
            .with_for_update()
        )
        if attempt is not None and attempt.status == "completed":
            return self._attempt_result(attempt, replayed=True)
        if attempt is None:
            attempt = AccountingPeriodCloseBackup(
                org_id=registry.org_id,
                close_id=close_id,
                period_id=period_id,
                period_month=period_month,
                database_identity=registry.database_identity,
                location_version_id=runtime.location.id,
                status="pending",
                attempt_count=0,
            )
            self.session.add(attempt)
            self.session.flush()
        elif (
            attempt.period_id != period_id
            or attempt.period_month != period_month
            or attempt.database_identity != registry.database_identity
        ):
            raise CloseBackupError("ACCOUNTING_PERIOD_CLOSE_BACKUP_IDENTITY_MISMATCH")

        attempt.status = "running"
        attempt.attempt_count += 1
        attempt.location_version_id = runtime.location.id
        attempt.error_code = None
        attempt.completed_at = None
        self.session.flush()
        try:
            portable = self._create_archive(
                registry=registry,
                close_id=close_id,
                period_month=period_month,
                attempt_number=attempt.attempt_count,
                runtime=runtime,
            )
        except Exception as exc:  # stable catalog result; never expose native paths/errors
            attempt.status = "failed"
            attempt.error_code = self._safe_error(exc)
            attempt.completed_at = utcnow()
            self.session.flush()
            return self._attempt_result(attempt, replayed=False)

        attempt.status = "completed"
        attempt.archive_file = str(portable.archive_file)
        attempt.archive_sha256 = portable.archive_sha256
        attempt.manifest_sha256 = portable.manifest_sha256
        attempt.error_code = None
        attempt.completed_at = utcnow()
        self.session.flush()
        return self._attempt_result(attempt, replayed=False)

    def _create_archive(
        self,
        *,
        registry: CompanyRegistry,
        close_id: uuid.UUID,
        period_month: str,
        attempt_number: int,
        runtime: CloseBackupRuntime,
    ) -> PortableBackupVerification:
        archive_file = runtime.backup_root / (
            f"{registry.taxpayer_identification_number}.finance-company.zip"
        )
        replacement_file = runtime.backup_root / (
            f".{registry.taxpayer_identification_number}-{close_id.hex}-a{attempt_number}"
            ".replacement"
        )

        settings_url = make_url(self.settings.database_url)
        company_url = make_url(self.settings.finance_company_database_url or "")
        if (
            settings_url.host is None
            or settings_url.database is None
            or company_url.host is None
            or company_url.username is None
        ):
            raise CloseBackupError("COMPANY_DATABASE_ROUTING_NOT_CONFIGURED")
        endpoint = PostgresEndpoint(
            host=company_url.host,
            port=company_url.port or 5432,
            database=registry.database_name,
            username="finance_backup",
            application_name="finance-close-backup",
        )
        registries = self.session.scalars(
            select(CompanyRegistry).where(
                CompanyRegistry.status.in_(("active", "archived"))
            )
        ).all()
        allowed_names = frozenset(
            {
                settings_url.database,
                *(item.database_name for item in registries),
            }
        )
        verifier = WindowsCurrentUserOnlyAclVerifier()
        credential_store = WindowsFinanceBackupCredentialStore()
        pgpass = WindowsProtectedPgPassProvider(
            credential_store,
            self.settings.finance_storage_dir,
            verifier,
        )
        publisher = WindowsWriteThroughPublisher()
        backup_id = (
            f"close-{period_month.replace('-', '')}-{close_id.hex[:12]}-"
            f"a{attempt_number}"
        )
        verified = create_integrated_online_backup(
            runtime.backup_root,
            backup_id=backup_id,
            evidence_root=self.settings.finance_evidence_dir,
            endpoint=endpoint,
            runtime_role=company_url.username,
            connection_provider=CredentialManagerConnectionProvider(credential_store),
            adapter_factory=lambda snapshot_id: PgDumpAdapter(
                endpoint,
                snapshot_id,
                pgpass,
                verifier,
                pg_dump_executable=runtime.pg_bin_dir / "pg_dump.exe",
                pg_restore_executable=runtime.pg_bin_dir / "pg_restore.exe",
            ),
            publisher=publisher,
            org_id=str(registry.org_id),
            database_identity=str(registry.database_identity),
            allowed_database_names=allowed_names,
        )
        try:
            if replacement_file.exists():
                portable = verify_portable_backup_archive(replacement_file)
                self._require_company_archive(portable, registry)
            else:
                portable = create_portable_backup_archive(
                    runtime.backup_root,
                    verified.backup_directory,
                    replacement_file,
                )
            self._remove_verified_staging(verified.backup_directory, runtime.backup_root)
            return self._publish_single_company_archive(
                portable=portable,
                archive_file=archive_file,
                registry=registry,
                backup_root=runtime.backup_root,
            )
        finally:
            self._remove_replacement_file(replacement_file, runtime.backup_root)

    @staticmethod
    def _require_company_archive(
        portable: PortableBackupVerification,
        registry: CompanyRegistry,
    ) -> None:
        if (
            portable.org_id != str(registry.org_id)
            or portable.database_identity != str(registry.database_identity)
            or portable.artifact_type != "company"
        ):
            raise CloseBackupError("ACCOUNTING_PERIOD_CLOSE_BACKUP_FILE_CONFLICT")

    def _publish_single_company_archive(
        self,
        *,
        portable: PortableBackupVerification,
        archive_file: Path,
        registry: CompanyRegistry,
        backup_root: Path,
    ) -> PortableBackupVerification:
        self._require_company_archive(portable, registry)
        previous_archive_file = backup_root / (
            f"{registry.taxpayer_identification_number}.previous.finance-company.zip"
        )
        publisher = WindowsWriteThroughPublisher()
        if archive_file.exists() or archive_file.is_symlink():
            current = verify_portable_backup_archive(archive_file)
            self._require_company_archive(current, registry)
            self._rotate_current_to_previous(
                current=current,
                previous_archive_file=previous_archive_file,
                registry=registry,
                backup_root=backup_root,
                publisher=publisher,
            )
        publisher.replace_file(
            portable.archive_file,
            archive_file,
            backup_root,
        )
        published = verify_portable_backup_archive(archive_file)
        self._require_company_archive(published, registry)
        self._remove_older_company_archives(
            retained_archive_files=(published.archive_file, previous_archive_file),
            registry=registry,
            backup_root=backup_root,
        )
        return published

    def _rotate_current_to_previous(
        self,
        *,
        current: PortableBackupVerification,
        previous_archive_file: Path,
        registry: CompanyRegistry,
        backup_root: Path,
        publisher: WindowsWriteThroughPublisher,
    ) -> None:
        """Durably retain the current verified package before replacing it."""

        self._require_company_archive(current, registry)
        if previous_archive_file.exists() or previous_archive_file.is_symlink():
            previous = verify_portable_backup_archive(previous_archive_file)
            self._require_company_archive(previous, registry)
        staged = backup_root / (
            f".{registry.taxpayer_identification_number}-{uuid.uuid4().hex}"
            ".previous.replacement"
        )
        try:
            source_before = current.archive_file.lstat()
            if current.archive_file.is_symlink():
                raise CloseBackupError("ACCOUNTING_PERIOD_CLOSE_BACKUP_FILE_CONFLICT")
            with current.archive_file.open("rb") as source, staged.open("xb") as target:
                shutil.copyfileobj(source, target)
                target.flush()
                os.fsync(target.fileno())
            source_after = current.archive_file.lstat()
            if (
                source_before.st_dev,
                source_before.st_ino,
                source_before.st_size,
                source_before.st_mtime_ns,
            ) != (
                source_after.st_dev,
                source_after.st_ino,
                source_after.st_size,
                source_after.st_mtime_ns,
            ):
                raise CloseBackupError("ACCOUNTING_PERIOD_CLOSE_BACKUP_FILE_CONFLICT")
            staged_verification = verify_portable_backup_archive(staged)
            self._require_company_archive(staged_verification, registry)
            if staged_verification.archive_sha256 != current.archive_sha256:
                raise CloseBackupError("ACCOUNTING_PERIOD_CLOSE_BACKUP_FILE_CONFLICT")
            publisher.replace_file(staged, previous_archive_file, backup_root)
            retained = verify_portable_backup_archive(previous_archive_file)
            self._require_company_archive(retained, registry)
            if retained.archive_sha256 != current.archive_sha256:
                raise CloseBackupError("ACCOUNTING_PERIOD_CLOSE_BACKUP_FILE_CONFLICT")
        except FileExistsError as exc:
            raise CloseBackupError(
                "ACCOUNTING_PERIOD_CLOSE_BACKUP_ROTATION_FAILED"
            ) from exc
        finally:
            self._remove_replacement_file(staged, backup_root)

    def _remove_older_company_archives(
        self,
        *,
        retained_archive_files: tuple[Path, ...],
        registry: CompanyRegistry,
        backup_root: Path,
    ) -> None:
        try:
            root = backup_root.resolve(strict=True)
            retained = {
                item.resolve(strict=True)
                for item in retained_archive_files
                if item.exists() and not item.is_symlink()
            }
            candidates = tuple(root.glob("*.finance-company.zip"))
        except OSError as exc:
            raise CloseBackupError(
                "ACCOUNTING_PERIOD_CLOSE_BACKUP_RETENTION_CLEANUP_FAILED"
        ) from exc
        for candidate in candidates:
            try:
                if candidate.is_symlink() or candidate.resolve(strict=True) in retained:
                    continue
                before = candidate.lstat()
                old = verify_portable_backup_archive(candidate)
                after = candidate.lstat()
                if (before.st_dev, before.st_ino, before.st_size) != (
                    after.st_dev,
                    after.st_ino,
                    after.st_size,
                ):
                    continue
                if old.artifact_type == "company" and old.org_id == str(registry.org_id):
                    candidate.unlink()
            except BackupError:
                # Unknown or damaged files are not safe to attribute and delete.
                continue
            except OSError as exc:
                raise CloseBackupError(
                    "ACCOUNTING_PERIOD_CLOSE_BACKUP_RETENTION_CLEANUP_FAILED"
                ) from exc

    @staticmethod
    def _remove_replacement_file(replacement: Path, root: Path) -> None:
        try:
            resolved_root = root.resolve(strict=True)
            if replacement.is_symlink():
                return
            target = replacement.resolve(strict=True)
            if target.parent == resolved_root and target.is_file():
                target.unlink()
        except OSError:
            return

    def _configuration_result(
        self, location: CloseBackupLocationVersion, *, replayed: bool
    ) -> dict[str, Any]:
        readiness = "ready"
        readiness_errors: list[str] = []
        try:
            self._validate_backup_root(location.backup_directory, probe=False)
            self._postgres_bin_dir()
            credential = WindowsFinanceBackupCredentialStore().load_password()
            if credential is None:
                raise CloseBackupError("BACKUP_CREDENTIAL_REQUIRED")
        except BackupCredentialError as exc:
            readiness = "not_ready"
            readiness_errors.append(str(exc))
        except CloseBackupError as exc:
            readiness = "not_ready"
            readiness_errors.append(exc.code)
        return {
            "status": "ok",
            "configured": True,
            "automatic_backup_on_close": True,
            "org_id": str(location.org_id),
            "backup_directory": location.backup_directory,
            "configuration_version": location.version,
            "retained_generations": 2,
            "configured_at": location.created_at.isoformat(),
            "readiness": readiness,
            "readiness_errors": readiness_errors,
            "replayed": replayed,
        }

    @staticmethod
    def _attempt_result(
        attempt: AccountingPeriodCloseBackup, *, replayed: bool
    ) -> dict[str, Any]:
        return {
            "status": attempt.status,
            "backup_id": str(attempt.id),
            "org_id": str(attempt.org_id),
            "close_id": str(attempt.close_id),
            "period_month": attempt.period_month,
            "attempt_count": attempt.attempt_count,
            "archive_file": attempt.archive_file,
            "archive_sha256": attempt.archive_sha256,
            "manifest_sha256": attempt.manifest_sha256,
            "error_code": attempt.error_code,
            "replayed": replayed,
        }

    def _current_location(self) -> CloseBackupLocationVersion | None:
        return self.session.scalar(
            select(CloseBackupLocationVersion)
            .where(CloseBackupLocationVersion.org_id == self.context.org_id)
            .order_by(CloseBackupLocationVersion.version.desc())
            .limit(1)
        )

    def _validate_backup_root(self, raw: str, *, probe: bool) -> Path:
        if "\x00" in raw:
            raise CloseBackupError("BACKUP_ROOT_INVALID")
        path = Path(raw)
        if not path.is_absolute():
            raise CloseBackupError("BACKUP_ROOT_ABSOLUTE_PATH_REQUIRED")
        absolute = Path(os.path.abspath(path))
        try:
            resolved = absolute.resolve(strict=True)
        except OSError as exc:
            raise CloseBackupError("BACKUP_STORAGE_UNAVAILABLE") from exc
        junction = getattr(resolved, "is_junction", None)
        if (
            resolved != absolute
            or not resolved.is_dir()
            or resolved.is_symlink()
            or (junction is not None and junction())
        ):
            raise CloseBackupError("BACKUP_STORAGE_UNAVAILABLE")
        if probe:
            try:
                WindowsWriteThroughPublisher().durable_directory_preflight(resolved)
            except (BackupError, OSError) as exc:
                raise CloseBackupError("BACKUP_STORAGE_NOT_WRITABLE") from exc
        return resolved

    def _prepare_backup_root(self, raw: str) -> Path:
        if "\x00" in raw:
            raise CloseBackupError("BACKUP_ROOT_INVALID")
        requested = Path(raw)
        if not requested.is_absolute():
            raise CloseBackupError("BACKUP_ROOT_ABSOLUTE_PATH_REQUIRED")
        absolute = Path(os.path.abspath(requested))
        if not absolute.exists():
            try:
                parent = absolute.parent.resolve(strict=True)
                if parent != absolute.parent or not parent.is_dir() or parent.is_symlink():
                    raise CloseBackupError("BACKUP_STORAGE_UNAVAILABLE")
                absolute.mkdir(mode=0o700)
            except CloseBackupError:
                raise
            except OSError as exc:
                raise CloseBackupError("BACKUP_STORAGE_UNAVAILABLE") from exc
        return self._validate_backup_root(str(absolute), probe=True)

    def _postgres_bin_dir(self) -> Path:
        executable_names = ("pg_dump.exe", "pg_restore.exe")
        candidates: list[Path] = []
        if self.settings.finance_postgres_bin_dir is not None:
            candidates.append(self.settings.finance_postgres_bin_dir)
        discovered = shutil.which("pg_dump.exe") or shutil.which("pg_dump")
        if discovered:
            candidates.append(Path(discovered).parent)
        if sys.platform == "win32":
            candidates.extend(
                (
                    Path("C:/Program Files/PostgreSQL/17/bin"),
                    Path("C:/PostgreSQL/17/bin"),
                )
            )
        for candidate in candidates:
            absolute = Path(os.path.abspath(candidate))
            if all((absolute / name).is_file() for name in executable_names):
                return absolute
        raise CloseBackupError("BACKUP_POSTGRES_TOOLS_UNAVAILABLE")

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        if isinstance(exc, CloseBackupError):
            return exc.code
        if isinstance(exc, (BackupError, BackupIntegrationError)):
            code = str(exc)
            if _SAFE_ERROR.fullmatch(code):
                return code
        return "ACCOUNTING_PERIOD_CLOSE_BACKUP_FAILED"

    @staticmethod
    def _remove_verified_staging(directory: Path, root: Path) -> None:
        try:
            resolved_root = root.resolve(strict=True)
            target = directory.resolve(strict=True)
            if target.parent != resolved_root or not target.name.endswith(".complete"):
                return
            shutil.rmtree(target)
        except OSError:
            # The portable archive is already verified and durable. A leftover
            # staging directory is non-authoritative and can be cleaned later.
            return
