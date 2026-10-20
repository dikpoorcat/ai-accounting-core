"""Storage configuration and immutable structure identities."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._session import RepositorySession


@dataclass(frozen=True)
class SchemaObject:
    type: str
    name: str
    table: str
    sql: str


@dataclass(frozen=True)
class ModuleSchema:
    owner: str
    version: int
    objects: tuple[SchemaObject, ...]
    sql_fingerprint: str
    contract_digest: str
    parent_digest: str | None = None

    @property
    def tables(self) -> frozenset[str]:
        return frozenset(item.name for item in self.objects if item.type == "table")


@dataclass(frozen=True)
class RepositoryFactory:
    schema: ModuleSchema
    build: Callable[[RepositorySession], Any]
    maintenance_build: Callable[[RepositorySession], Any] | None = None


@dataclass(frozen=True)
class OwnerProfile:
    owner_id: str
    display_name: str
    password_hash: str


@dataclass(frozen=True)
class CompanySpec:
    company_id: str
    database_id: str
    credit_code: str
    legal_name: str
    start_period: str
    company_root: Path | str
    backup_directory: Path | str | None = None


@dataclass(frozen=True)
class CompanyLocation:
    company_id: str
    database_id: str
    credit_code: str
    legal_name: str
    company_root: Path
    database_path: Path
    backup_directory: Path


@dataclass(frozen=True)
class CompanyMetadata:
    company_id: str
    database_id: str
    credit_code: str
    legal_name: str
    start_period: str
    company_root: Path
    database_path: Path
    backup_directory: Path
    accounting_standard: str
    currency: str
    profile_seq: int = 1


@dataclass(frozen=True)
class DatabaseSnapshot:
    path: Path
    company_id: str
    database_id: str
    head: int
    structure_fingerprint: str
