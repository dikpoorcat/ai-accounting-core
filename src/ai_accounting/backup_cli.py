"""Credential CLI plus the fail-closed Windows backup command boundary."""

from __future__ import annotations

import argparse
import getpass
import ipaddress
import os
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from pydantic import SecretStr
from sqlalchemy.engine import make_url

from .backup import BackupError
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
    create_integrated_stopped_backup,
)
from .config import SettingsConfigurationError, get_settings
from .service_lease import ServiceLeaseError, WindowsBackupServiceLease
from .windows_backup import (
    WindowsCurrentUserOnlyAclVerifier,
    WindowsVolumeInspector,
    WindowsWriteThroughPublisher,
    preflight_windows_backup_root,
)


class BackupCliError(BackupIntegrationError):
    """Stable caller-facing CLI refusal."""


class _StableArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        del message
        raise BackupCliError("BACKUP_CLI_ARGUMENT_INVALID")


@dataclass(frozen=True)
class _BackupCliSettings:
    storage_dir: Path
    evidence_dir: Path
    service_lock_file: Path
    database_host: str
    database_port: int
    database_name: str
    runtime_role: str


def main(argv: Sequence[str] | None = None) -> None:
    try:
        _dispatch(_parser().parse_args(argv))
    except (
        BackupError,
        BackupCredentialError,
        ServiceLeaseError,
        SettingsConfigurationError,
    ) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        # This boundary never exposes argv, environment, native exception text,
        # stderr from PostgreSQL tools, or a traceback.
        print("BACKUP_LOCAL_COMMAND_FAILED", file=sys.stderr)
        raise SystemExit(1) from None


def _parser() -> argparse.ArgumentParser:
    parser = _StableArgumentParser(description="Encrypted stopped-service backup")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("credential-set", help="store finance_backup password securely")
    commands.add_parser("credential-delete", help="delete finance_backup password")
    create = commands.add_parser("create", help="create one stopped-service backup")
    create.add_argument("--backup-root", required=True, type=Path)
    create.add_argument("--purpose", choices=("daily", "pre_upgrade"), required=True)
    create.add_argument("--backup-id")
    create.add_argument("--pg-bin-dir", required=True, type=Path)
    return parser


def _dispatch(args: argparse.Namespace) -> None:
    if args.command == "credential-set":
        _credential_set()
    elif args.command == "credential-delete":
        WindowsFinanceBackupCredentialStore().delete_password()
        print("BACKUP_CREDENTIAL_DELETED")
    else:
        _create(args)


def _credential_set() -> None:
    first = _secret_prompt("finance_backup password: ")
    second = _secret_prompt("Repeat finance_backup password: ")
    if first != second:
        raise BackupCliError("BACKUP_CREDENTIAL_CONFIRMATION_MISMATCH")
    WindowsFinanceBackupCredentialStore().save_password(SecretStr(first))
    print("BACKUP_CREDENTIAL_STORED")


def _create(args: argparse.Namespace) -> None:
    # DEC-035 A trusts the validated local production Settings, runtime database
    # account, and current Windows account.  Every narrower deployment gate below
    # remains fail-closed and is checked before the backup can be published.
    settings = _load_production_settings()
    backup_root = _absolute_required(args.backup_root, "BACKUP_ROOT_ABSOLUTE_PATH_REQUIRED")
    pg_bin_dir = _absolute_required(args.pg_bin_dir, "BACKUP_PG_BIN_ABSOLUTE_PATH_REQUIRED")
    endpoint = PostgresEndpoint(
        host=settings.database_host,
        port=settings.database_port,
        database=settings.database_name,
        username="finance_backup",
        application_name="finance-backup-cli",
    )
    verifier = WindowsCurrentUserOnlyAclVerifier()
    credential_store = WindowsFinanceBackupCredentialStore()
    pgpass_provider = WindowsProtectedPgPassProvider(
        credential_store,
        settings.storage_dir,
        verifier,
    )
    publisher = WindowsWriteThroughPublisher()
    preflight_windows_backup_root(
        backup_root,
        WindowsVolumeInspector(),
        publisher,
    )
    service_lease = WindowsBackupServiceLease(settings.service_lock_file, verifier)
    verification = create_integrated_stopped_backup(
        backup_root,
        backup_id=args.backup_id or _new_backup_id(),
        purpose=args.purpose,
        evidence_root=settings.evidence_dir,
        endpoint=endpoint,
        runtime_role=settings.runtime_role,
        service_lease=service_lease,
        connection_provider=CredentialManagerConnectionProvider(credential_store),
        adapter_factory=lambda snapshot_id: PgDumpAdapter(
            endpoint,
            snapshot_id,
            pgpass_provider,
            verifier,
            pg_dump_executable=pg_bin_dir / "pg_dump.exe",
            pg_restore_executable=pg_bin_dir / "pg_restore.exe",
        ),
        publisher=publisher,
    )
    print(f"BACKUP_COMPLETE {verification.backup_id} {verification.manifest_sha256}")


def _load_production_settings() -> _BackupCliSettings:
    configured = get_settings()
    if configured.finance_environment != "production":
        raise BackupCliError("BACKUP_PRODUCTION_ENVIRONMENT_REQUIRED")
    storage = _absolute_required(
        configured.finance_storage_dir, "BACKUP_FINANCE_STORAGE_DIR_ABSOLUTE_PATH_REQUIRED"
    )
    evidence = _absolute_required(
        configured.finance_evidence_dir, "BACKUP_FINANCE_EVIDENCE_DIR_ABSOLUTE_PATH_REQUIRED"
    )
    service_lock = _absolute_required(
        configured.finance_service_lock_file,
        "BACKUP_FINANCE_SERVICE_LOCK_FILE_ABSOLUTE_PATH_REQUIRED",
    )
    try:
        evidence.relative_to(storage)
        service_lock.relative_to(storage)
    except ValueError as exc:
        raise BackupCliError("BACKUP_PRODUCTION_PATH_OUTSIDE_STORAGE_ROOT") from exc
    runtime = make_url(configured.database_url)
    host = runtime.host
    port = runtime.port or 5432
    database = runtime.database
    runtime_role = runtime.username
    if (
        host is None
        or database is None
        or runtime_role is None
        or runtime_role == "finance_backup"
    ):
        raise BackupCliError("BACKUP_RUNTIME_DATABASE_TARGET_INVALID")
    if not _is_loopback_database_host(host):
        raise BackupCliError("BACKUP_RUNTIME_DATABASE_NOT_LOOPBACK")
    return _BackupCliSettings(
        storage,
        evidence,
        service_lock,
        host,
        port,
        database,
        runtime_role,
    )


def _absolute_required(path: Path, error_code: str) -> Path:
    if not path.is_absolute():
        raise BackupCliError(error_code)
    return Path(os.path.abspath(path))


def _new_backup_id() -> str:
    timestamp = datetime.now(UTC).replace(microsecond=0).strftime("%Y%m%dT%H%M%SZ")
    return f"backup-{timestamp}-{uuid.uuid4().hex[:8]}"


def _is_loopback_database_host(host: str) -> bool:
    if host.casefold() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _secret_prompt(prompt: str) -> str:
    try:
        value = getpass.getpass(prompt)
    except (EOFError, KeyboardInterrupt) as exc:
        raise BackupCliError("BACKUP_SECRET_INPUT_UNAVAILABLE") from exc
    if not value:
        raise BackupCliError("BACKUP_CREDENTIAL_INVALID")
    return value


if __name__ == "__main__":
    main()
