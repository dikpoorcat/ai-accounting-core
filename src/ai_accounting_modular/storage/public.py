"""Public factories for exact-schema, single-company SQLite storage."""

from ._database import restore_database_copy, verify_database
from ._engine import Storage, create_catalog
from ._schema import module_schema
from ._session import RepositorySession
from ._types import (
    CompanyLocation,
    CompanyMetadata,
    CompanySpec,
    DatabaseSnapshot,
    ModuleSchema,
    OwnerProfile,
    RepositoryFactory,
    SchemaObject,
)

__all__ = [
    "CompanyLocation",
    "CompanyMetadata",
    "CompanySpec",
    "DatabaseSnapshot",
    "ModuleSchema",
    "OwnerProfile",
    "RepositoryFactory",
    "RepositorySession",
    "SchemaObject",
    "Storage",
    "create_catalog",
    "module_schema",
    "restore_database_copy",
    "verify_database",
]
