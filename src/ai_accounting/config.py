from __future__ import annotations

import ipaddress
import os
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError

DEVELOPMENT_DATABASE_URL = "postgresql+psycopg://finance:finance@localhost:5432/finance"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    finance_environment: Literal["development", "production"] = "development"
    database_url: str = DEVELOPMENT_DATABASE_URL
    finance_migration_database_url: str | None = None
    finance_storage_dir: Path = Path("data")
    finance_service_lock_file: Path = Path("data/service.lock")
    finance_evidence_dir: Path = Path("data/evidence")
    finance_evidence_import_dir: Path | None = None
    finance_bank_import_dir: Path | None = None
    finance_max_evidence_bytes: int = Field(default=20 * 1024 * 1024, gt=0)
    finance_max_bank_import_bytes: int = Field(default=20 * 1024 * 1024, gt=0)

    def __init__(self, **values: Any) -> None:
        super().__init__(**values)
        self._validate_production_controls()

    def _validate_production_controls(self) -> None:
        if self.finance_environment != "production":
            return
        if self.finance_migration_database_url is None:
            raise SettingsConfigurationError(
                "PRODUCTION_MIGRATION_DATABASE_URL_REQUIRED"
            )
        _validate_production_database_urls(
            self.database_url,
            self.finance_migration_database_url,
        )
        required_roots = {
            "FINANCE_STORAGE_DIR": self.finance_storage_dir,
            "FINANCE_SERVICE_LOCK_FILE": self.finance_service_lock_file,
            "FINANCE_EVIDENCE_DIR": self.finance_evidence_dir,
            "FINANCE_EVIDENCE_IMPORT_DIR": self.finance_evidence_import_dir,
            "FINANCE_BANK_IMPORT_DIR": self.finance_bank_import_dir,
        }
        normalized_roots: dict[str, Path] = {}
        for name, root in required_roots.items():
            if root is None or not root.is_absolute():
                raise SettingsConfigurationError(
                    f"PRODUCTION_{name}_ABSOLUTE_PATH_REQUIRED"
                )
            normalized_roots[name] = Path(os.path.abspath(os.fspath(root)))
        self.finance_storage_dir = normalized_roots["FINANCE_STORAGE_DIR"]
        self.finance_service_lock_file = normalized_roots["FINANCE_SERVICE_LOCK_FILE"]
        self.finance_evidence_dir = normalized_roots["FINANCE_EVIDENCE_DIR"]
        self.finance_evidence_import_dir = normalized_roots[
            "FINANCE_EVIDENCE_IMPORT_DIR"
        ]
        self.finance_bank_import_dir = normalized_roots["FINANCE_BANK_IMPORT_DIR"]
        try:
            self.finance_evidence_dir.relative_to(self.finance_storage_dir)
        except ValueError as exc:
            raise SettingsConfigurationError(
                "PRODUCTION_EVIDENCE_DIR_OUTSIDE_STORAGE_ROOT"
            ) from exc
        try:
            self.finance_service_lock_file.relative_to(self.finance_storage_dir)
        except ValueError as exc:
            raise SettingsConfigurationError(
                "PRODUCTION_SERVICE_LOCK_FILE_OUTSIDE_STORAGE_ROOT"
            ) from exc

    def runtime_database_url(self) -> str:
        """URL used by the application process."""
        return self.database_url

    def migration_database_url(self, configured_url: str | None = None) -> str:
        """URL used by Alembic, preserving explicit development test configuration."""
        if self.finance_migration_database_url is not None:
            return self.finance_migration_database_url
        if self.finance_environment == "production":
            raise ValueError("production FINANCE_MIGRATION_DATABASE_URL is required")
        return configured_url or self.database_url


class SettingsConfigurationError(ValueError):
    """Caller-safe production configuration failure without echoing secrets."""


def _validate_production_database_urls(runtime_url: str, migration_url: str) -> None:
    try:
        runtime = make_url(runtime_url)
        migration = make_url(migration_url)
    except ArgumentError as exc:
        raise SettingsConfigurationError("PRODUCTION_DATABASE_URL_INVALID") from exc
    for name, parsed in (("DATABASE_URL", runtime), ("FINANCE_MIGRATION_DATABASE_URL", migration)):
        if parsed.get_backend_name() != "postgresql":
            raise SettingsConfigurationError(f"PRODUCTION_{name}_POSTGRESQL_REQUIRED")
        if not parsed.username or not parsed.password:
            raise SettingsConfigurationError(
                f"PRODUCTION_{name}_ACCOUNT_AND_SECRET_REQUIRED"
            )
        if parsed.username == "finance" and parsed.password == "finance":
            raise SettingsConfigurationError(
                f"PRODUCTION_{name}_DEVELOPMENT_CREDENTIALS_FORBIDDEN"
            )
        if not _is_loopback_database_host(parsed.host):
            raise SettingsConfigurationError(
                f"PRODUCTION_{name}_LOCAL_DATABASE_REQUIRED"
            )
    if runtime.username == migration.username:
        raise SettingsConfigurationError("PRODUCTION_DATABASE_ACCOUNTS_MUST_DIFFER")
    runtime_target = (runtime.host, runtime.port, runtime.database)
    migration_target = (migration.host, migration.port, migration.database)
    if runtime_target != migration_target:
        raise SettingsConfigurationError("PRODUCTION_DATABASE_TARGETS_MUST_MATCH")


def _is_loopback_database_host(host: str | None) -> bool:
    if host is None:
        return False
    normalized = host.casefold().rstrip(".")
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


@lru_cache
def get_settings() -> Settings:
    return Settings()
