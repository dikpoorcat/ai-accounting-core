"""Catalog-backed routing for physically isolated company databases."""

from __future__ import annotations

import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Connection, Engine, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session, sessionmaker

from .config import Settings, get_settings
from .database import SessionLocal, make_engine, make_session_factory
from .models import CatalogMetadata, CompanyRegistry, OrganizationDatabaseMetadata

_DATABASE_NAME = re.compile(r"(?:finance|finance_company_[0-9a-f]{32})\Z")


class CompanyRoutingError(ValueError):
    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class CompanyDatabaseRouter:
    """Resolve a catalog row to a cached engine without storing connection URLs."""

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._engines: dict[uuid.UUID, tuple[uuid.UUID, Engine]] = {}
        self._lock = threading.RLock()

    @property
    def enabled(self) -> bool:
        return self.settings.multi_company_enabled

    @staticmethod
    def validate_database_name(value: str) -> str:
        if not _DATABASE_NAME.fullmatch(value):
            raise CompanyRoutingError("COMPANY_DATABASE_NAME_INVALID")
        return value

    def company_url(self, database_name: str, *, migration: bool = False) -> URL:
        database_name = self.validate_database_name(database_name)
        raw = (
            self.settings.finance_migration_database_url
            if migration
            else self.settings.finance_company_database_url
        )
        if raw is None:
            raise CompanyRoutingError("COMPANY_DATABASE_ROUTING_NOT_CONFIGURED")
        parsed = make_url(raw)
        if parsed.get_backend_name() != "postgresql":
            raise CompanyRoutingError("COMPANY_DATABASE_POSTGRESQL_REQUIRED")
        return parsed.set(database=database_name)

    def resolve(
        self,
        catalog_session: Session,
        org_id: uuid.UUID,
        *,
        for_write: bool,
    ) -> CompanyRegistry:
        query = select(CompanyRegistry).where(CompanyRegistry.org_id == org_id)
        if for_write:
            query = query.with_for_update()
        registry = catalog_session.scalar(query)
        if registry is None:
            raise CompanyRoutingError("ORGANIZATION_NOT_FOUND")
        if for_write and registry.status != "active":
            raise CompanyRoutingError("COMPANY_NOT_ACTIVE")
        if not for_write and registry.status not in {"active", "archived"}:
            raise CompanyRoutingError("COMPANY_NOT_READABLE")
        self.validate_database_name(registry.database_name)
        return registry

    def engine_for(self, registry: CompanyRegistry) -> Engine:
        with self._lock:
            cached = self._engines.get(registry.org_id)
            if cached is not None and cached[0] == registry.database_identity:
                return cached[1]
            if cached is not None:
                cached[1].dispose()
            database_url = self.company_url(registry.database_name).render_as_string(
                hide_password=False
            )
            engine = make_engine(database_url)
            if self.settings.finance_environment == "production":
                with engine.connect() as connection:
                    assert_runtime_role(connection)
            self._verify_database_binding(engine, registry)
            self._engines[registry.org_id] = (registry.database_identity, engine)
            return engine

    def factory_for(self, registry: CompanyRegistry) -> sessionmaker[Session]:
        return make_session_factory(self.engine_for(registry))

    @staticmethod
    def _verify_database_binding(engine: Engine, registry: CompanyRegistry) -> None:
        with Session(engine) as session:
            metadata = session.get(OrganizationDatabaseMetadata, 1)
            if (
                metadata is None
                or metadata.org_id != registry.org_id
                or metadata.database_identity != registry.database_identity
            ):
                raise CompanyRoutingError("COMPANY_DATABASE_IDENTITY_MISMATCH")

    def dispose(self) -> None:
        with self._lock:
            for _, engine in self._engines.values():
                engine.dispose()
            self._engines.clear()


def catalog_instance_id(session: Session) -> uuid.UUID:
    metadata = session.get(CatalogMetadata, 1)
    if metadata is None:
        raise CompanyRoutingError("CATALOG_NOT_INITIALIZED")
    return metadata.catalog_instance_id


@contextmanager
def catalog_transaction() -> Iterator[Session]:
    """Use the configured catalog, or the legacy single database during migration."""

    with SessionLocal.begin() as session:
        if get_settings().multi_company_enabled:
            session.info["catalog_mode"] = True
        yield session


router = CompanyDatabaseRouter()


def _role_flags(
    connection: Connection, role_name: str | None = None
) -> tuple[bool, bool, bool, bool, bool]:
    row = connection.execute(
        text(
            "SELECT rolsuper, rolcreatedb, rolcreaterole, rolreplication, rolbypassrls "
            "FROM pg_catalog.pg_roles "
            "WHERE rolname = COALESCE(:role_name, current_user)"
        ),
        {"role_name": role_name},
    ).one_or_none()
    if row is None:
        raise CompanyRoutingError("DATABASE_ROLE_PRIVILEGES_UNAVAILABLE")
    return tuple(bool(item) for item in row)  # type: ignore[return-value]


def assert_runtime_role(connection: Connection) -> None:
    """Reject an application role that can create databases, roles, or bypass controls."""

    flags = _role_flags(connection)
    if any(flags):
        raise CompanyRoutingError("COMPANY_RUNTIME_ROLE_PRIVILEGES_INVALID")


def assert_provisioning_role(connection: Connection) -> None:
    """Require narrowly scoped database creation without superuser/role creation."""

    is_superuser, can_create_database, can_create_role, can_replicate, bypasses_rls = (
        _role_flags(connection)
    )
    if (
        is_superuser
        or not can_create_database
        or can_create_role
        or can_replicate
        or bypasses_rls
    ):
        raise CompanyRoutingError("COMPANY_PROVISIONING_ROLE_PRIVILEGES_INVALID")


def grant_runtime_database_access(connection: Connection, runtime_role: str) -> None:
    """Grant only application DML privileges on the current migrated database."""

    if any(_role_flags(connection, runtime_role)):
        raise CompanyRoutingError("COMPANY_RUNTIME_ROLE_PRIVILEGES_INVALID")
    preparer = connection.dialect.identifier_preparer
    quoted_role = preparer.quote_identifier(runtime_role)
    database_name = connection.scalar(text("SELECT current_database()"))
    if not isinstance(database_name, str):
        raise CompanyRoutingError("COMPANY_DATABASE_IDENTITY_MISMATCH")
    quoted_database = preparer.quote_identifier(database_name)
    connection.exec_driver_sql(
        f"GRANT CONNECT ON DATABASE {quoted_database} TO {quoted_role}"
    )
    connection.exec_driver_sql(f"GRANT USAGE ON SCHEMA public TO {quoted_role}")
    connection.exec_driver_sql(
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public "
        f"TO {quoted_role}"
    )
    connection.exec_driver_sql(
        f"GRANT USAGE, SELECT, UPDATE ON ALL SEQUENCES IN SCHEMA public TO {quoted_role}"
    )
    connection.exec_driver_sql(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {quoted_role}"
    )
    connection.exec_driver_sql(
        "ALTER DEFAULT PRIVILEGES IN SCHEMA public "
        f"GRANT USAGE, SELECT, UPDATE ON SEQUENCES TO {quoted_role}"
    )
