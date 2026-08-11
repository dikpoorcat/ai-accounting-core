from __future__ import annotations

import hashlib
import io
import os
import shutil
import subprocess
import sys
import tarfile
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import create_engine
from sqlalchemy.engine import make_url
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.backup_integration import (
    BackupIntegrationError,
    PgDumpAdapter,
    PgRestoreAdapter,
    PostgresEndpoint,
    create_integrated_stopped_backup,
    postgres_backup_snapshot,
    run_isolated_restore_drill,
)
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


class PsycopgConnectionProvider:
    """Test-only provider; secrets stay in this fixture and never reach integration objects."""

    def __init__(self, connections: dict[tuple[str, int, str, str], str]) -> None:
        self._connections = connections

    @contextmanager
    def connect(self, endpoint: PostgresEndpoint):  # type: ignore[no-untyped-def]
        key = (endpoint.host, endpoint.port, endpoint.database, endpoint.username)
        url = make_url(self._connections[key]).set(drivername="postgresql")
        with psycopg.connect(url.render_as_string(hide_password=False)) as connection:
            yield connection


class StaticPgPassProvider:
    def __init__(self, paths: dict[tuple[str, int, str, str], Path]) -> None:
        self._paths = paths

    @contextmanager
    def lease_pgpass(self, endpoint: PostgresEndpoint):  # type: ignore[no-untyped-def]
        key = (endpoint.host, endpoint.port, endpoint.database, endpoint.username)
        yield self._paths[key]


class TestAclVerifier:
    def assert_current_windows_user_only(self, path: Path) -> None:
        assert path.is_file()


class BackupLease:
    @contextmanager
    def acquire_backup_lease(self):  # type: ignore[no-untyped-def]
        yield


class PortablePublisher:
    def publish(self, partial: Path, complete: Path, root: Path) -> None:
        assert partial.parent == root
        assert complete.parent == root
        os.replace(partial, complete)


class PortableVerifiedArchiveCopyProvider:
    def __init__(self, lease_root: Path) -> None:
        self.lease_root = lease_root

    @contextmanager
    def lease_verified_archive(
        self,
        source: Path,
        source_root: Path,
        *,
        expected_sha256: str,
        expected_size_bytes: int,
    ):  # type: ignore[no-untyped-def]
        assert source.parent == source_root
        self.lease_root.mkdir()
        copied = self.lease_root / "database.dump"
        copied.write_bytes(source.read_bytes())
        content = copied.read_bytes()
        assert hashlib.sha256(content).hexdigest() == expected_sha256
        assert len(content) == expected_size_bytes
        try:
            yield copied
        finally:
            copied.unlink()
            self.lease_root.rmdir()


class DockerPgToolRunner:
    """Run the adapter's PostgreSQL client operation inside its PG17 container."""

    def __init__(self, container: PostgresContainer) -> None:
        self.container = container
        self.commands: list[tuple[str, ...]] = []
        self.last_failure_category = "none"

    def __call__(
        self, argv: tuple[str, ...], *, environment: dict[str, str]
    ) -> subprocess.CompletedProcess[str]:
        self.commands.append(tuple(argv))
        if len(argv) >= 3 and argv[:3] == (sys.executable, "-m", "alembic"):
            result = subprocess.run(
                argv,
                check=False,
                shell=False,
                stdin=subprocess.DEVNULL,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                env=environment,
            )
            if result.returncode != 0:
                self.last_failure_category = _safe_failure_category(
                    result.stdout + result.stderr
                )
            return result
        if "--format=custom" in argv:
            return self._dump(argv)
        if "--list" in argv:
            return self._list(argv)
        if "--exit-on-error" in argv:
            return self._restore(argv)
        raise AssertionError(argv)

    def _dump(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        destination = Path(argv[argv.index("--file") + 1])
        container_archive = "/tmp/finance-backup.dump"
        translated = self._translate_connection(argv[1:])
        translated[translated.index("--file") + 1] = container_archive
        result = self.container.exec(["pg_dump", *translated])
        if result.exit_code == 0:
            if not self._copy_from_container(container_archive, destination):
                self.last_failure_category = "docker-copy"
                return subprocess.CompletedProcess(argv, 1, "", "copy failed")
        return self._completed(argv, result)

    def _list(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        archive = Path(argv[-1])
        container_archive = "/tmp/finance-list.dump"
        if not self._copy_to_container(archive, container_archive):
            self.last_failure_category = "docker-copy"
            return subprocess.CompletedProcess(argv, 1, "", "copy failed")
        return self._completed(
            argv, self.container.exec(["pg_restore", "--list", container_archive])
        )

    def _restore(self, argv: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
        archive = Path(argv[-1])
        container_archive = "/tmp/finance-restore.dump"
        if not self._copy_to_container(archive, container_archive):
            self.last_failure_category = "docker-copy"
            return subprocess.CompletedProcess(argv, 1, "", "copy failed")
        translated = self._translate_connection(argv[1:-1])
        result = self.container.exec(["pg_restore", *translated, container_archive])
        return self._completed(argv, result)

    @staticmethod
    def _translate_connection(arguments: tuple[str, ...]) -> list[str]:
        translated = list(arguments)
        translated[translated.index("--host") + 1] = "/var/run/postgresql"
        port_index = translated.index("--port")
        del translated[port_index : port_index + 2]
        return translated

    def _copy_from_container(self, source: str, destination: Path) -> bool:
        try:
            chunks, _ = self.container.get_wrapped_container().get_archive(source)
            archive_bytes = b"".join(chunks)
            with tarfile.open(fileobj=io.BytesIO(archive_bytes), mode="r:") as archive:
                members = archive.getmembers()
                if len(members) != 1 or not members[0].isfile():
                    return False
                source_file = archive.extractfile(members[0])
                if source_file is None:
                    return False
                destination.write_bytes(source_file.read())
            return True
        except (OSError, tarfile.TarError):
            return False

    def _copy_to_container(self, source: Path, destination: str) -> bool:
        try:
            content = source.read_bytes()
            stream = io.BytesIO()
            name = Path(destination).name
            with tarfile.open(fileobj=stream, mode="w") as archive:
                member = tarfile.TarInfo(name)
                member.size = len(content)
                member.mode = 0o600
                archive.addfile(member, io.BytesIO(content))
            return bool(
                self.container.get_wrapped_container().put_archive(
                    str(Path(destination).parent).replace("\\", "/"), stream.getvalue()
                )
            )
        except OSError:
            return False

    def _completed(
        self, argv: tuple[str, ...], result: Any
    ) -> subprocess.CompletedProcess[str]:
        output = result.output.decode("utf-8", errors="replace")
        if result.exit_code != 0:
            lowered = output.casefold()
            self.last_failure_category = next(
                (
                    category
                    for marker, category in (
                        ("snapshot", "snapshot"),
                        ("permission denied", "permission"),
                        ("authentication failed", "authentication"),
                        ("does not exist", "missing-object"),
                    )
                    if marker in lowered
                ),
                "unclassified",
            )
        return subprocess.CompletedProcess(argv, result.exit_code, output, output)


def _endpoint_and_url(
    container: PostgresContainer,
    *,
    username: str | None = None,
    password: str | None = None,
    application: str,
) -> tuple[PostgresEndpoint, str]:
    url = make_url(container.get_connection_url(driver="psycopg"))
    selected_user = username or url.username
    selected_password = password if password is not None else url.password
    assert selected_user is not None and selected_password is not None and url.database is not None
    endpoint = PostgresEndpoint(
        host=url.host or "127.0.0.1",
        port=url.port or 5432,
        database=url.database,
        username=selected_user,
        application_name=application,
    )
    connection_url = url.set(username=selected_user, password=selected_password).render_as_string(
        hide_password=False
    )
    return endpoint, connection_url


def _write_pgpass(path: Path, endpoint: PostgresEndpoint, password: str) -> None:
    path.write_text(
        f"{endpoint.host}:{endpoint.port}:{endpoint.database}:{endpoint.username}:{password}\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)


def _endpoint_key(endpoint: PostgresEndpoint) -> tuple[str, int, str, str]:
    return endpoint.host, endpoint.port, endpoint.database, endpoint.username


def _safe_failure_category(output: str) -> str:
    lowered = output.casefold()
    if any(
        marker in lowered
        for marker in ("authentication failed", "no password supplied", "fe_sendauth")
    ):
        return "authentication"
    if any(
        marker in lowered
        for marker in ("connection refused", "could not connect", "server closed")
    ):
        return "connection"
    if any(
        marker in lowered
        for marker in ("new upgrade operations detected", "detected added", "detected removed")
    ):
        categories = tuple(
            category
            for markers, category in (
                (("add_table", "remove_table", "added table", "removed table"), "table"),
                (("add_column", "remove_column", "added column", "removed column"), "column"),
                (("add_fk", "remove_fk", "foreign key"), "foreign-key"),
                (("add_index", "remove_index", "index"), "index"),
                (("add_constraint", "remove_constraint", "constraint"), "constraint"),
            )
            if any(marker in lowered for marker in markers)
        )
        return "schema-diff:" + (",".join(categories) if categories else "other")
    return "unclassified"


def test_alembic_checker_safe_diagnostic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINANCE_MIGRATION_DATABASE_URL", raising=False)
    repository_root = Path(__file__).parents[1]
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as target:  # noqa: E501
        endpoint, target_url = _endpoint_and_url(
            target, application="finance-alembic-check-diagnostic"
        )
        config = Config(str(repository_root / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", target_url)
        command.upgrade(config, "head")
        target_parts = make_url(target_url)
        assert target_parts.password is not None
        pgpass = tmp_path / "diagnostic.pgpass"
        _write_pgpass(pgpass, endpoint, target_parts.password)
        provider = StaticPgPassProvider({_endpoint_key(endpoint): pgpass})
        runner = DockerPgToolRunner(target)
        operational = PostgresEndpoint(
            host=endpoint.host,
            port=endpoint.port,
            database="operational_database",
            username="finance_backup",
            application_name="operational-placeholder",
        )
        adapter = PgRestoreAdapter(
            endpoint,
            operational,
            provider,
            TestAclVerifier(),
            pg_restore_executable=Path(sys.executable),
            runner=runner,  # type: ignore[arg-type]
        )
        try:
            adapter.alembic_check(repository_root)
        except BackupIntegrationError:
            pytest.fail(f"alembic failure category: {runner.last_failure_category}")


def test_pg17_dump_restore_alembic_head_and_evidence_cross_validation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("FINANCE_MIGRATION_DATABASE_URL", raising=False)
    repository_root = Path(__file__).parents[1]
    evidence_root = tmp_path / "evidence-store"
    evidence_path = evidence_root / "ab" / "invoice.bin"
    evidence_path.parent.mkdir(parents=True)
    evidence_path.write_bytes(b"restorable-invoice-evidence")
    backup_password = "test-only-finance-backup-password"

    with (
        PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as source,  # noqa: E501
        PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as target,  # noqa: E501
    ):
        source_admin_endpoint, source_admin_url = _endpoint_and_url(
            source, application="source-admin"
        )
        target_endpoint, target_url = _endpoint_and_url(
            target, application="finance-restore-drill"
        )
        config = Config(str(repository_root / "alembic.ini"))
        config.set_main_option("sqlalchemy.url", source_admin_url)
        command.upgrade(config, "head")

        source_engine = create_engine(source_admin_url)
        try:
            evidence_id = uuid.uuid4()
            organization_id = uuid.uuid4()
            with source_engine.begin() as connection:
                connection.exec_driver_sql(
                    """
                    INSERT INTO organizations (
                        id, name, taxpayer_type, filing_cycle, jurisdiction,
                        urban_maintenance_rate, accounting_standard,
                        accounting_period_control_enabled, created_at
                    ) VALUES (
                        %s, %s, 'small_scale', 'quarterly', 'CN', 0.07,
                        'small_enterprise', true, %s
                    )
                    """,
                    (organization_id, "Backup restore integration", datetime.now(UTC)),
                )
                connection.exec_driver_sql(
                    """
                    INSERT INTO evidence (
                        id, org_id, sha256, original_name, media_type, source,
                        size_bytes, storage_path, metadata, created_at
                    ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, '{}'::json, %s)
                    """,
                    (
                        evidence_id,
                        organization_id,
                        "4656152a3e214ccb39b39c0542121a0e45ac693bd997faa6fe891795331a331a",
                        "invoice.bin",
                        "application/octet-stream",
                        "restore_test",
                        evidence_path.stat().st_size,
                        str(evidence_path),
                        datetime.now(UTC),
                    ),
                )
                connection.exec_driver_sql(
                    "CREATE ROLE finance_backup LOGIN INHERIT NOSUPERUSER NOCREATEDB "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS PASSWORD "
                    f"'{backup_password}'"
                )
                connection.exec_driver_sql("GRANT pg_read_all_data TO finance_backup")
                connection.exec_driver_sql("GRANT pg_monitor TO finance_backup")
                connection.exec_driver_sql("REVOKE CONNECT ON DATABASE postgres FROM PUBLIC")
                connection.exec_driver_sql("REVOKE CONNECT ON DATABASE template1 FROM PUBLIC")
        finally:
            source_engine.dispose()

        source_endpoint, source_backup_url = _endpoint_and_url(
            source,
            username="finance_backup",
            password=backup_password,
            application="finance-backup-integration",
        )
        source_pgpass = tmp_path / "source.pgpass"
        target_pgpass = tmp_path / "target.pgpass"
        target_url_parts = make_url(target_url)
        assert target_url_parts.password is not None
        _write_pgpass(source_pgpass, source_endpoint, backup_password)
        _write_pgpass(target_pgpass, target_endpoint, target_url_parts.password)

        keys = {
            _endpoint_key(source_endpoint): source_backup_url,
            _endpoint_key(target_endpoint): target_url,
        }
        pgpasses = {
            _endpoint_key(source_endpoint): source_pgpass,
            _endpoint_key(target_endpoint): target_pgpass,
        }
        connection_provider = PsycopgConnectionProvider(keys)
        pgpass_provider = StaticPgPassProvider(pgpasses)
        privilege_engine = create_engine(source_admin_url)
        try:
            with privilege_engine.begin() as connection:
                connection.exec_driver_sql("GRANT pg_signal_backend TO finance_backup")
            with pytest.raises(
                BackupIntegrationError,
                match="BACKUP_DATABASE_ROLE_MEMBERSHIP_INVALID",
            ):
                with postgres_backup_snapshot(
                    connection_provider,
                    source_endpoint,
                    runtime_role=source_admin_endpoint.username,
                ):
                    pytest.fail("polluted finance_backup role must not yield a snapshot")
            with privilege_engine.begin() as connection:
                connection.exec_driver_sql("REVOKE pg_signal_backend FROM finance_backup")
        finally:
            privilege_engine.dispose()
        source_runner = DockerPgToolRunner(source)
        target_runner = DockerPgToolRunner(target)
        tool_placeholder = Path(sys.executable)

        try:
            verification = create_integrated_stopped_backup(
                tmp_path / "encrypted-removable-media",
                backup_id="pg17-restore-drill",
                purpose="pre_upgrade",
                evidence_root=evidence_root,
                endpoint=source_endpoint,
                runtime_role=source_admin_endpoint.username,
                service_lease=BackupLease(),
                connection_provider=connection_provider,
                adapter_factory=lambda snapshot_id: PgDumpAdapter(
                    source_endpoint,
                    snapshot_id,
                    pgpass_provider,
                    TestAclVerifier(),
                    pg_dump_executable=tool_placeholder,
                    pg_restore_executable=tool_placeholder,
                    runner=source_runner,  # type: ignore[arg-type]
                ),
                publisher=PortablePublisher(),
            )
        except BackupIntegrationError:
            pytest.fail(f"pg_dump failure category: {source_runner.last_failure_category}")
        restore_adapter = PgRestoreAdapter(
            target_endpoint,
            source_endpoint,
            pgpass_provider,
            TestAclVerifier(),
            pg_restore_executable=tool_placeholder,
            runner=target_runner,  # type: ignore[arg-type]
        )
        drill = run_isolated_restore_drill(
            verification.backup_directory.parent,
            verification.backup_directory,
            operational_endpoint=source_endpoint,
            restore_adapter=restore_adapter,
            connection_provider=connection_provider,
            archive_copy_provider=PortableVerifiedArchiveCopyProvider(
                tmp_path / "local-restore-copy"
            ),
            repository_root=repository_root,
        )

        assert drill.backup_id == "pg17-restore-drill"
        assert drill.schema_revision == verification.schema_revision
        assert drill.evidence_count == 1
        assert any("--snapshot" in command_line for command_line in source_runner.commands)
        alembic_commands = [
            command_line
            for command_line in target_runner.commands
            if command_line[:3] == (sys.executable, "-m", "alembic")
        ]
        assert len(alembic_commands) == 1
        assert alembic_commands[0][-1] == "check"
        assert "upgrade" not in alembic_commands[0]
