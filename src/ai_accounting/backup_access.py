"""The shared backup-role contract for provisioning and existing company databases."""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import Connection, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from .backup_integration import BackupIntegrationError
from .config import Settings
from .models import CatalogMetadata, CompanyRegistry
from .schema_readiness import DatabaseSchemaError, require_current_schema


def lock_backup_access(session: Session) -> None:
    """Serialize catalog membership publication with exact backup CONNECT checks."""
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('finance-backup-access', 0))")
        )


def require_backup_role(connection: Connection) -> None:
    flags = connection.execute(
        text(
            "SELECT rolcanlogin, rolinherit, rolsuper, rolcreatedb, rolcreaterole, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = 'finance_backup'"
        )
    ).one_or_none()
    if flags is None:
        raise BackupIntegrationError("BACKUP_DATABASE_ROLE_REQUIRED")
    if tuple(flags) != (True, True, False, False, False, False, False):
        raise BackupIntegrationError("BACKUP_DATABASE_ROLE_PRIVILEGES_INVALID")
    memberships = connection.scalars(
        text(
            "SELECT parent.rolname FROM pg_auth_members m "
            "JOIN pg_roles child ON child.oid = m.member "
            "JOIN pg_roles parent ON parent.oid = m.roleid "
            "WHERE child.rolname = 'finance_backup' ORDER BY parent.rolname"
        )
    ).all()
    if memberships != ["pg_monitor", "pg_read_all_data"]:
        raise BackupIntegrationError("BACKUP_DATABASE_ROLE_MEMBERSHIP_INVALID")


def grant_backup_database_access(connection: Connection) -> None:
    """Provision the current finance database; never create or elevate the backup role."""
    require_backup_role(connection)
    name = connection.scalar(text("SELECT current_database()"))
    quoted = connection.dialect.identifier_preparer.quote_identifier(name)
    connection.exec_driver_sql(f"REVOKE CONNECT ON DATABASE {quoted} FROM PUBLIC")
    connection.exec_driver_sql(f"GRANT CONNECT ON DATABASE {quoted} TO finance_backup")


def reconcile_backup_access(
    session: Session, settings: Settings, registries: Sequence[CompanyRegistry]
) -> None:
    """Repair only absent CONNECT grants on identity-checked, registered databases.

    This is an infrastructure write, never part of a read-only configuration query.
    Unexpected access, role privileges, schemas or database bindings fail closed.
    """
    lock_backup_access(session)
    connection = session.connection()
    require_backup_role(connection)
    catalog_url = make_url(settings.database_url)
    company_url = make_url(settings.finance_company_database_url or "")
    if (catalog_url.host, catalog_url.port or 5432) != (company_url.host, company_url.port or 5432):
        raise BackupIntegrationError("BACKUP_DATABASE_ENDPOINT_MISMATCH")
    expected = {catalog_url.database, *(item.database_name for item in registries)}
    accessible = set(
        connection.scalars(
            text(
                "SELECT datname FROM pg_database WHERE datallowconn "
                "AND has_database_privilege('finance_backup', oid, 'CONNECT')"
            )
        )
    )
    if accessible - expected:
        raise BackupIntegrationError("BACKUP_DATABASE_ROLE_CONNECT_PRIVILEGES_INVALID")
    missing = expected - accessible
    if not missing:
        return
    if settings.finance_migration_database_url is None:
        raise BackupIntegrationError("BACKUP_ACCESS_REPAIR_NOT_CONFIGURED")
    migration_url = make_url(settings.finance_migration_database_url)
    if (migration_url.host, migration_url.port or 5432) != (
        catalog_url.host,
        catalog_url.port or 5432,
    ):
        raise BackupIntegrationError("BACKUP_DATABASE_ENDPOINT_MISMATCH")
    metadata = session.get(CatalogMetadata, 1)
    if metadata is None:
        raise BackupIntegrationError("CATALOG_NOT_INITIALIZED")

    # Verify every missing target before applying any grant. All ACL changes then
    # commit together on the catalog connection (PostgreSQL database ACLs are global).
    engine = create_engine(
        migration_url.set(database=catalog_url.database), connect_args={"connect_timeout": 10}
    )
    try:
        with engine.begin() as admin:
            require_current_schema(admin, catalog=True)
            actual_id = admin.scalar(
                text("SELECT catalog_instance_id FROM catalog_metadata WHERE singleton_key = 1")
            )
            if actual_id != metadata.catalog_instance_id:
                raise BackupIntegrationError("BACKUP_DATABASE_IDENTITY_MISMATCH")
            require_backup_role(admin)
            for registry in registries:
                if registry.database_name not in missing:
                    continue
                target = create_engine(
                    migration_url.set(database=registry.database_name),
                    connect_args={"connect_timeout": 10},
                )
                try:
                    with target.connect() as probe:
                        require_current_schema(probe)
                        binding = probe.execute(
                            text(
                                "SELECT org_id, database_identity, current_catalog_instance_id "
                                "FROM organization_database_metadata WHERE singleton_key = 1"
                            )
                        ).one_or_none()
                        if binding is None or tuple(binding) != (
                            registry.org_id,
                            registry.database_identity,
                            metadata.catalog_instance_id,
                        ):
                            raise BackupIntegrationError("BACKUP_DATABASE_IDENTITY_MISMATCH")
                finally:
                    target.dispose()
            for name in sorted(missing):
                if not admin.scalar(
                    text(
                        "SELECT current_user = pg_get_userbyid(datdba) FROM pg_database "
                        "WHERE datname = :name AND datallowconn"
                    ),
                    {"name": name},
                ):
                    raise BackupIntegrationError("BACKUP_ACCESS_REPAIR_OWNER_REQUIRED")
            for name in sorted(missing):
                quoted = admin.dialect.identifier_preparer.quote_identifier(name)
                admin.exec_driver_sql(f"GRANT CONNECT ON DATABASE {quoted} TO finance_backup")
    except DatabaseSchemaError as exc:
        raise BackupIntegrationError(exc.code) from exc
    except BackupIntegrationError:
        raise
    except Exception as exc:
        raise BackupIntegrationError("BACKUP_ACCESS_REPAIR_FAILED") from exc
    finally:
        engine.dispose()
