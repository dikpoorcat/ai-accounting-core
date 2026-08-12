from __future__ import annotations

import hashlib
import os
import sys
import uuid
from contextlib import AbstractContextManager
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from ai_accounting import backup_cli, backup_credentials
from ai_accounting.backup_credentials import (
    BackupCredentialError,
    WindowsFinanceBackupCredentialStore,
    WindowsProtectedArchiveCopyProvider,
    WindowsProtectedPgPassProvider,
)
from ai_accounting.backup_integration import PostgresEndpoint
from ai_accounting.config import Settings
from ai_accounting.windows_backup import WindowsCurrentUserOnlyAclVerifier


class InMemoryPasswordStore:
    def __init__(self, value: str | None = "backup-password") -> None:
        self.value = value

    def save_password(self, password: SecretStr) -> None:
        self.value = password.get_secret_value()

    def load_password(self) -> SecretStr | None:
        return SecretStr(self.value) if self.value is not None else None

    def delete_password(self) -> None:
        self.value = None


def _endpoint() -> PostgresEndpoint:
    return PostgresEndpoint(
        host="127.0.0.1",
        port=5432,
        database="finance",
        username="finance_backup",
        application_name="finance-backup-test",
    )


def _production_settings(tmp_path: Path) -> Settings:
    return Settings(
        finance_environment="production",
        database_url=(
            "postgresql+psycopg://runtime:test-runtime-only@127.0.0.1:5432/finance"
        ),
        finance_migration_database_url=(
            "postgresql+psycopg://migration:test-migration-only@127.0.0.1:5432/finance"
        ),
        finance_storage_dir=tmp_path,
        finance_service_lock_file=tmp_path / "service.lock",
        finance_evidence_dir=tmp_path / "evidence",
        finance_evidence_import_dir=tmp_path / "evidence-import",
        finance_bank_import_dir=tmp_path / "bank-import",
    )


@pytest.mark.skipif(sys.platform != "win32", reason="protected ACL is Windows-only")
def test_pgpass_lease_has_current_user_only_acl_and_is_removed_after_use(
    tmp_path: Path,
) -> None:
    verifier = WindowsCurrentUserOnlyAclVerifier()
    provider = WindowsProtectedPgPassProvider(
        InMemoryPasswordStore("colon:slash\\password"),
        tmp_path,
        verifier,
    )

    with provider.lease_pgpass(_endpoint()) as pgpass:
        directory = pgpass.parent
        assert pgpass.read_text(encoding="utf-8") == (
            "127.0.0.1:5432:finance:finance_backup:colon\\:slash\\\\password\n"
        )
        verifier.assert_current_windows_user_only(pgpass)
        verifier.assert_current_windows_user_only(directory)
    assert not pgpass.exists()
    assert not directory.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="protected ACL is Windows-only")
def test_verified_archive_copy_is_acl_protected_and_pinned_until_release(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "verified.complete"
    lease_root = tmp_path / "local"
    source_root.mkdir()
    lease_root.mkdir()
    source = source_root / "database.dump"
    content = b"PGDMP\x00verified-archive"
    source.write_bytes(content)
    verifier = WindowsCurrentUserOnlyAclVerifier()
    provider = WindowsProtectedArchiveCopyProvider(lease_root, verifier)

    with provider.lease_verified_archive(
        source,
        source_root,
        expected_sha256=hashlib.sha256(content).hexdigest(),
        expected_size_bytes=len(content),
    ) as copied:
        copied_directory = copied.parent
        assert copied != source
        assert copied.read_bytes() == content
        verifier.assert_current_windows_user_only(copied)
        verifier.assert_current_windows_user_only(copied_directory)
        replacement = lease_root / "replacement.dump"
        replacement.write_bytes(content)
        with pytest.raises(OSError):
            os.replace(replacement, copied)
    assert not copied.exists()
    assert not copied_directory.exists()


@pytest.mark.skipif(sys.platform != "win32", reason="protected ACL is Windows-only")
def test_verified_archive_copy_rejects_verify_to_open_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_root = tmp_path / "verified.complete"
    lease_root = tmp_path / "local"
    source_root.mkdir()
    lease_root.mkdir()
    source = source_root / "database.dump"
    original = b"PGDMP\x00original"
    source.write_bytes(original)
    validated = backup_credentials._validated_archive_source

    def replace_after_validation(
        selected_source: Path, selected_root: Path
    ) -> tuple[Path, os.stat_result]:
        path, source_stat = validated(selected_source, selected_root)
        replacement = source_root / "replacement.dump"
        replacement.write_bytes(b"PGDMP\x00replacement")
        os.replace(replacement, source)
        return path, source_stat

    monkeypatch.setattr(
        backup_credentials, "_validated_archive_source", replace_after_validation
    )
    provider = WindowsProtectedArchiveCopyProvider(
        lease_root, WindowsCurrentUserOnlyAclVerifier()
    )

    with pytest.raises(BackupCredentialError, match="BACKUP_ARCHIVE_SOURCE_CHANGED"):
        with provider.lease_verified_archive(
            source,
            source_root,
            expected_sha256=hashlib.sha256(original).hexdigest(),
            expected_size_bytes=len(original),
        ):
            pytest.fail("replacement must not yield a restore archive")


@pytest.mark.skipif(sys.platform != "win32", reason="Credential Manager is Windows-only")
def test_backup_credential_store_round_trip_uses_an_isolated_target() -> None:
    target = f"ai-accounting-core/tests/finance-backup/{uuid.uuid4()}"
    store = WindowsFinanceBackupCredentialStore(target=target)
    try:
        store.save_password(SecretStr("test-only-credential"))
        loaded = store.load_password()
        assert loaded is not None
        assert loaded.get_secret_value() == "test-only-credential"
    finally:
        store.delete_password()
    assert store.load_password() is None


def test_credential_layout_is_explicit_and_fails_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backup_credentials._assert_backup_credential_layout()
    original_sizeof = backup_credentials.ctypes.sizeof

    def invalid_size(value: object) -> int:
        if value is backup_credentials._CredentialW:
            return 1
        return original_sizeof(value)

    monkeypatch.setattr(backup_credentials.ctypes, "sizeof", invalid_size)
    with pytest.raises(BackupCredentialError, match="BACKUP_CREDENTIAL_STORE_UNAVAILABLE"):
        backup_credentials._assert_backup_credential_layout()


def test_credential_set_uses_no_echo_and_never_prints_password(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    store = InMemoryPasswordStore(None)
    prompts = iter(("private-backup-password", "private-backup-password"))
    monkeypatch.setattr(backup_cli.getpass, "getpass", lambda prompt: next(prompts))
    monkeypatch.setattr(backup_cli, "WindowsFinanceBackupCredentialStore", lambda: store)

    backup_cli.main(("credential-set",))

    captured = capsys.readouterr()
    assert captured.out == "BACKUP_CREDENTIAL_STORED\n"
    assert captured.err == ""
    assert "private-backup-password" not in captured.out + captured.err
    assert store.value == "private-backup-password"


def test_cli_argument_failure_is_one_stable_code_without_traceback(
    capsys: pytest.CaptureFixture[str],
) -> None:
    with pytest.raises(SystemExit) as exited:
        backup_cli.main(("create", "--host", "127.0.0.1"))
    assert exited.value.code == 1
    captured = capsys.readouterr()
    assert captured.err == "BACKUP_CLI_ARGUMENT_INVALID\n"
    assert "Traceback" not in captured.err


def test_create_cli_reaches_removable_media_preflight_after_validated_settings(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    media = tmp_path / "media"
    pg_bin = tmp_path / "pg-bin"
    called: list[str] = []
    credential_store = InMemoryPasswordStore("not-read-before-preflight")

    def unexpected(name: str):  # type: ignore[no-untyped-def]
        def fail(*args: object, **kwargs: object) -> None:
            del args, kwargs
            called.append(name)
            raise AssertionError(name)

        return fail

    monkeypatch.setattr(
        backup_cli,
        "get_settings",
        lambda: _production_settings(tmp_path),
    )
    monkeypatch.setattr(
        backup_cli,
        "WindowsFinanceBackupCredentialStore",
        lambda: credential_store,
    )
    monkeypatch.setattr(backup_cli, "WindowsVolumeInspector", object)
    monkeypatch.setattr(backup_cli, "WindowsWriteThroughPublisher", object)

    def reject_unencrypted_media(*args: object) -> None:
        del args
        called.append("preflight")
        raise backup_cli.BackupIntegrationError("BACKUP_VOLUME_NOT_ENCRYPTED")

    monkeypatch.setattr(backup_cli, "preflight_windows_backup_root", reject_unencrypted_media)
    monkeypatch.setattr(
        backup_cli, "create_integrated_stopped_backup", unexpected("integrated-backup")
    )

    with pytest.raises(SystemExit) as exited:
        backup_cli.main(
            (
                "create",
                "--backup-root",
                str(media),
                "--purpose",
                "daily",
                "--backup-id",
                "cli-backup",
                "--pg-bin-dir",
                str(pg_bin),
            )
        )

    output = capsys.readouterr()
    assert exited.value.code == 1
    assert output.out == ""
    assert output.err == "BACKUP_VOLUME_NOT_ENCRYPTED\n"
    assert called == ["preflight"]
    assert not media.exists()
    assert not pg_bin.exists()


def test_create_cli_rejects_nonproduction_before_windows_or_credential_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: list[str] = []

    def unexpected(name: str):  # type: ignore[no-untyped-def]
        def fail(*args: object, **kwargs: object) -> None:
            del args, kwargs
            called.append(name)
            raise AssertionError(name)

        return fail

    monkeypatch.setattr(
        backup_cli,
        "get_settings",
        lambda: SimpleNamespace(finance_environment="development"),
    )
    monkeypatch.setattr(
        backup_cli, "WindowsFinanceBackupCredentialStore", unexpected("credential")
    )
    monkeypatch.setattr(
        backup_cli, "preflight_windows_backup_root", unexpected("preflight")
    )

    with pytest.raises(SystemExit) as exited:
        backup_cli.main(
            (
                "create",
                "--backup-root",
                str(tmp_path / "media"),
                "--purpose",
                "daily",
                "--pg-bin-dir",
                str(tmp_path / "pg-bin"),
            )
        )

    output = capsys.readouterr()
    assert exited.value.code == 1
    assert output.out == ""
    assert output.err == "BACKUP_PRODUCTION_ENVIRONMENT_REQUIRED\n"
    assert called == []


class _BackupLeaseContext(AbstractContextManager[None]):
    def __init__(self, called: list[str]) -> None:
        self._called = called

    def __enter__(self) -> None:
        self._called.append("lease")

    def __exit__(self, *args: object) -> None:
        del args


class _BackupLease:
    def __init__(self, called: list[str]) -> None:
        self._called = called

    def acquire_backup_lease(self) -> _BackupLeaseContext:
        return _BackupLeaseContext(self._called)


def test_create_cli_missing_dedicated_credential_fails_after_preflight_and_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    called: list[str] = []
    credential_store = InMemoryPasswordStore(None)
    lease = _BackupLease(called)

    monkeypatch.setattr(backup_cli, "get_settings", lambda: _production_settings(tmp_path))
    monkeypatch.setattr(
        backup_cli, "WindowsFinanceBackupCredentialStore", lambda: credential_store
    )
    monkeypatch.setattr(backup_cli, "WindowsVolumeInspector", object)
    monkeypatch.setattr(backup_cli, "WindowsWriteThroughPublisher", object)
    monkeypatch.setattr(
        backup_cli,
        "preflight_windows_backup_root",
        lambda *args: called.append("preflight"),
    )
    monkeypatch.setattr(backup_cli, "WindowsBackupServiceLease", lambda *args: lease)

    with pytest.raises(SystemExit) as exited:
        backup_cli.main(
            (
                "create",
                "--backup-root",
                str(tmp_path / "media"),
                "--purpose",
                "daily",
                "--pg-bin-dir",
                str(tmp_path / "pg-bin"),
            )
        )

    output = capsys.readouterr()
    assert exited.value.code == 1
    assert output.out == ""
    assert output.err == "BACKUP_CREDENTIAL_REQUIRED\n"
    assert called == ["preflight", "lease"]


def test_cli_rejects_nonloopback_runtime_database(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(
        backup_cli,
        "get_settings",
        lambda: SimpleNamespace(
            finance_environment="production",
            finance_storage_dir=tmp_path,
            finance_evidence_dir=tmp_path / "evidence",
            finance_service_lock_file=tmp_path / "service.lock",
            database_url="postgresql+psycopg://runtime:secret@db.example/finance",
        ),
    )
    with pytest.raises(backup_cli.BackupCliError, match="BACKUP_RUNTIME_DATABASE_NOT_LOOPBACK"):
        backup_cli._load_production_settings()


def test_cli_does_not_accept_a_caller_supplied_runtime_role() -> None:
    with pytest.raises(backup_cli.BackupCliError, match="BACKUP_CLI_ARGUMENT_INVALID"):
        backup_cli._parser().parse_args(
            (
                "create",
                "--backup-root",
                "C:/media",
                "--purpose",
                "daily",
                "--pg-bin-dir",
                "C:/postgres/bin",
                "--runtime-role",
                "nonexistent_role",
            )
        )


def test_pgpass_requires_credential_before_creating_any_path(tmp_path: Path) -> None:
    provider = WindowsProtectedPgPassProvider(
        InMemoryPasswordStore(None),
        tmp_path,
        SimpleNamespace(assert_current_windows_user_only=lambda path: None),
    )
    with pytest.raises(BackupCredentialError, match="BACKUP_CREDENTIAL_REQUIRED"):
        with provider.lease_pgpass(_endpoint()):
            pytest.fail("missing credential must not yield")
    assert tuple(tmp_path.iterdir()) == ()
