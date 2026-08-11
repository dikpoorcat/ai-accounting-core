from __future__ import annotations

import hashlib
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import Session

from ai_accounting import path_security
from ai_accounting.config import Settings, SettingsConfigurationError
from ai_accounting.evidence import register_evidence
from ai_accounting.models import Organization
from ai_accounting.path_security import (
    PathSecurityError,
    read_regular_file_in_root,
    resolve_regular_file_in_root,
)
from ai_accounting.schemas import RegisterEvidenceRequest


def test_supply_chain_controls_pin_dependencies_actions_and_vulnerability_gate() -> None:
    repository_root = Path(__file__).parents[1]
    lock_file = repository_root / "uv.lock"
    pyproject = (repository_root / "pyproject.toml").read_text(encoding="utf-8")
    workflow = (repository_root / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    dependabot = (repository_root / ".github" / "dependabot.yml").read_text(encoding="utf-8")
    compose = (repository_root / "docker-compose.yml").read_text(encoding="utf-8")

    assert lock_file.is_file()
    assert "[tool.uv]" in pyproject
    assert 'required-version = "==0.12.3"' in pyproject
    assert "uv sync --locked --all-extras" in workflow
    assert "continue-on-error" not in workflow
    assert "uv run pip-audit --format json --output pip-audit.json" in workflow
    assert "package-ecosystem: pip" in dependabot
    assert "package-ecosystem: github-actions" in dependabot

    action_references = re.findall(r"^\s*- uses: [^@\s]+@([^\s#]+) # v\S+$", workflow, re.MULTILINE)
    assert action_references
    assert all(re.fullmatch(r"[0-9a-f]{40}", reference) for reference in action_references)

    postgres_image = (
        "postgres:17-alpine@sha256:"
        "742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"  # noqa: E501
    )
    assert f"image: {postgres_image}" in workflow
    assert f"image: {postgres_image}" in compose
    unpinned_postgres = re.compile(r"postgres:17-alpine(?!@sha256:[0-9a-f]{64})")
    for test_file in (repository_root / "tests").glob("*.py"):
        if test_file.resolve() == Path(__file__).resolve():
            continue
        assert not unpinned_postgres.search(test_file.read_text(encoding="utf-8")), test_file


def _production_settings(tmp_path: Path, **overrides: object) -> Settings:
    values: dict[str, object] = {
        "finance_environment": "production",
        "database_url": "postgresql+psycopg://runtime:secret@localhost/finance",
        "finance_migration_database_url": "postgresql+psycopg://migrator:secret@localhost/finance",
        "finance_storage_dir": tmp_path / "storage",
        "finance_service_lock_file": tmp_path / "storage" / "service.lock",
        "finance_evidence_dir": tmp_path / "storage" / "evidence",
        "finance_evidence_import_dir": tmp_path / "incoming" / "evidence",
        "finance_bank_import_dir": tmp_path / "incoming" / "bank",
    }
    values.update(overrides)
    return Settings(**values)


def test_production_settings_reject_default_database_url_and_missing_migration_url(
    tmp_path: Path,
) -> None:
    with pytest.raises(SettingsConfigurationError):
        _production_settings(
            tmp_path,
            database_url="postgresql+psycopg://finance:finance@localhost:5432/finance",
            finance_migration_database_url=None,
        )


def test_production_settings_require_distinct_urls_and_absolute_roots(tmp_path: Path) -> None:
    settings = _production_settings(tmp_path)

    assert settings.runtime_database_url() == settings.database_url
    assert settings.migration_database_url() == settings.finance_migration_database_url

    with pytest.raises(SettingsConfigurationError):
        _production_settings(
            tmp_path,
            finance_migration_database_url="postgresql+psycopg://runtime:secret@localhost/finance",
        )
    with pytest.raises(SettingsConfigurationError):
        _production_settings(tmp_path, finance_bank_import_dir=Path("data/incoming/bank"))
    with pytest.raises(SettingsConfigurationError):
        _production_settings(
            tmp_path,
            database_url="sqlite:///runtime.db",
        )
    with pytest.raises(SettingsConfigurationError):
        _production_settings(
            tmp_path,
            finance_migration_database_url=(
                "postgresql+psycopg://runtime:other@db.example/finance"
            ),
        )
    with pytest.raises(SettingsConfigurationError):
        _production_settings(
            tmp_path,
            finance_migration_database_url=(
                "postgresql+psycopg://migrator:secret@other.example/finance"
            ),
        )
    with pytest.raises(
        SettingsConfigurationError,
        match="PRODUCTION_DATABASE_URL_LOCAL_DATABASE_REQUIRED",
    ):
        _production_settings(
            tmp_path,
            database_url="postgresql+psycopg://runtime:secret@db.example/finance",
            finance_migration_database_url=(
                "postgresql+psycopg://migrator:secret@db.example/finance"
            ),
        )


def test_production_settings_normalize_paths_before_containment(tmp_path: Path) -> None:
    with pytest.raises(
        SettingsConfigurationError,
        match="PRODUCTION_EVIDENCE_DIR_OUTSIDE_STORAGE_ROOT",
    ):
        _production_settings(
            tmp_path,
            finance_storage_dir=tmp_path / "allowed" / "child" / "..",
            finance_evidence_dir=(
                tmp_path / "allowed" / "child" / ".." / ".." / "outside"
            ),
        )


def test_production_configuration_errors_do_not_echo_database_secrets(
    tmp_path: Path,
) -> None:
    with pytest.raises(SettingsConfigurationError) as error:
        _production_settings(
            tmp_path,
            database_url=(
                "postgresql+psycopg://runtime:TOPSECRET@db.example/finance"
            ),
            finance_migration_database_url=None,
        )

    assert "TOPSECRET" not in str(error.value)


def test_regular_file_must_stay_under_its_allowlisted_root(tmp_path: Path) -> None:
    import_root = tmp_path / "incoming"
    import_root.mkdir()
    permitted = import_root / "statement.csv"
    permitted.write_text("ok", encoding="utf-8")
    outside = tmp_path / "outside.csv"
    outside.write_text("not allowed", encoding="utf-8")

    assert resolve_regular_file_in_root(permitted, import_root, max_bytes=10) == permitted.resolve()
    assert read_regular_file_in_root(permitted, import_root, max_bytes=10) == (
        permitted.resolve(),
        b"ok",
    )
    with pytest.raises(PathSecurityError, match="FILE_PATH_NOT_ALLOWED"):
        resolve_regular_file_in_root(outside, import_root, max_bytes=100)


def test_regular_file_rejects_symlink_and_oversized_input(tmp_path: Path) -> None:
    import_root = tmp_path / "incoming"
    import_root.mkdir()
    oversized = import_root / "oversized.csv"
    oversized.write_bytes(b"1234")
    with pytest.raises(PathSecurityError, match="FILE_TOO_LARGE"):
        resolve_regular_file_in_root(oversized, import_root, max_bytes=3)

    target = tmp_path / "outside.csv"
    target.write_text("not allowed", encoding="utf-8")
    link = import_root / "statement.csv"
    try:
        link.symlink_to(target)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")

    with pytest.raises(PathSecurityError, match="FILE_REPARSE_POINT_NOT_ALLOWED"):
        resolve_regular_file_in_root(link, import_root, max_bytes=100)


def test_regular_file_read_rejects_parent_replacement_after_resolution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import_root = tmp_path / "incoming"
    statement_dir = import_root / "statements"
    statement_dir.mkdir(parents=True)
    statement = statement_dir / "statement.csv"
    statement.write_bytes(b"INSIDE")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / statement.name).write_bytes(b"OUTSIDE_SECRET")
    probe = import_root / "symlink-probe"
    try:
        probe.symlink_to(outside, target_is_directory=True)
        probe.unlink()
    except OSError as exc:
        pytest.skip(f"directory symlink creation is unavailable: {exc}")

    original_open = path_security.os.open
    swapped = False

    def swap_parent_then_open(path: object, flags: int, *args: object) -> int:
        nonlocal swapped
        if not swapped:
            swapped = True
            statement_dir.rename(import_root / "statements-original")
            statement_dir.symlink_to(outside, target_is_directory=True)
        return original_open(path, flags, *args)

    monkeypatch.setattr(path_security.os, "open", swap_parent_then_open)

    with pytest.raises(PathSecurityError):
        read_regular_file_in_root(statement, import_root, max_bytes=100)


def test_evidence_file_import_is_constrained_to_its_configured_root(
    session: Session, organization: Organization, tmp_path: Path
) -> None:
    import_root = tmp_path / "incoming"
    import_root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("not allowed", encoding="utf-8")
    settings = Settings(
        finance_evidence_dir=tmp_path / "storage" / "evidence",
        finance_evidence_import_dir=import_root,
    )
    request = RegisterEvidenceRequest(
        org_id=organization.id,
        source="pilot-test",
        file_path=outside,
        media_type="text/plain",
    )

    with pytest.raises(PathSecurityError, match="FILE_PATH_NOT_ALLOWED"):
        register_evidence(session, request, settings)


def test_evidence_storage_rejects_a_symlinked_directory(
    session: Session, organization: Organization, tmp_path: Path
) -> None:
    storage_root = tmp_path / "storage"
    storage_root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    evidence_dir = storage_root / "evidence"
    try:
        evidence_dir.symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    settings = Settings(finance_evidence_dir=evidence_dir)
    request = RegisterEvidenceRequest(
        org_id=organization.id,
        source="pilot-test",
        content_base64="c2FmZQ==",
        media_type="text/plain",
    )

    with pytest.raises(PathSecurityError, match="FILE_REPARSE_POINT_NOT_ALLOWED"):
        register_evidence(session, request, settings)


def test_evidence_storage_rejects_a_symlinked_content_address_directory(
    session: Session, organization: Organization, tmp_path: Path
) -> None:
    evidence_dir = tmp_path / "storage" / "evidence"
    evidence_dir.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    digest = hashlib.sha256(b"safe").hexdigest()
    try:
        (evidence_dir / digest[:2]).symlink_to(outside, target_is_directory=True)
    except OSError as exc:
        pytest.skip(f"symlink creation is unavailable: {exc}")
    settings = Settings(finance_evidence_dir=evidence_dir)
    request = RegisterEvidenceRequest(
        org_id=organization.id,
        source="pilot-test",
        content_base64="c2FmZQ==",
        media_type="text/plain",
    )

    with pytest.raises(PathSecurityError, match="FILE_REPARSE_POINT_NOT_ALLOWED"):
        register_evidence(session, request, settings)


def test_evidence_deduplication_rejects_tampered_stored_content(
    session: Session,
    organization: Organization,
    tmp_path: Path,
) -> None:
    settings = Settings(finance_evidence_dir=tmp_path / "storage" / "evidence")
    request = RegisterEvidenceRequest(
        org_id=organization.id,
        source="pilot-test",
        content_base64="c2FmZQ==",
        media_type="text/plain",
    )
    evidence = register_evidence(session, request, settings)
    Path(evidence.storage_path).write_bytes(b"tampered")

    with pytest.raises(PathSecurityError, match="EVIDENCE_CONTENT_ADDRESS_MISMATCH"):
        register_evidence(session, request, settings)


def test_production_mcp_import_does_not_load_or_echo_credentials(tmp_path: Path) -> None:
    storage = tmp_path / "storage"
    environment = os.environ.copy()
    environment.update(
        {
            "FINANCE_ENVIRONMENT": "production",
            "DATABASE_URL": (
                "postgresql+psycopg://runtime:runtime-secret@localhost/finance"
            ),
            "FINANCE_MIGRATION_DATABASE_URL": (
                "postgresql+psycopg://migrator:migration-secret@localhost/finance"
            ),
            "FINANCE_STORAGE_DIR": str(storage),
            "FINANCE_SERVICE_LOCK_FILE": str(storage / "service.lock"),
            "FINANCE_EVIDENCE_DIR": str(storage / "evidence"),
            "FINANCE_EVIDENCE_IMPORT_DIR": str(tmp_path / "incoming" / "evidence"),
            "FINANCE_BANK_IMPORT_DIR": str(tmp_path / "incoming" / "bank"),
        }
    )
    completed = subprocess.run(
        [sys.executable, "-c", "import ai_accounting.mcp_server"],
        cwd=Path(__file__).parents[1],
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0, completed.stderr
    assert "runtime-secret" not in completed.stderr
    assert "migration-secret" not in completed.stderr
