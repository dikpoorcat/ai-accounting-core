"""Deployment integration boundaries for DEC-017/025/028/029 backups.

The production ``create`` CLI remains fail-closed until DEC-035 freezes a
non-spoofable machine deployment binding.  Credential management and the
integration types are available independently.  The types here keep passwords
out of URLs, argv, process output, manifests, and object reprs.
"""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Sequence
from contextlib import AbstractContextManager, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.engine import URL

from .backup import (
    BackupError,
    BackupPrecondition,
    BackupPublisher,
    BackupRequest,
    BackupVerification,
    DatabaseDumpMetadata,
    EvidenceSnapshot,
    create_stopped_backup,
    verify_backup,
)

_IDENTIFIER_PATTERN = re.compile(r"[A-Za-z_][A-Za-z0-9_.-]{0,62}\Z")
_SNAPSHOT_PATTERN = re.compile(r"[A-Za-z0-9-]{1,128}\Z")
_SAFE_APPLICATION_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,63}\Z")
_SYSTEM_IDENTIFIER_PATTERN = re.compile(r"[1-9][0-9]{0,19}\Z")
_FINANCE_BACKUP_ROLE = "finance_backup"
_PROJECT_SECRET_ENVIRONMENT_NAMES = {
    "DATABASE_URL",
    "FINANCE_MIGRATION_DATABASE_URL",
    "FINANCE_RUNTIME_DATABASE_URL",
    "FINANCE_BACKUP_DATABASE_URL",
    "FINANCE_BACKUP_PASSWORD",
    "FINANCE_DATABASE_PASSWORD",
    "FINANCE_OWNER_PASSWORD",
    "FINANCE_OWNER_SESSION_TOKEN",
    "FINANCE_SESSION_TOKEN",
}


class BackupIntegrationError(BackupError):
    """Stable diagnostic for a deployment-integration refusal."""


@dataclass(frozen=True)
class PostgresEndpoint:
    """A password-free PostgreSQL endpoint safe for argv and diagnostics."""

    host: str
    port: int
    database: str
    username: str
    application_name: str

    def __post_init__(self) -> None:
        if (
            not _valid_postgres_host(self.host)
            or not 1 <= self.port <= 65_535
            or not _IDENTIFIER_PATTERN.fullmatch(self.database)
            or not _IDENTIFIER_PATTERN.fullmatch(self.username)
            or not _SAFE_APPLICATION_PATTERN.fullmatch(self.application_name)
        ):
            raise BackupIntegrationError("BACKUP_POSTGRES_ENDPOINT_INVALID")

    def same_database_as(self, other: PostgresEndpoint) -> bool:
        return (
            self.host.casefold(),
            self.port,
            self.database,
        ) == (
            other.host.casefold(),
            other.port,
            other.database,
        )

    def passwordless_sqlalchemy_url(self) -> str:
        """Return a URL containing only separately validated non-secret fields."""
        return URL.create(
            "postgresql+psycopg",
            username=self.username,
            host=self.host,
            port=self.port,
            database=self.database,
        ).render_as_string(hide_password=True)


class PgPassFileProvider(Protocol):
    """Lease an ACL-protected pgpass file without exposing its contents."""

    def lease_pgpass(self, endpoint: PostgresEndpoint) -> AbstractContextManager[Path]: ...


class PgPassFileAccessVerifier(Protocol):
    """Prove at the use site that only the current Windows user can read a lease."""

    def assert_current_windows_user_only(self, path: Path) -> None: ...


class BackupServiceLease(Protocol):
    """Hold the cross-process exclusion lease through complete publication."""

    def acquire_backup_lease(self) -> AbstractContextManager[None]: ...


class BackupDatabaseConnectionProvider(Protocol):
    """Open a DB connection while keeping authentication material private."""

    def connect(self, endpoint: PostgresEndpoint) -> AbstractContextManager[Any]: ...


class VerifiedArchiveCopyProvider(Protocol):
    """Lease a hash-checked local copy while holding its path stable."""

    def lease_verified_archive(
        self,
        source: Path,
        source_root: Path,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
    ) -> AbstractContextManager[Path]: ...


@dataclass(frozen=True)
class DatabaseBackupSnapshot:
    schema_revision: str
    source_system_identifier: str
    snapshot_id: str
    evidence: tuple[EvidenceSnapshot, ...]


class CommandRunner(Protocol):
    def __call__(
        self,
        argv: Sequence[str],
        *,
        environment: dict[str, str],
    ) -> subprocess.CompletedProcess[str]: ...


def _run_command(
    argv: Sequence[str],
    *,
    environment: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        list(argv),
        check=False,
        shell=False,
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=environment,
    )


class PgDumpAdapter:
    """Run PostgreSQL 17 pg_dump/list with a leased protected pgpass file."""

    def __init__(
        self,
        endpoint: PostgresEndpoint,
        snapshot_id: str,
        pgpass_provider: PgPassFileProvider,
        access_verifier: PgPassFileAccessVerifier,
        *,
        pg_dump_executable: Path,
        pg_restore_executable: Path,
        runner: CommandRunner = _run_command,
    ) -> None:
        if endpoint.username != _FINANCE_BACKUP_ROLE:
            raise BackupIntegrationError("BACKUP_DATABASE_ROLE_INVALID")
        if not _SNAPSHOT_PATTERN.fullmatch(snapshot_id):
            raise BackupIntegrationError("BACKUP_DATABASE_SNAPSHOT_INVALID")
        self._endpoint = endpoint
        self._snapshot_id = snapshot_id
        self._pgpass_provider = pgpass_provider
        self._access_verifier = access_verifier
        self._pg_dump = _validated_executable(pg_dump_executable)
        self._pg_restore = _validated_executable(pg_restore_executable)
        self._runner = runner

    def dump(self, destination: Path, metadata: DatabaseDumpMetadata) -> None:
        del metadata
        argv = (
            str(self._pg_dump),
            "--format=custom",
            "--no-password",
            "--file",
            str(destination),
            "--snapshot",
            self._snapshot_id,
            "--host",
            self._endpoint.host,
            "--port",
            str(self._endpoint.port),
            "--username",
            self._endpoint.username,
            "--dbname",
            self._endpoint.database,
        )
        self._run_authenticated(argv, "BACKUP_DATABASE_DUMP_FAILED")

    def list_archive(self, archive: Path) -> tuple[str, ...]:
        result = self._runner(
            (str(self._pg_restore), "--list", str(archive)),
            environment=_sanitized_pg_environment(None, "finance-backup-list"),
        )
        if result.returncode != 0:
            raise BackupIntegrationError("BACKUP_DATABASE_ARCHIVE_INVALID")
        contents = tuple(
            line for line in result.stdout.splitlines() if line and not line.startswith(";")
        )
        if not contents:
            raise BackupIntegrationError("BACKUP_DATABASE_ARCHIVE_INVALID")
        return contents

    def _run_authenticated(self, argv: Sequence[str], error_code: str) -> None:
        try:
            with self._pgpass_provider.lease_pgpass(self._endpoint) as pgpass_path:
                protected = _validated_pgpass_path(pgpass_path)
                self._access_verifier.assert_current_windows_user_only(protected)
                result = self._runner(
                    argv,
                    environment=_sanitized_pg_environment(
                        protected, self._endpoint.application_name
                    ),
                )
        except BackupIntegrationError:
            raise
        except Exception as exc:
            raise BackupIntegrationError("BACKUP_PGPASS_UNAVAILABLE") from exc
        if result.returncode != 0:
            raise BackupIntegrationError(error_code)


class PgRestoreAdapter:
    """Restore an archive only into an explicitly isolated password-free endpoint."""

    def __init__(
        self,
        endpoint: PostgresEndpoint,
        operational_endpoint: PostgresEndpoint,
        pgpass_provider: PgPassFileProvider,
        access_verifier: PgPassFileAccessVerifier,
        *,
        pg_restore_executable: Path,
        runner: CommandRunner = _run_command,
    ) -> None:
        if endpoint.same_database_as(operational_endpoint):
            raise BackupIntegrationError("BACKUP_RESTORE_TARGET_IS_OPERATIONAL_DATABASE")
        self._endpoint = endpoint
        self._pgpass_provider = pgpass_provider
        self._access_verifier = access_verifier
        self._pg_restore = _validated_executable(pg_restore_executable)
        self._runner = runner

    @property
    def endpoint(self) -> PostgresEndpoint:
        return self._endpoint

    def list_archive(self, archive: Path) -> tuple[str, ...]:
        result = self._runner(
            (str(self._pg_restore), "--list", str(archive)),
            environment=_sanitized_pg_environment(None, "finance-restore-list"),
        )
        if result.returncode != 0:
            raise BackupIntegrationError("BACKUP_DATABASE_ARCHIVE_INVALID")
        contents = tuple(
            line for line in result.stdout.splitlines() if line and not line.startswith(";")
        )
        if not contents:
            raise BackupIntegrationError("BACKUP_DATABASE_ARCHIVE_INVALID")
        return contents

    def restore(self, archive: Path) -> None:
        argv = (
            str(self._pg_restore),
            "--exit-on-error",
            "--no-password",
            "--no-owner",
            "--no-privileges",
            "--host",
            self._endpoint.host,
            "--port",
            str(self._endpoint.port),
            "--username",
            self._endpoint.username,
            "--dbname",
            self._endpoint.database,
            str(archive),
        )
        try:
            with self._pgpass_provider.lease_pgpass(self._endpoint) as pgpass_path:
                protected = _validated_pgpass_path(pgpass_path)
                self._access_verifier.assert_current_windows_user_only(protected)
                result = self._runner(
                    argv,
                    environment=_sanitized_pg_environment(
                        protected, self._endpoint.application_name
                    ),
                )
        except BackupIntegrationError:
            raise
        except Exception as exc:
            raise BackupIntegrationError("BACKUP_PGPASS_UNAVAILABLE") from exc
        if result.returncode != 0:
            raise BackupIntegrationError("BACKUP_DATABASE_RESTORE_FAILED")

    def alembic_check(self, repository_root: Path) -> None:
        """Run only ``alembic check``; this method never invokes upgrade."""
        root = repository_root.resolve(strict=True)
        script_location = (root / "alembic").resolve(strict=True)
        try:
            with self._pgpass_provider.lease_pgpass(self._endpoint) as pgpass_path:
                protected = _validated_pgpass_path(pgpass_path)
                self._access_verifier.assert_current_windows_user_only(protected)
                with tempfile.TemporaryDirectory(prefix="finance-alembic-check-") as temporary:
                    config_path = Path(temporary) / "alembic.ini"
                    template = (root / "alembic.ini").read_text(encoding="utf-8")
                    template, script_location_count = re.subn(
                        r"(?m)^script_location\s*=.*$",
                        lambda _: f"script_location = {script_location.as_posix()}",
                        template,
                    )
                    template, sqlalchemy_url_count = re.subn(
                        r"(?m)^sqlalchemy\.url\s*=.*$",
                        lambda _: (
                            "sqlalchemy.url = "
                            + self._endpoint.passwordless_sqlalchemy_url()
                        ),
                        template,
                    )
                    if script_location_count != 1 or sqlalchemy_url_count != 1:
                        raise BackupIntegrationError("BACKUP_ALEMBIC_CHECK_FAILED")
                    config_path.write_text(template, encoding="utf-8")
                    result = self._runner(
                        (
                            sys.executable,
                            "-m",
                            "alembic",
                            "-c",
                            str(config_path),
                            "check",
                        ),
                        environment=_sanitized_pg_environment(
                            protected, "finance-restore-alembic-check"
                        ),
                    )
        except BackupIntegrationError:
            raise
        except Exception as exc:
            raise BackupIntegrationError("BACKUP_ALEMBIC_CHECK_FAILED") from exc
        if result.returncode != 0:
            raise BackupIntegrationError("BACKUP_ALEMBIC_CHECK_FAILED")


@contextmanager
def postgres_backup_snapshot(
    connection_provider: BackupDatabaseConnectionProvider,
    endpoint: PostgresEndpoint,
    *,
    runtime_role: str,
) -> Iterator[DatabaseBackupSnapshot]:
    """Hold one exported read-only snapshot through evidence projection and pg_dump."""
    if endpoint.username != _FINANCE_BACKUP_ROLE or not _IDENTIFIER_PATTERN.fullmatch(runtime_role):
        raise BackupIntegrationError("BACKUP_DATABASE_ROLE_INVALID")
    try:
        with connection_provider.connect(endpoint) as connection:
            with connection.transaction():
                connection.execute(
                    "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
                )
                current_user = connection.execute("SELECT current_user").fetchone()
                if current_user is None or current_user[0] != _FINANCE_BACKUP_ROLE:
                    raise BackupIntegrationError("BACKUP_DATABASE_ROLE_INVALID")
                _assert_finance_backup_role_is_minimal(connection)
                _assert_finance_backup_database_connect_is_minimal(connection)
                active = connection.execute(
                    """
                    SELECT count(*)
                    FROM pg_catalog.pg_stat_activity
                    WHERE datname = current_database()
                      AND usename = %s
                      AND pid <> pg_backend_pid()
                    """,
                    (runtime_role,),
                ).fetchone()
                if active is None or not isinstance(active[0], int):
                    raise BackupIntegrationError("BACKUP_CONNECTION_CHECK_FAILED")
                if active[0] != 0:
                    raise BackupIntegrationError("BACKUP_ACTIVE_CONNECTIONS")
                source_identity = connection.execute(
                    """
                    SELECT current_setting('server_version_num'), system_identifier::text
                    FROM pg_catalog.pg_control_system()
                    """
                ).fetchone()
                if (
                    source_identity is None
                    or not _is_postgres_17_version(source_identity[0])
                    or not _is_system_identifier(source_identity[1])
                ):
                    raise BackupIntegrationError("BACKUP_SOURCE_DATABASE_IDENTITY_INVALID")
                revisions = connection.execute(
                    "SELECT version_num FROM alembic_version ORDER BY version_num"
                ).fetchall()
                if len(revisions) != 1 or not isinstance(revisions[0][0], str):
                    raise BackupIntegrationError("BACKUP_SCHEMA_REVISION_INVALID")
                rows = connection.execute(
                    """
                    SELECT id::text, sha256, size_bytes, storage_path
                    FROM evidence
                    ORDER BY id::text, sha256
                    """
                ).fetchall()
                evidence = tuple(
                    EvidenceSnapshot(
                        evidence_id=row[0],
                        sha256=row[1],
                        size_bytes=row[2],
                        storage_path=Path(row[3]),
                    )
                    for row in rows
                )
                exported = connection.execute("SELECT pg_export_snapshot()").fetchone()
                if (
                    exported is None
                    or not isinstance(exported[0], str)
                    or not _SNAPSHOT_PATTERN.fullmatch(exported[0])
                ):
                    raise BackupIntegrationError("BACKUP_DATABASE_SNAPSHOT_INVALID")
                yield DatabaseBackupSnapshot(
                    schema_revision=revisions[0][0],
                    source_system_identifier=source_identity[1],
                    snapshot_id=exported[0],
                    evidence=evidence,
                )
    except BackupIntegrationError:
        raise
    except Exception as exc:
        raise BackupIntegrationError("BACKUP_DATABASE_SNAPSHOT_FAILED") from exc


def create_integrated_stopped_backup(
    backup_root: Path,
    *,
    backup_id: str,
    purpose: str,
    evidence_root: Path,
    endpoint: PostgresEndpoint,
    runtime_role: str,
    service_lease: BackupServiceLease,
    connection_provider: BackupDatabaseConnectionProvider,
    adapter_factory: Callable[[str], PgDumpAdapter],
    publisher: BackupPublisher,
) -> BackupVerification:
    """Create one stopped-service backup without accepting a password or URL."""
    with service_lease.acquire_backup_lease():
        with postgres_backup_snapshot(
            connection_provider, endpoint, runtime_role=runtime_role
        ) as snapshot:
            return create_stopped_backup(
                backup_root,
                BackupRequest(
                    backup_id=backup_id,
                    purpose=purpose,
                    precondition=BackupPrecondition(
                        service_stopped=True,
                        active_business_connections=0,
                    ),
                    evidence_root=evidence_root,
                    evidence=snapshot.evidence,
                    database=DatabaseDumpMetadata(
                        schema_revision=snapshot.schema_revision,
                        source_system_identifier=snapshot.source_system_identifier,
                    ),
                ),
                adapter_factory(snapshot.snapshot_id),
                publisher=publisher,
            )


@dataclass(frozen=True)
class RestoreDrillResult:
    backup_id: str
    schema_revision: str
    evidence_count: int


def run_isolated_restore_drill(
    backup_root: Path,
    backup_directory: Path,
    *,
    operational_endpoint: PostgresEndpoint,
    restore_adapter: PgRestoreAdapter,
    connection_provider: BackupDatabaseConnectionProvider,
    archive_copy_provider: VerifiedArchiveCopyProvider,
    repository_root: Path,
) -> RestoreDrillResult:
    """Restore into a fresh isolated DB and verify head/schema/evidence without upgrade."""
    if restore_adapter.endpoint.same_database_as(operational_endpoint):
        raise BackupIntegrationError("BACKUP_RESTORE_TARGET_IS_OPERATIONAL_DATABASE")
    verified = verify_backup(backup_root, backup_directory)
    head = _single_alembic_head(repository_root)
    if verified.schema_revision != head:
        raise BackupIntegrationError("BACKUP_SCHEMA_REVISION_NOT_CURRENT_HEAD")
    _assert_isolated_restore_target(
        connection_provider,
        restore_adapter.endpoint,
        source_system_identifier=verified.source_system_identifier,
    )
    with archive_copy_provider.lease_verified_archive(
        verified.backup_directory / "database.dump",
        verified.backup_directory,
        expected_sha256=verified.database_sha256,
        expected_size_bytes=verified.database_size_bytes,
    ) as archive:
        if not restore_adapter.list_archive(archive):
            raise BackupIntegrationError("BACKUP_DATABASE_ARCHIVE_INVALID")
        restore_adapter.restore(archive)

    with connection_provider.connect(restore_adapter.endpoint) as connection:
        revisions = connection.execute(
            "SELECT version_num FROM alembic_version ORDER BY version_num"
        ).fetchall()
        if revisions != [(head,)]:
            raise BackupIntegrationError("BACKUP_RESTORED_SCHEMA_REVISION_MISMATCH")
        rows = connection.execute(
            """
            SELECT id::text, sha256, size_bytes
            FROM evidence
            ORDER BY id::text, sha256
            """
        ).fetchall()
    expected = tuple(
        (entry.evidence_id, entry.sha256, entry.size_bytes) for entry in verified.evidence
    )
    if tuple(tuple(row) for row in rows) != expected:
        raise BackupIntegrationError("BACKUP_RESTORED_EVIDENCE_MISMATCH")
    restore_adapter.alembic_check(repository_root)
    return RestoreDrillResult(
        backup_id=verified.backup_id,
        schema_revision=head,
        evidence_count=len(expected),
    )


def production_backup_command() -> None:
    """Stable pause until DEC-035 freezes a non-spoofable deployment binding."""
    raise BackupIntegrationError("BACKUP_DEC035_DEPLOYMENT_BINDING_UNDECIDED")


def _single_alembic_head(repository_root: Path) -> str:
    config = Config(str(repository_root / "alembic.ini"))
    config.set_main_option("script_location", str(repository_root / "alembic"))
    heads = ScriptDirectory.from_config(config).get_heads()
    if len(heads) != 1:
        raise BackupIntegrationError("BACKUP_ALEMBIC_HEAD_NOT_SINGLE")
    return heads[0]


def _assert_finance_backup_role_is_minimal(connection: Any) -> None:
    attributes = connection.execute(
        """
        SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole,
               rolreplication, rolbypassrls
        FROM pg_catalog.pg_roles
        WHERE rolname = current_user
        """
    ).fetchall()
    if attributes != [(True, True, False, False, False, False, False)]:
        raise BackupIntegrationError("BACKUP_DATABASE_ROLE_PRIVILEGES_INVALID")
    memberships = connection.execute(
        """
        SELECT parent.rolname
        FROM pg_catalog.pg_auth_members AS membership
        JOIN pg_catalog.pg_roles AS member_role
          ON member_role.oid = membership.member
        JOIN pg_catalog.pg_roles AS parent
          ON parent.oid = membership.roleid
        WHERE member_role.rolname = current_user
        ORDER BY parent.rolname
        """
    ).fetchall()
    if memberships != [("pg_monitor",), ("pg_read_all_data",)]:
        raise BackupIntegrationError("BACKUP_DATABASE_ROLE_MEMBERSHIP_INVALID")


def _assert_finance_backup_database_connect_is_minimal(connection: Any) -> None:
    databases = connection.execute(
        """
        SELECT datname
        FROM pg_catalog.pg_database
        WHERE datallowconn
          AND has_database_privilege(current_user, oid, 'CONNECT')
        ORDER BY datname
        """
    ).fetchall()
    current = connection.execute("SELECT current_database()").fetchone()
    if current is None or databases != [(current[0],)]:
        raise BackupIntegrationError("BACKUP_DATABASE_ROLE_CONNECT_PRIVILEGES_INVALID")


def _assert_isolated_restore_target(
    provider: BackupDatabaseConnectionProvider,
    endpoint: PostgresEndpoint,
    *,
    source_system_identifier: str,
) -> None:
    try:
        with provider.connect(endpoint) as connection:
            identity = connection.execute(
                """
                SELECT current_setting('server_version_num'), system_identifier::text
                FROM pg_catalog.pg_control_system()
                """
            ).fetchone()
            if identity is None or not _is_postgres_17_version(identity[0]):
                raise BackupIntegrationError("BACKUP_RESTORE_TARGET_NOT_POSTGRES_17")
            if not _is_system_identifier(identity[1]):
                raise BackupIntegrationError("BACKUP_RESTORE_TARGET_IDENTITY_INVALID")
            if identity[1] == source_system_identifier:
                raise BackupIntegrationError("BACKUP_RESTORE_TARGET_IS_SOURCE_CLUSTER")
            count = connection.execute(
                """
                WITH user_namespaces AS (
                    SELECT oid, nspname
                    FROM pg_catalog.pg_namespace
                    WHERE nspname NOT IN ('pg_catalog', 'information_schema')
                      AND nspname NOT LIKE 'pg_toast%'
                ), non_system_objects AS (
                    SELECT oid FROM user_namespaces WHERE nspname <> 'public'
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_class AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.relnamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_proc AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.pronamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_type AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.typnamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_collation AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.collnamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_conversion AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.connamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_operator AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.oprnamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_opclass AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.opcnamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_opfamily AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.opfnamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_ts_config AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.cfgnamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_ts_dict AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.dictnamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_ts_parser AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.prsnamespace
                    UNION ALL
                    SELECT object.oid FROM pg_catalog.pg_ts_template AS object
                    JOIN user_namespaces AS namespace ON namespace.oid = object.tmplnamespace
                    UNION ALL
                    SELECT oid FROM pg_catalog.pg_extension WHERE extname <> 'plpgsql'
                    UNION ALL SELECT oid FROM pg_catalog.pg_foreign_data_wrapper
                    UNION ALL SELECT oid FROM pg_catalog.pg_foreign_server
                    UNION ALL SELECT oid FROM pg_catalog.pg_user_mapping
                    UNION ALL SELECT oid FROM pg_catalog.pg_event_trigger
                    UNION ALL SELECT oid FROM pg_catalog.pg_publication
                    UNION ALL SELECT oid FROM pg_catalog.pg_subscription
                    UNION ALL SELECT oid FROM pg_catalog.pg_largeobject_metadata
                    UNION ALL SELECT oid FROM pg_catalog.pg_default_acl
                    UNION ALL
                    SELECT oid FROM pg_catalog.pg_language
                    WHERE lanname NOT IN ('internal', 'c', 'sql', 'plpgsql')
                )
                SELECT count(*) FROM non_system_objects
                """
            ).fetchone()
    except BackupIntegrationError:
        raise
    except Exception as exc:
        raise BackupIntegrationError("BACKUP_RESTORE_TARGET_CHECK_FAILED") from exc
    if count is None or count[0] != 0:
        raise BackupIntegrationError("BACKUP_RESTORE_TARGET_NOT_EMPTY")


def _is_postgres_17_version(value: object) -> bool:
    try:
        version = int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False
    return 170_000 <= version < 180_000


def _is_system_identifier(value: object) -> bool:
    return (
        isinstance(value, str)
        and _SYSTEM_IDENTIFIER_PATTERN.fullmatch(value) is not None
        and int(value) <= 2**64 - 1
    )


def _validated_executable(path: Path) -> Path:
    try:
        candidate = path.resolve(strict=True)
    except OSError as exc:
        raise BackupIntegrationError("BACKUP_POSTGRES_TOOL_UNAVAILABLE") from exc
    if not candidate.is_file() or candidate.is_symlink():
        raise BackupIntegrationError("BACKUP_POSTGRES_TOOL_UNAVAILABLE")
    return candidate


def _valid_postgres_host(host: str) -> bool:
    if not host or len(host) > 253:
        return False
    try:
        ipaddress.ip_address(host)
        return True
    except ValueError:
        pass
    labels = host.removesuffix(".").split(".")
    return bool(labels) and all(
        1 <= len(label) <= 63
        and label[0].isalnum()
        and label[-1].isalnum()
        and all(character.isalnum() or character == "-" for character in label)
        for label in labels
    )


def _validated_pgpass_path(path: Path) -> Path:
    absolute = Path(os.path.abspath(path))
    try:
        candidate = absolute.resolve(strict=True)
    except OSError as exc:
        raise BackupIntegrationError("BACKUP_PGPASS_UNAVAILABLE") from exc
    if candidate != absolute or not candidate.is_file() or candidate.is_symlink():
        raise BackupIntegrationError("BACKUP_PGPASS_UNAVAILABLE")
    return candidate


def _sanitized_pg_environment(pgpass_path: Path | None, application_name: str) -> dict[str, str]:
    environment: dict[str, str] = {}
    for name, value in os.environ.items():
        upper = name.upper()
        if (
            upper.startswith("PG")
            or upper.startswith("FINANCE_")
            or upper in _PROJECT_SECRET_ENVIRONMENT_NAMES
            or upper == "DATABASE_URL"
            or any(marker in upper for marker in ("PASSWORD", "SECRET", "TOKEN", "CREDENTIAL"))
        ):
            continue
        environment[name] = value
    environment["PGAPPNAME"] = application_name
    environment["PGCLIENTENCODING"] = "UTF8"
    if pgpass_path is not None:
        environment["PGPASSFILE"] = str(pgpass_path)
    return environment
