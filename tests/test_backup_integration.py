from __future__ import annotations

import hashlib
import logging.config
import os
import sys
from contextlib import contextmanager
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from sqlalchemy.engine import make_url

from ai_accounting import backup_integration, service_lease, windows_backup
from ai_accounting.backup import (
    BackupError,
    BackupPrecondition,
    BackupRequest,
    DatabaseDumpMetadata,
    EvidenceSnapshot,
    create_stopped_backup,
)
from ai_accounting.backup_integration import (
    BackupIntegrationError,
    PgDumpAdapter,
    PgRestoreAdapter,
    PostgresEndpoint,
    _assert_isolated_restore_target,
    postgres_backup_snapshot,
)
from ai_accounting.windows_backup import (
    WindowsVolumeFacts,
    WindowsWriteThroughPublisher,
    preflight_windows_backup_root,
)

_SOURCE_SYSTEM_IDENTIFIER = "7612345678901234567"


class PgPassProvider:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def lease_pgpass(self, endpoint: PostgresEndpoint):  # type: ignore[no-untyped-def]
        del endpoint
        yield self.path


class AccessVerifier:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.paths: list[Path] = []

    def assert_current_windows_user_only(self, path: Path) -> None:
        self.paths.append(path)
        if self.fail:
            raise BackupIntegrationError("BACKUP_PGPASS_ACL_INVALID")


class RecordingRunner:
    def __init__(self, *, returncode: int = 0) -> None:
        self.returncode = returncode
        self.calls: list[tuple[tuple[str, ...], dict[str, str]]] = []

    def __call__(
        self, argv: tuple[str, ...], *, environment: dict[str, str]
    ) -> CompletedProcess[str]:
        self.calls.append((tuple(argv), environment))
        if "--file" in argv:
            Path(argv[argv.index("--file") + 1]).write_bytes(b"PGDMP\x00test")
        stdout = "123; 0 0 TABLE public evidence finance_backup\n" if "--list" in argv else ""
        return CompletedProcess(argv, self.returncode, stdout=stdout, stderr="secret-error")


def _endpoint(
    *, host: str = "127.0.0.1", port: int = 5432, database: str = "finance_test",
    username: str = "finance_backup", application: str = "finance-backup-test"
) -> PostgresEndpoint:
    return PostgresEndpoint(
        host=host,
        port=port,
        database=database,
        username=username,
        application_name=application,
    )


@pytest.mark.parametrize(
    "host",
    ("db.example@evil", "db.example/path", "db.example?x", "db.example#fragment", "-bad"),
)
def test_postgres_endpoint_rejects_uri_injection_hosts(host: str) -> None:
    with pytest.raises(BackupIntegrationError, match="BACKUP_POSTGRES_ENDPOINT_INVALID"):
        _endpoint(host=host)


def test_passwordless_url_handles_ipv6_without_credential_injection() -> None:
    endpoint = _endpoint(host="2001:db8::1")
    parsed = make_url(endpoint.passwordless_sqlalchemy_url())
    assert parsed.host == endpoint.host
    assert parsed.username == endpoint.username
    assert parsed.password is None


def test_pg_dump_uses_only_password_free_argv_and_sanitized_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "do-not-leak-password"
    pgpass = tmp_path / "protected.pgpass"
    pgpass.write_text(f"*:*:*:*:{secret}\n", encoding="utf-8")
    for name in (
        "PGPASSWORD",
        "PGSERVICE",
        "PGSERVICEFILE",
        "DATABASE_URL",
        "FINANCE_MIGRATION_DATABASE_URL",
        "FINANCE_BACKUP_PASSWORD",
        "SOME_TOKEN",
    ):
        monkeypatch.setenv(name, secret)
    runner = RecordingRunner()
    verifier = AccessVerifier()
    adapter = PgDumpAdapter(
        _endpoint(),
        "00000003-0000001B-1",
        PgPassProvider(pgpass),
        verifier,
        pg_dump_executable=Path(sys.executable),
        pg_restore_executable=Path(sys.executable),
        runner=runner,  # type: ignore[arg-type]
    )

    destination = tmp_path / "database.dump"
    adapter.dump(
        destination,
        DatabaseDumpMetadata("0001_baseline", _SOURCE_SYSTEM_IDENTIFIER),
    )
    assert adapter.list_archive(destination)
    assert verifier.paths == [pgpass.resolve()]
    for argv, environment in runner.calls:
        assert secret not in "\0".join(argv)
        assert secret not in "\0".join(environment.values())
        assert not ({
            "PGPASSWORD",
            "PGSERVICE",
            "PGSERVICEFILE",
            "DATABASE_URL",
            "FINANCE_MIGRATION_DATABASE_URL",
            "FINANCE_BACKUP_PASSWORD",
            "SOME_TOKEN",
        } & environment.keys())
    assert runner.calls[0][1]["PGPASSFILE"] == str(pgpass.resolve())
    assert "--snapshot" in runner.calls[0][0]


def test_pgpass_acl_failure_refuses_before_spawning_pg_dump(tmp_path: Path) -> None:
    pgpass = tmp_path / "protected.pgpass"
    pgpass.write_text("placeholder", encoding="utf-8")
    runner = RecordingRunner()
    adapter = PgDumpAdapter(
        _endpoint(),
        "00000003-0000001B-1",
        PgPassProvider(pgpass),
        AccessVerifier(fail=True),
        pg_dump_executable=Path(sys.executable),
        pg_restore_executable=Path(sys.executable),
        runner=runner,  # type: ignore[arg-type]
    )

    with pytest.raises(BackupIntegrationError, match="BACKUP_PGPASS_ACL_INVALID"):
        adapter.dump(
            tmp_path / "database.dump",
            DatabaseDumpMetadata("0001_baseline", _SOURCE_SYSTEM_IDENTIFIER),
        )
    assert runner.calls == []


def test_restore_rejects_operational_database_even_with_a_different_username(
    tmp_path: Path,
) -> None:
    pgpass = tmp_path / "protected.pgpass"
    pgpass.write_text("placeholder", encoding="utf-8")
    operational = _endpoint(username="finance_runtime")
    with pytest.raises(
        BackupIntegrationError, match="BACKUP_RESTORE_TARGET_IS_OPERATIONAL_DATABASE"
    ):
        PgRestoreAdapter(
            _endpoint(username="restore_admin"),
            operational,
            PgPassProvider(pgpass),
            AccessVerifier(),
            pg_restore_executable=Path(sys.executable),
        )


def test_alembic_check_preserves_repository_logging_configuration(tmp_path: Path) -> None:
    pgpass = tmp_path / "protected.pgpass"
    pgpass.write_text("placeholder", encoding="utf-8")
    target = _endpoint(
        port=5433,
        database="restore_test",
        username="restore_admin",
        application="restore-check",
    )

    def runner(
        argv: tuple[str, ...], *, environment: dict[str, str]
    ) -> CompletedProcess[str]:
        del environment
        config_path = Path(argv[argv.index("-c") + 1])
        generated = config_path.read_text(encoding="utf-8")
        assert target.passwordless_sqlalchemy_url() in generated
        assert "finance:finance" not in generated
        logging.config.fileConfig(config_path, disable_existing_loggers=False)
        return CompletedProcess(argv, 0, stdout="", stderr="")

    adapter = PgRestoreAdapter(
        target,
        _endpoint(username="finance_runtime"),
        PgPassProvider(pgpass),
        AccessVerifier(),
        pg_restore_executable=Path(sys.executable),
        runner=runner,
    )

    adapter.alembic_check(Path(__file__).parents[1])


class Rows:
    def __init__(self, rows: list[tuple[object, ...]]) -> None:
        self.rows = rows

    def fetchone(self):  # type: ignore[no-untyped-def]
        return self.rows[0] if self.rows else None

    def fetchall(self):  # type: ignore[no-untyped-def]
        return self.rows


class FakeTransaction:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *args: object) -> None:
        return None


class SnapshotConnection:
    def __init__(
        self,
        *,
        active: int = 0,
        memberships: list[tuple[object, ...]] | None = None,
        connect_databases: list[tuple[object, ...]] | None = None,
    ) -> None:
        self.active = active
        self.memberships = memberships or [("pg_monitor",), ("pg_read_all_data",)]
        self.connect_databases = connect_databases or [("finance_test",)]
        self.sql: list[str] = []

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    def execute(self, sql: str, parameters: object = None) -> Rows:
        del parameters
        normalized = " ".join(sql.split())
        self.sql.append(normalized)
        if normalized == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY":
            return Rows([])
        if normalized == "SELECT current_user":
            return Rows([("finance_backup",)])
        if "FROM pg_catalog.pg_roles" in normalized and "rolcanlogin" in normalized:
            return Rows([(True, True, False, False, False, False, False)])
        if "FROM pg_catalog.pg_auth_members" in normalized:
            return Rows(self.memberships)
        if "FROM pg_catalog.pg_database" in normalized:
            return Rows(self.connect_databases)
        if normalized == "SELECT current_database()":
            return Rows([("finance_test",)])
        if "pg_stat_activity" in normalized:
            return Rows([(self.active,)])
        if "pg_control_system()" in normalized:
            return Rows([("170005", _SOURCE_SYSTEM_IDENTIFIER)])
        if "alembic_version" in normalized:
            return Rows([("0001_baseline",)])
        if "FROM evidence" in normalized:
            return Rows(
                [
                    (
                        "0c6f0e54-0556-45ba-b029-1c07dd30d617",
                        "a" * 64,
                        12,
                        os.fspath(Path("D:/evidence/a")),
                    )
                ]
            )
        if "pg_export_snapshot" in normalized:
            return Rows([("00000003-0000001B-1",)])
        raise AssertionError(normalized)


class SnapshotProvider:
    def __init__(self, connection: SnapshotConnection) -> None:
        self.connection = connection

    @contextmanager
    def connect(self, endpoint: PostgresEndpoint):  # type: ignore[no-untyped-def]
        del endpoint
        yield self.connection


class RestoreTargetConnection:
    def __init__(
        self,
        *,
        version: str = "170005",
        system_identifier: str = "7712345678901234567",
        object_count: int = 0,
        expected_catalog_token: str | None = None,
    ) -> None:
        self.version = version
        self.system_identifier = system_identifier
        self.object_count = object_count
        self.expected_catalog_token = expected_catalog_token

    def execute(self, sql: str) -> Rows:
        normalized = " ".join(sql.split())
        if "pg_control_system()" in normalized:
            return Rows([(self.version, self.system_identifier)])
        if "WITH user_namespaces" in normalized:
            if self.expected_catalog_token is not None:
                assert self.expected_catalog_token in normalized
            return Rows([(self.object_count,)])
        raise AssertionError(normalized)


@pytest.mark.parametrize(
    ("endpoint", "connection", "code"),
    (
        (
            _endpoint(host="localhost", database="restore_alias", username="restore_admin"),
            RestoreTargetConnection(system_identifier=_SOURCE_SYSTEM_IDENTIFIER),
            "BACKUP_RESTORE_TARGET_IS_SOURCE_CLUSTER",
        ),
        (
            _endpoint(database="other_database", username="restore_admin"),
            RestoreTargetConnection(system_identifier=_SOURCE_SYSTEM_IDENTIFIER),
            "BACKUP_RESTORE_TARGET_IS_SOURCE_CLUSTER",
        ),
        (
            _endpoint(database="restore_pg16", username="restore_admin"),
            RestoreTargetConnection(version="160010"),
            "BACKUP_RESTORE_TARGET_NOT_POSTGRES_17",
        ),
        (
            _endpoint(database="restore_function", username="restore_admin"),
            RestoreTargetConnection(
                object_count=1, expected_catalog_token="pg_catalog.pg_proc"
            ),
            "BACKUP_RESTORE_TARGET_NOT_EMPTY",
        ),
        (
            _endpoint(database="restore_schema", username="restore_admin"),
            RestoreTargetConnection(
                object_count=1, expected_catalog_token="nspname <> 'public'"
            ),
            "BACKUP_RESTORE_TARGET_NOT_EMPTY",
        ),
    ),
)
def test_restore_target_rejects_cluster_alias_version_and_non_system_pollution(
    endpoint: PostgresEndpoint,
    connection: RestoreTargetConnection,
    code: str,
) -> None:
    with pytest.raises(BackupIntegrationError, match=code):
        _assert_isolated_restore_target(
            SnapshotProvider(connection),  # type: ignore[arg-type]
            endpoint,
            source_system_identifier=_SOURCE_SYSTEM_IDENTIFIER,
        )


def test_restore_target_accepts_only_empty_pg17_different_cluster() -> None:
    _assert_isolated_restore_target(
        SnapshotProvider(RestoreTargetConnection()),  # type: ignore[arg-type]
        _endpoint(database="restore_empty", username="restore_admin"),
        source_system_identifier=_SOURCE_SYSTEM_IDENTIFIER,
    )


class RestoredConnection:
    def execute(self, sql: str) -> Rows:
        normalized = " ".join(sql.split())
        if "FROM alembic_version" in normalized:
            return Rows([("0001_baseline",)])
        if "FROM evidence" in normalized:
            return Rows([])
        raise AssertionError(normalized)


class VerifiedCopyLease:
    def __init__(self, copy: Path) -> None:
        self.copy = copy
        self.active = False
        self.arguments: tuple[object, ...] | None = None

    @contextmanager
    def lease_verified_archive(
        self,
        source: Path,
        source_root: Path,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
    ):  # type: ignore[no-untyped-def]
        self.arguments = (
            source,
            source_root,
            expected_sha256,
            expected_size_bytes,
        )
        self.active = True
        try:
            yield self.copy
        finally:
            self.active = False


class CopyOnlyRestoreAdapter:
    def __init__(self, endpoint: PostgresEndpoint, lease: VerifiedCopyLease) -> None:
        self.endpoint = endpoint
        self.lease = lease
        self.paths: list[Path] = []
        self.checked = False

    def list_archive(self, archive: Path) -> tuple[str, ...]:
        assert self.lease.active
        assert archive == self.lease.copy
        self.paths.append(archive)
        return ("TABLE public.evidence",)

    def restore(self, archive: Path) -> None:
        assert self.lease.active
        assert archive == self.lease.copy
        self.paths.append(archive)

    def alembic_check(self, repository_root: Path) -> None:
        del repository_root
        assert not self.lease.active
        self.checked = True


def test_restore_drill_lists_and_restores_only_the_leased_verified_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    complete = tmp_path / "one.complete"
    complete.mkdir()
    source = complete / "database.dump"
    source.write_bytes(b"untrusted-removable-path")
    local_copy = tmp_path / "current-user-only.dump"
    local_copy.write_bytes(b"verified-local-copy")
    digest = hashlib.sha256(b"verified-local-copy").hexdigest()
    verified = type(
        "Verified",
        (),
        {
            "backup_directory": complete,
            "backup_id": "one",
            "schema_revision": "0001_baseline",
            "source_system_identifier": _SOURCE_SYSTEM_IDENTIFIER,
            "database_sha256": digest,
            "database_size_bytes": len(b"verified-local-copy"),
            "evidence": (),
        },
    )()
    monkeypatch.setattr(backup_integration, "verify_backup", lambda *args: verified)
    monkeypatch.setattr(
        backup_integration,
        "_single_alembic_head",
        lambda root: "0001_baseline",
    )
    monkeypatch.setattr(
        backup_integration,
        "_assert_isolated_restore_target",
        lambda *args, **kwargs: None,
    )
    lease = VerifiedCopyLease(local_copy)
    adapter = CopyOnlyRestoreAdapter(
        _endpoint(port=5433, database="restore", username="restore_admin"), lease
    )

    result = backup_integration.run_isolated_restore_drill(
        tmp_path,
        complete,
        operational_endpoint=_endpoint(username="finance_runtime"),
        restore_adapter=adapter,  # type: ignore[arg-type]
        connection_provider=SnapshotProvider(RestoredConnection()),  # type: ignore[arg-type]
        archive_copy_provider=lease,
        repository_root=tmp_path,
    )

    assert result.backup_id == "one"
    assert adapter.paths == [local_copy, local_copy]
    assert adapter.checked
    assert lease.arguments == (source, complete, digest, len(b"verified-local-copy"))


def test_finance_backup_sql_requires_dedicated_cluster_and_exact_connect_scope() -> None:
    script = (
        Path(__file__).parents[1] / "deploy" / "windows" / "finance_backup.sql"
    ).read_text(encoding="utf-8")
    assert "finance_dedicated_local_cluster" in script
    assert "REVOKE CONNECT ON DATABASE postgres FROM PUBLIC" in script
    assert "REVOKE CONNECT ON DATABASE template1 FROM PUBLIC" in script
    assert "FINANCE_BACKUP_ROLE_CONNECT_PRIVILEGES_INVALID" in script
    assert "BEGIN;" in script and "COMMIT;" in script


def test_evidence_projection_and_dump_snapshot_share_one_read_only_transaction() -> None:
    connection = SnapshotConnection()
    with postgres_backup_snapshot(
        SnapshotProvider(connection), _endpoint(), runtime_role="finance_runtime"
    ) as snapshot:
        assert snapshot.schema_revision == "0001_baseline"
        assert snapshot.source_system_identifier == _SOURCE_SYSTEM_IDENTIFIER
        assert snapshot.snapshot_id == "00000003-0000001B-1"
        assert snapshot.evidence[0].sha256 == "a" * 64
    assert connection.sql[0] == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert connection.sql[-1] == "SELECT pg_export_snapshot()"


def test_any_remaining_runtime_session_blocks_snapshot() -> None:
    with pytest.raises(BackupIntegrationError, match="BACKUP_ACTIVE_CONNECTIONS"):
        with postgres_backup_snapshot(
            SnapshotProvider(SnapshotConnection(active=1)),
            _endpoint(),
            runtime_role="finance_runtime",
        ):
            pytest.fail("snapshot must not be yielded")


def test_any_extra_finance_backup_role_membership_blocks_snapshot() -> None:
    connection = SnapshotConnection(
        memberships=[("pg_monitor",), ("pg_read_all_data",), ("pg_signal_backend",)]
    )
    with pytest.raises(
        BackupIntegrationError, match="BACKUP_DATABASE_ROLE_MEMBERSHIP_INVALID"
    ):
        with postgres_backup_snapshot(
            SnapshotProvider(connection),
            _endpoint(),
            runtime_role="finance_runtime",
        ):
            pytest.fail("privilege-polluted backup role must not yield a snapshot")


def test_finance_backup_connect_on_any_other_database_blocks_snapshot() -> None:
    connection = SnapshotConnection(
        connect_databases=[("finance_test",), ("postgres",)]
    )
    with pytest.raises(
        BackupIntegrationError,
        match="BACKUP_DATABASE_ROLE_CONNECT_PRIVILEGES_INVALID",
    ):
        with postgres_backup_snapshot(
            SnapshotProvider(connection),
            _endpoint(),
            runtime_role="finance_runtime",
        ):
            pytest.fail("cross-database CONNECT must not yield a snapshot")


class FakeDumpAdapter:
    def dump(self, destination: Path, metadata: DatabaseDumpMetadata) -> None:
        del metadata
        destination.write_bytes(b"PGDMP\x00data")

    def list_archive(self, archive: Path) -> tuple[str, ...]:
        del archive
        return ("TABLE public.evidence",)


class FailingPublisher:
    def publish(self, partial: Path, complete: Path, root: Path) -> None:
        del complete, root
        assert partial.is_dir()
        raise BackupError("BACKUP_PUBLISH_FAILED")


def test_durable_publisher_failure_keeps_partial_and_never_creates_complete(
    tmp_path: Path,
) -> None:
    evidence_root = tmp_path / "evidence"
    evidence_root.mkdir()
    content = b"evidence"
    source = evidence_root / "source.bin"
    source.write_bytes(content)
    request = BackupRequest(
        backup_id="durable-failure",
        purpose="daily",
        precondition=BackupPrecondition(True, 0),
        evidence_root=evidence_root,
        evidence=(
            EvidenceSnapshot(
                "0c6f0e54-0556-45ba-b029-1c07dd30d617",
                hashlib.sha256(content).hexdigest(),
                len(content),
                source,
            ),
        ),
        database=DatabaseDumpMetadata(
            "0001_baseline", _SOURCE_SYSTEM_IDENTIFIER
        ),
    )
    root = tmp_path / "media"

    with pytest.raises(BackupError, match="BACKUP_PUBLISH_FAILED"):
        create_stopped_backup(root, request, FakeDumpAdapter(), publisher=FailingPublisher())
    assert (root / "durable-failure.partial").is_dir()
    assert not (root / "durable-failure.complete").exists()


class FakeVolumeProvider:
    def __init__(self, facts: WindowsVolumeFacts) -> None:
        self.facts = facts

    def inspect(self, path: Path) -> WindowsVolumeFacts:
        del path
        return self.facts


class ProbePublisher:
    def __init__(self) -> None:
        self.probed: Path | None = None

    def durable_directory_preflight(self, root: Path) -> None:
        self.probed = root


def test_windows_preflight_requires_removable_fully_encrypted_volume(tmp_path: Path) -> None:
    publisher = ProbePublisher()
    facts = WindowsVolumeFacts(tmp_path, 2, "NTFS", "FullyEncrypted", "On", 100)
    assert preflight_windows_backup_root(
        tmp_path, FakeVolumeProvider(facts), publisher  # type: ignore[arg-type]
    ) == facts
    assert publisher.probed == tmp_path.resolve()

    for rejected, code in (
        (WindowsVolumeFacts(tmp_path, 3, "NTFS", "FullyEncrypted", "On", 100), "REMOVABLE"),
        (WindowsVolumeFacts(tmp_path, 2, "NTFS", "EncryptionInProgress", "On", 99), "ENCRYPTED"),
    ):
        with pytest.raises(BackupIntegrationError, match=f"BACKUP_VOLUME_NOT_{code}"):
            preflight_windows_backup_root(
                tmp_path, FakeVolumeProvider(rejected), publisher  # type: ignore[arg-type]
            )


def test_windows_publisher_api_failure_leaves_partial_unpublished(tmp_path: Path) -> None:
    partial = tmp_path / "one.partial"
    complete = tmp_path / "one.complete"
    partial.mkdir()
    publisher = object.__new__(WindowsWriteThroughPublisher)
    publisher._move_write_through = lambda source, destination: False  # type: ignore[method-assign]

    with pytest.raises(BackupError, match="BACKUP_PUBLISH_FAILED"):
        publisher.publish(partial, complete, tmp_path)
    assert partial.is_dir()
    assert not complete.exists()


class FunctionStub:
    argtypes: object = None
    restype: object = None


class Kernel32Stub:
    GetDriveTypeW = FunctionStub()
    GetVolumePathNameW = FunctionStub()
    GetVolumeInformationW = FunctionStub()
    MoveFileExW = FunctionStub()


def test_windows_kernel32_volume_and_move_signatures_are_explicit() -> None:
    kernel32 = Kernel32Stub()
    windows_backup._configure_kernel32(kernel32)
    assert kernel32.GetDriveTypeW.restype is not None
    assert len(kernel32.GetVolumePathNameW.argtypes) == 3
    assert len(kernel32.GetVolumeInformationW.argtypes) == 8
    assert len(kernel32.MoveFileExW.argtypes) == 3
    assert kernel32.MoveFileExW.restype is not None


class LeaseAclVerifier:
    def assert_current_windows_user_only(self, path: Path) -> None:
        assert path.is_file()


@pytest.mark.skipif(sys.platform != "win32", reason="LockFileEx is Windows-only")
def test_windows_service_and_backup_modes_are_cross_process_exclusive(tmp_path: Path) -> None:
    lock_file = tmp_path / "service.lock"
    lock_file.write_bytes(b"\x00")
    verifier = LeaseAclVerifier()

    with service_lease.acquire_windows_service_lease(
        lock_file, mode="service", access_verifier=verifier
    ):
        with pytest.raises(service_lease.ServiceLeaseError, match="SERVICE_LEASE_HELD"):
            with service_lease.acquire_windows_service_lease(
                lock_file, mode="backup", access_verifier=verifier
            ):
                pytest.fail("exclusive backup lease must not be yielded")
    with service_lease.acquire_windows_service_lease(
        lock_file, mode="backup", access_verifier=verifier
    ):
        pass


class LeaseKernel32Stub:
    LockFileEx = FunctionStub()
    UnlockFileEx = FunctionStub()


def test_windows_service_lease_lock_signatures_are_explicit() -> None:
    kernel32 = LeaseKernel32Stub()
    service_lease._configure_kernel32(kernel32)
    assert len(kernel32.LockFileEx.argtypes) == 6
    assert kernel32.LockFileEx.restype is not None
    assert len(kernel32.UnlockFileEx.argtypes) == 5
    assert kernel32.UnlockFileEx.restype is not None


@pytest.mark.skipif(sys.platform != "win32", reason="LockFileEx is Windows-only")
def test_service_lease_rejects_lock_file_replaced_between_lstat_and_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    lock_file = tmp_path / "service.lock"
    replaced = tmp_path / "replaced.lock"
    lock_file.write_bytes(b"original")
    original_open = service_lease.os.open

    def replacing_open(path: object, flags: int) -> int:
        os.replace(lock_file, replaced)
        lock_file.write_bytes(b"replacement")
        return original_open(path, flags)

    monkeypatch.setattr(service_lease.os, "open", replacing_open)
    with pytest.raises(service_lease.ServiceLeaseError, match="SERVICE_LEASE_UNAVAILABLE"):
        with service_lease.acquire_windows_service_lease(
            lock_file,
            mode="backup",
            access_verifier=LeaseAclVerifier(),
        ):
            pytest.fail("replaced lock file must not be leased")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ACL is Windows-only")
def test_pgpass_acl_rejects_any_other_sid_allow_ace(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pgpass = tmp_path / "protected.pgpass"
    pgpass.write_text("placeholder", encoding="utf-8")
    current_sid = "S-1-5-21-1000"
    monkeypatch.setattr(
        windows_backup,
        "_windows_acl_facts",
        lambda path: (current_sid, current_sid, True, (current_sid, "S-1-5-21-2000")),
    )
    with pytest.raises(BackupIntegrationError, match="BACKUP_PGPASS_ACL_INVALID"):
        windows_backup.WindowsCurrentUserOnlyAclVerifier().assert_current_windows_user_only(pgpass)
