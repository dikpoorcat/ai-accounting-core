"""Public factories for exact-schema, single-company SQLite storage."""

from ._company import company_metadata, create_company_database
from ._database import restore_database_copy, verify_database
from ._directory import publish_company_directory
from ._engine import Storage, create_catalog
from ._schema import company_structure_contract, module_schema
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
from ._units import open_database_unit

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
    "company_metadata",
    "company_structure_contract",
    "create_catalog",
    "create_company_database",
    "module_schema",
    "open_database_unit",
    "publish_company_directory",
    "restore_database_copy",
    "verify_database",
]
