"""Offline multi-company cutover and handoff-import commands."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import MetaData, create_engine, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

from alembic import command

from .accounting_periods import canonical_sha256
from .backup import BackupError, EvidenceSnapshot, verify_backup
from .backup_credentials import (
    WindowsProtectedArchiveCopyProvider,
    WindowsProtectedPgPassProvider,
)
from .backup_integration import PgRestoreAdapter, PostgresEndpoint
from .company_router import (
    CompanyDatabaseRouter,
    assert_provisioning_role,
    grant_runtime_database_access,
)
from .config import get_settings
from .credential_store import WindowsCredentialStore
from .identity import ExecutorIdentity, ExecutorKind, IdentityError
from .identity_service import IdentityService
from .models import (
    CatalogMetadata,
    CompanyLifecycleAction,
    CompanyRegistry,
    Organization,
    OrganizationDatabaseMetadata,
    OrganizationProfileVersion,
    utcnow,
)
from .path_security import (
    PathSecurityError,
    ensure_directory_in_root,
    read_regular_file_in_root,
    write_new_regular_file_in_root,
)
from .windows_backup import WindowsCurrentUserOnlyAclVerifier

_ROOT = Path(__file__).resolve().parents[2]
_IDENTITY_TABLES = (
    "owner_accounts",
    "owner_sessions",
    "owner_recovery_codes",
    "identity_audit_events",
)
_CATALOG_TABLES = frozenset(
    {
        "alembic_version",
        "catalog_metadata",
        "company_registry",
        "company_lifecycle_actions",
        *_IDENTITY_TABLES,
    }
)
_COUNT_TABLES = (
    "organizations",
    "business_events",
    "vouchers",
    "open_items",
    "execution_attributions",
    "accounting_period_close_approvals",
)


class CompanyCliError(ValueError):
    pass


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Offline company-database lifecycle utility")
    commands = parser.add_subparsers(dest="command", required=True)
    migrate = commands.add_parser(
        "migrate-single-database",
        help="split a stopped 0001 finance database into catalog plus one company database",
    )
    migrate.add_argument("--backup-root", type=Path, required=True)
    migrate.add_argument("--backup-directory", type=Path, required=True)
    migrate.add_argument("--restore-drill-manifest-sha256", required=True)
    migrate.add_argument("--env-file", type=Path, default=Path(".env"))
    import_company = commands.add_parser(
        "import-company",
        help="restore and register one independently exported handoff company",
    )
    import_company.add_argument("--backup-root", type=Path, required=True)
    import_company.add_argument("--backup-directory", type=Path, required=True)
    import_company.add_argument("--pg-bin-dir", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        if args.command == "migrate-single-database":
            _migrate_single_database(args)
        elif args.command == "import-company":
            _import_company(args)
    except (BackupError, CompanyCliError, IdentityError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("COMPANY_LOCAL_COMMAND_FAILED", file=sys.stderr)
        raise SystemExit(1) from None


def _migrate_single_database(args: argparse.Namespace) -> None:
    settings = get_settings()
    if settings.finance_provisioning_database_url is None:
        raise CompanyCliError("COMPANY_PROVISIONING_NOT_CONFIGURED")
    backup_root = args.backup_root.resolve(strict=True)
    backup_directory = args.backup_directory.resolve(strict=True)
    verification = verify_backup(backup_root, backup_directory)
    if (
        verification.schema_revision != "0001_formal_baseline"
        or verification.purpose != "pre_upgrade"
    ):
        raise CompanyCliError("COMPANY_MIGRATION_BACKUP_REVISION_INVALID")
    if verification.manifest_sha256 != args.restore_drill_manifest_sha256:
        raise CompanyCliError("COMPANY_MIGRATION_RESTORE_DRILL_NOT_VERIFIED")

    source_runtime_url = make_url(settings.database_url)
    source_migration_url = make_url(
        settings.finance_migration_database_url or settings.database_url
    )
    if source_runtime_url.database != "finance" or source_migration_url.database != "finance":
        raise CompanyCliError("COMPANY_MIGRATION_SOURCE_DATABASE_INVALID")
    catalog_runtime_url = source_runtime_url.set(database="finance_catalog")
    catalog_migration_url = source_migration_url.set(database="finance_catalog")
    source_engine = create_engine(source_migration_url)
    try:
        precheck = _source_precheck(source_engine)
        org_id = precheck["org_id"]
        database_identity = uuid.uuid5(org_id, "finance-company-database")
        catalog_id = uuid.uuid4()
        _create_catalog_database(settings.finance_provisioning_database_url)
        _catalog_target_precheck(catalog_migration_url, org_id=org_id)
        _upgrade_catalog(catalog_migration_url, catalog_id)
        catalog_engine = create_engine(catalog_migration_url)
        try:
            with Session(catalog_engine) as metadata_session:
                metadata = metadata_session.get(CatalogMetadata, 1)
                if metadata is None:
                    raise CompanyCliError("COMPANY_MIGRATION_CATALOG_NOT_INITIALIZED")
                catalog_id = metadata.catalog_instance_id
            _copy_catalog_identity(
                source_engine=source_engine,
                catalog_engine=catalog_engine,
                org_id=org_id,
                database_identity=database_identity,
                precheck=precheck,
                backup_manifest_sha256=verification.manifest_sha256,
            )
            _upgrade_existing_business(
                source_migration_url,
                org_id=org_id,
                database_identity=database_identity,
                catalog_id=catalog_id,
            )
            if settings.finance_environment == "production":
                runtime_role = source_runtime_url.username
                if runtime_role is None:
                    raise CompanyCliError("COMPANY_RUNTIME_ACCOUNT_INVALID")
                _grant_runtime_access(catalog_migration_url, runtime_role)
                _grant_runtime_access(source_migration_url, runtime_role)
            _verify_source_after_cutover(source_engine, precheck, database_identity)
            with Session(catalog_engine) as catalog_session, catalog_session.begin():
                registry = catalog_session.get(CompanyRegistry, org_id)
                if registry is None:
                    raise CompanyCliError("COMPANY_MIGRATION_CATALOG_REGISTRATION_MISSING")
                registry.status = "active"
                registry.updated_at = utcnow()
                action = catalog_session.scalar(
                    select(CompanyLifecycleAction).where(
                        CompanyLifecycleAction.org_id == org_id,
                        CompanyLifecycleAction.action_type == "import",
                        CompanyLifecycleAction.idempotency_key == "migrate-single-database",
                    )
                )
                if action is not None:
                    action.status = "completed"
                    action.completed_at = utcnow()
            _write_cutover_environment(
                args.env_file,
                catalog_runtime_url=catalog_runtime_url,
                company_runtime_url=source_runtime_url,
            )
        finally:
            catalog_engine.dispose()
    finally:
        source_engine.dispose()
    print(f"COMPANY_MIGRATION_COMPLETE {org_id}")
    print("DATABASE_URL_NOW_TARGETS_FINANCE_CATALOG")


class _MigrationPasswordStore:
    def __init__(self, password: str) -> None:
        self._password = SecretStr(password)

    def load_password(self) -> SecretStr:
        return self._password


def _import_company(args: argparse.Namespace) -> None:
    settings = get_settings()
    if not settings.multi_company_enabled or settings.finance_provisioning_database_url is None:
        raise CompanyCliError("MULTI_COMPANY_NOT_CONFIGURED")
    backup_root = args.backup_root.resolve(strict=True)
    backup_directory = args.backup_directory.resolve(strict=True)
    pg_bin_dir = args.pg_bin_dir.resolve(strict=True)
    verification = verify_backup(backup_root, backup_directory)
    if (
        verification.artifact_type != "company"
        or verification.purpose != "handoff"
        or verification.org_id is None
        or verification.database_identity is None
    ):
        raise CompanyCliError("COMPANY_IMPORT_HANDOFF_ARTIFACT_REQUIRED")
    org_id = uuid.UUID(verification.org_id)
    database_identity = uuid.UUID(verification.database_identity)
    import_idempotency_key = f"handoff:{verification.manifest_sha256}"
    import_request_hash = canonical_sha256(
        {
            "manifest_sha256": verification.manifest_sha256,
            "org_id": str(org_id),
            "database_identity": str(database_identity),
        }
    )
    scripts = ScriptDirectory.from_config(Config(str(_ROOT / "alembic.ini")))
    heads = scripts.get_heads()
    if heads != [verification.schema_revision]:
        raise CompanyCliError("COMPANY_IMPORT_SCHEMA_REVISION_MISMATCH")

    token = WindowsCredentialStore().load_session_token()
    if token is None:
        raise IdentityError("IDENTITY_LOCAL_SESSION_REQUIRED")
    migration_url = make_url(settings.finance_migration_database_url or "")
    if not migration_url.username or migration_url.password is None:
        raise CompanyCliError("COMPANY_IMPORT_MIGRATION_ACCOUNT_REQUIRED")
    database_name = f"finance_company_{uuid.uuid4().hex}"
    CompanyDatabaseRouter.validate_database_name(database_name)
    target_url = migration_url.set(database=database_name)
    catalog_url = make_url(settings.database_url)
    verifier = WindowsCurrentUserOnlyAclVerifier()
    password_store = _MigrationPasswordStore(migration_url.password)
    pgpass_provider = WindowsProtectedPgPassProvider(
        password_store,
        settings.finance_storage_dir,
        verifier,
    )
    archive_provider = WindowsProtectedArchiveCopyProvider(
        settings.finance_storage_dir,
        verifier,
    )
    target_endpoint = PostgresEndpoint(
        host=target_url.host or "",
        port=target_url.port or 5432,
        database=database_name,
        username=migration_url.username,
        application_name="finance-company-import",
    )
    catalog_endpoint = PostgresEndpoint(
        host=catalog_url.host or "",
        port=catalog_url.port or 5432,
        database=catalog_url.database or "",
        username=migration_url.username,
        application_name="finance-company-import-check",
    )
    adapter = PgRestoreAdapter(
        target_endpoint,
        catalog_endpoint,
        pgpass_provider,
        verifier,
        pg_restore_executable=pg_bin_dir / "pg_restore.exe",
    )

    catalog_engine = create_engine(settings.database_url)
    try:
        with Session(catalog_engine) as catalog_session, catalog_session.begin():
            catalog_session.info["catalog_mode"] = True
            context_value = IdentityService(catalog_session).authorize_execution(
                session_token=token.get_secret_value(),
                executor=ExecutorIdentity(
                    kind=ExecutorKind.SYSTEM_JOB,
                    executor_name="finance-company",
                    executor_version="0.1.0",
                ),
                request_correlation_id=uuid.uuid4(),
            )
            existing_registry = catalog_session.get(CompanyRegistry, org_id)
            if existing_registry is not None:
                existing_action = catalog_session.scalar(
                    select(CompanyLifecycleAction).where(
                        CompanyLifecycleAction.org_id == org_id,
                        CompanyLifecycleAction.action_type == "import",
                        CompanyLifecycleAction.idempotency_key == import_idempotency_key,
                    )
                )
                if (
                    existing_action is not None
                    and existing_action.status == "completed"
                    and existing_action.request_payload_hash == import_request_hash
                    and existing_registry.database_identity == database_identity
                ):
                    print(f"COMPANY_IMPORT_ALREADY_COMPLETE {org_id}")
                    return
                raise CompanyCliError("COMPANY_IMPORT_ORGANIZATION_ALREADY_EXISTS")
            _create_named_database(settings.finance_provisioning_database_url, database_name)
            archive = backup_directory / "database.dump"
            with archive_provider.lease_verified_archive(
                archive,
                backup_root,
                expected_sha256=verification.database_sha256,
                expected_size_bytes=verification.database_size_bytes,
            ) as protected_archive:
                adapter.restore(protected_archive)
            imported = _validate_imported_company(
                target_url,
                org_id=org_id,
                database_identity=database_identity,
                expected_schema_revision=verification.schema_revision,
                expected_evidence=verification.evidence,
            )
            evidence_paths = _install_imported_evidence(
                verification.evidence,
                backup_root=backup_root,
                evidence_root=settings.finance_evidence_dir,
                max_evidence_bytes=settings.finance_max_evidence_bytes,
            )
            duplicate_taxpayer = catalog_session.scalar(
                select(CompanyRegistry.org_id).where(
                    CompanyRegistry.taxpayer_identification_number
                    == imported.taxpayer_identification_number
                )
            )
            if duplicate_taxpayer is not None:
                raise CompanyCliError("COMPANY_IMPORT_TAXPAYER_ALREADY_EXISTS")
            catalog_id = catalog_session.get(CatalogMetadata, 1)
            if catalog_id is None:
                raise CompanyCliError("COMPANY_IMPORT_CATALOG_NOT_INITIALIZED")
            target_engine = create_engine(target_url)
            try:
                with target_engine.begin() as target:
                    target.execute(
                        text(
                            "UPDATE organization_database_metadata "
                            "SET current_catalog_instance_id = :catalog_id "
                            "WHERE singleton_key = 1"
                        ),
                        {"catalog_id": catalog_id.catalog_instance_id},
                    )
                    for evidence_id, storage_path in evidence_paths.items():
                        target.execute(
                            text(
                                "UPDATE evidence SET storage_path = :storage_path "
                                "WHERE id = :evidence_id AND org_id = :org_id"
                            ),
                            {
                                "storage_path": storage_path,
                                "evidence_id": evidence_id,
                                "org_id": org_id,
                            },
                        )
            finally:
                target_engine.dispose()
            if settings.finance_environment == "production":
                runtime_role = make_url(settings.finance_company_database_url or "").username
                if runtime_role is None:
                    raise CompanyCliError("COMPANY_RUNTIME_ACCOUNT_INVALID")
                _grant_runtime_access(target_url, runtime_role)
            latest_profile = imported.profile
            registry = CompanyRegistry(
                org_id=org_id,
                database_name=database_name,
                database_identity=database_identity,
                status="active",
                display_name=latest_profile.name,
                taxpayer_identification_number=latest_profile.taxpayer_identification_number,
                profile_effective_from=latest_profile.effective_from,
                filing_cycle=latest_profile.filing_cycle,
                urban_maintenance_rate=latest_profile.urban_maintenance_rate,
            )
            catalog_session.add(registry)
            catalog_session.flush()
            catalog_session.add(
                CompanyLifecycleAction(
                    org_id=org_id,
                    action_type="import",
                    idempotency_key=import_idempotency_key,
                    request_payload_hash=import_request_hash,
                    status="completed",
                    input_facts={
                        "artifact_type": "company",
                        "manifest_sha256": verification.manifest_sha256,
                    },
                    owner_account_id=context_value.owner_account_id,
                    owner_session_id=context_value.owner_session_id,
                    owner_credential_version=context_value.owner_credential_version,
                    executor_kind=context_value.executor_kind.value,
                    executor_name=context_value.executor_name,
                    executor_version=context_value.executor_version,
                    completed_at=utcnow(),
                )
            )
    finally:
        catalog_engine.dispose()
    print(f"COMPANY_IMPORT_COMPLETE {org_id}")


class _ImportedCompany:
    def __init__(self, organization: Organization, profile: OrganizationProfileVersion) -> None:
        self.taxpayer_identification_number = organization.taxpayer_identification_number
        self.profile = profile


def _validate_imported_company(
    target_url: URL,
    *,
    org_id: uuid.UUID,
    database_identity: uuid.UUID,
    expected_schema_revision: str,
    expected_evidence: tuple[object, ...],
) -> _ImportedCompany:
    engine = create_engine(target_url)
    try:
        table_names = set(inspect(engine).get_table_names())
        if table_names & set(_IDENTITY_TABLES):
            raise CompanyCliError("COMPANY_IMPORT_IDENTITY_TABLE_FORBIDDEN")
        with Session(engine) as session:
            revision = session.scalar(text("SELECT version_num FROM alembic_version"))
            if revision != expected_schema_revision:
                raise CompanyCliError("COMPANY_IMPORT_SCHEMA_REVISION_MISMATCH")
            organizations = session.scalars(select(Organization)).all()
            if len(organizations) != 1 or organizations[0].id != org_id:
                raise CompanyCliError("COMPANY_IMPORT_ORGANIZATION_MISMATCH")
            metadata = session.get(OrganizationDatabaseMetadata, 1)
            if metadata is None or metadata.database_identity != database_identity:
                raise CompanyCliError("COMPANY_IMPORT_DATABASE_IDENTITY_MISMATCH")
            profile = session.scalar(
                select(OrganizationProfileVersion)
                .where(OrganizationProfileVersion.org_id == org_id)
                .order_by(OrganizationProfileVersion.effective_from.desc())
                .limit(1)
            )
            if profile is None:
                raise CompanyCliError("COMPANY_IMPORT_PROFILE_MISSING")
            evidence_rows = session.execute(
                text("SELECT id::text, sha256, size_bytes FROM evidence ORDER BY id::text")
            ).mappings().all()
            expected = sorted(
                (item.evidence_id, item.sha256, item.size_bytes) for item in expected_evidence
            )
            actual = sorted(
                (row["id"], row["sha256"], row["size_bytes"]) for row in evidence_rows
            )
            if actual != expected:
                raise CompanyCliError("COMPANY_IMPORT_EVIDENCE_MISMATCH")
            return _ImportedCompany(organizations[0], profile)
    finally:
        engine.dispose()


def _install_imported_evidence(
    evidence: tuple[EvidenceSnapshot, ...],
    *,
    backup_root: Path,
    evidence_root: Path,
    max_evidence_bytes: int,
) -> dict[uuid.UUID, str]:
    """Install verified handoff evidence into the target content-addressed store."""

    try:
        destination_root = ensure_directory_in_root(evidence_root, evidence_root)
        installed: dict[uuid.UUID, str] = {}
        for item in evidence:
            evidence_id = uuid.UUID(str(item.evidence_id))
            digest = str(item.sha256)
            _, content = read_regular_file_in_root(
                Path(item.storage_path),
                backup_root,
                max_bytes=max_evidence_bytes,
            )
            if (
                len(content) != int(item.size_bytes)
                or hashlib.sha256(content).hexdigest() != digest
            ):
                raise CompanyCliError("COMPANY_IMPORT_EVIDENCE_MISMATCH")
            destination_parent = ensure_directory_in_root(
                destination_root / digest[:2] / digest[2:4],
                destination_root,
            )
            destination = destination_parent / digest
            if destination.exists():
                destination, existing = read_regular_file_in_root(
                    destination,
                    destination_root,
                    max_bytes=max_evidence_bytes,
                )
                if hashlib.sha256(existing).hexdigest() != digest or len(existing) != len(content):
                    raise CompanyCliError("COMPANY_IMPORT_EVIDENCE_CONFLICT")
            else:
                destination = write_new_regular_file_in_root(
                    destination,
                    destination_root,
                    content,
                    max_bytes=max_evidence_bytes,
                )
            installed[evidence_id] = str(destination)
        return installed
    except PathSecurityError as exc:
        raise CompanyCliError("COMPANY_IMPORT_EVIDENCE_UNAVAILABLE") from exc


def _create_named_database(provisioning_url: str, database_name: str) -> None:
    engine = create_engine(provisioning_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            if get_settings().finance_environment == "production":
                assert_provisioning_role(connection)
            if connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = :name"),
                {"name": database_name},
            ) is not None:
                raise CompanyCliError("COMPANY_IMPORT_DATABASE_ALREADY_EXISTS")
            connection.exec_driver_sql(f'CREATE DATABASE "{database_name}"')
    finally:
        engine.dispose()


def _grant_runtime_access(database_url: URL, runtime_role: str) -> None:
    engine = create_engine(database_url)
    try:
        with engine.begin() as connection:
            grant_runtime_database_access(connection, runtime_role)
    finally:
        engine.dispose()


def _source_precheck(engine: sa.Engine) -> dict[str, object]:
    with engine.connect() as connection:
        revisions = connection.execute(
            text("SELECT version_num FROM alembic_version")
        ).scalars().all()
        if revisions != ["0001_formal_baseline"]:
            raise CompanyCliError("COMPANY_MIGRATION_SOURCE_REVISION_UNKNOWN")
        organizations = connection.execute(
            text(
                "SELECT id, name, taxpayer_identification_number, filing_cycle, "
                "urban_maintenance_rate, created_at FROM organizations ORDER BY id"
            )
        ).mappings().all()
        if len(organizations) != 1:
            raise CompanyCliError("COMPANY_MIGRATION_REQUIRES_ONE_ORGANIZATION")
        counts = {
            table: int(connection.scalar(text(f'SELECT COUNT(*) FROM "{table}"')) or 0)
            for table in _COUNT_TABLES
        }
        identity = {
            table: _table_digest(connection, table)
            for table in _IDENTITY_TABLES
        }
        owner_count = len(identity["owner_accounts"]["rows"])
        if owner_count != 1:
            raise CompanyCliError("COMPANY_MIGRATION_REQUIRES_ONE_OWNER")
        return {
            "org_id": uuid.UUID(str(organizations[0]["id"])),
            "organization": dict(organizations[0]),
            "counts": counts,
            "identity": identity,
        }


def _create_catalog_database(provisioning_url: str) -> None:
    engine = create_engine(provisioning_url, isolation_level="AUTOCOMMIT")
    try:
        with engine.connect() as connection:
            if get_settings().finance_environment == "production":
                assert_provisioning_role(connection)
            exists = connection.scalar(
                text("SELECT 1 FROM pg_database WHERE datname = 'finance_catalog'")
            )
            if exists is None:
                connection.exec_driver_sql('CREATE DATABASE "finance_catalog"')
    finally:
        engine.dispose()


def _upgrade_catalog(url: URL, catalog_id: uuid.UUID) -> None:
    config = Config(str(_ROOT / "catalog_alembic.ini"))
    rendered = url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", rendered.replace("%", "%%"))
    config.attributes["database_url_override"] = rendered
    config.attributes["catalog_instance_id"] = catalog_id
    command.upgrade(config, "head")


def _catalog_target_precheck(url: URL, *, org_id: uuid.UUID) -> None:
    """Refuse to run the catalog migration tree against an unknown database."""

    engine = create_engine(url)
    try:
        table_names = set(inspect(engine).get_table_names())
        if not table_names:
            return
        if table_names != _CATALOG_TABLES:
            raise CompanyCliError("COMPANY_MIGRATION_CATALOG_HISTORY_UNKNOWN")
        with engine.connect() as connection:
            revisions = connection.execute(
                text("SELECT version_num FROM alembic_version ORDER BY version_num")
            ).scalars().all()
            registered = connection.execute(
                text("SELECT org_id FROM company_registry ORDER BY org_id")
            ).scalars().all()
        if revisions != ["0002_company_primary"] or registered not in ([], [org_id]):
            raise CompanyCliError("COMPANY_MIGRATION_CATALOG_HISTORY_UNKNOWN")
    finally:
        engine.dispose()


def _copy_catalog_identity(
    *,
    source_engine: sa.Engine,
    catalog_engine: sa.Engine,
    org_id: uuid.UUID,
    database_identity: uuid.UUID,
    precheck: dict[str, object],
    backup_manifest_sha256: str,
) -> None:
    organization = precheck["organization"]
    assert isinstance(organization, dict)
    with catalog_engine.begin() as target:
        metadata = target.execute(
            text("SELECT catalog_instance_id FROM catalog_metadata WHERE singleton_key = 1")
        ).scalar_one_or_none()
        if metadata is None:
            raise CompanyCliError("COMPANY_MIGRATION_CATALOG_NOT_INITIALIZED")
        existing = target.execute(
            text(
                "SELECT org_id, database_name, database_identity, status, display_name, "
                "taxpayer_identification_number, filing_cycle, urban_maintenance_rate "
                "FROM company_registry ORDER BY org_id"
            )
        ).mappings().all()
        if existing and [item["org_id"] for item in existing] != [org_id]:
            raise CompanyCliError("COMPANY_MIGRATION_CATALOG_TARGET_NOT_EMPTY")
        if not existing:
            target.execute(
                text(
                    "INSERT INTO company_registry "
                    "(org_id, database_name, database_identity, status, display_name, "
                    "taxpayer_identification_number, profile_effective_from, filing_cycle, "
                    "urban_maintenance_rate, is_primary, created_at, updated_at, archived_at) "
                    "VALUES (:org_id, 'finance', :database_identity, 'provisioning', :name, "
                    ":taxpayer_id, DATE '0001-01-01', :filing_cycle, :urban_rate, TRUE, "
                    "CURRENT_TIMESTAMP, CURRENT_TIMESTAMP, NULL)"
                ),
                {
                    "org_id": org_id,
                    "database_identity": database_identity,
                    "name": organization["name"],
                    "taxpayer_id": organization["taxpayer_identification_number"],
                    "filing_cycle": organization["filing_cycle"],
                    "urban_rate": organization["urban_maintenance_rate"],
                },
            )
        else:
            registry = existing[0]
            if (
                registry["database_name"] != "finance"
                or registry["database_identity"] != database_identity
                or registry["status"] != "provisioning"
                or registry["display_name"] != organization["name"]
                or registry["taxpayer_identification_number"]
                != organization["taxpayer_identification_number"]
                or registry["filing_cycle"] != organization["filing_cycle"]
                or registry["urban_maintenance_rate"]
                != organization["urban_maintenance_rate"]
            ):
                raise CompanyCliError("COMPANY_MIGRATION_CATALOG_RETRY_MISMATCH")
        target_metadata = MetaData()
        target_metadata.reflect(target, only=list(_IDENTITY_TABLES))
        for table_name in _IDENTITY_TABLES:
            target.exec_driver_sql(f'ALTER TABLE "{table_name}" DISABLE TRIGGER USER')
        with source_engine.connect() as source:
            source_metadata = MetaData()
            source_metadata.reflect(source, only=list(_IDENTITY_TABLES))
            for table_name in _IDENTITY_TABLES:
                target_count = int(
                    target.scalar(text(f'SELECT COUNT(*) FROM "{table_name}"')) or 0
                )
                rows = source.execute(
                    select(source_metadata.tables[table_name]).order_by(
                        source_metadata.tables[table_name].c.id
                    )
                ).mappings().all()
                if target_count == 0 and rows:
                    target.execute(
                        target_metadata.tables[table_name].insert(),
                        [dict(row) for row in rows],
                    )
                elif target_count != len(rows):
                    raise CompanyCliError("COMPANY_MIGRATION_IDENTITY_COPY_MISMATCH")
        for table_name in _IDENTITY_TABLES:
            target.exec_driver_sql(f'ALTER TABLE "{table_name}" ENABLE TRIGGER USER')
        identity = precheck["identity"]
        assert isinstance(identity, dict)
        for table_name in _IDENTITY_TABLES:
            copied = _table_digest(target, table_name)
            if (
                copied["count"] != identity[table_name]["count"]
                or copied["sha256"] != identity[table_name]["sha256"]
            ):
                raise CompanyCliError("COMPANY_MIGRATION_IDENTITY_COPY_MISMATCH")
        owner = target.execute(text("SELECT * FROM owner_accounts LIMIT 1")).mappings().one()
        request_hash = canonical_sha256(
            {
                "command": "migrate-single-database",
                "org_id": str(org_id),
                "backup_manifest_sha256": backup_manifest_sha256,
            }
        )
        target.execute(
            text(
                "INSERT INTO company_lifecycle_actions "
                "(id, org_id, action_type, idempotency_key, request_payload_hash, status, "
                "input_facts, calculation_hash, error_code, owner_account_id, owner_session_id, "
                "owner_credential_version, executor_kind, executor_name, executor_version, "
                "created_at, completed_at) "
                "SELECT :id, :org_id, 'import', 'migrate-single-database', :request_hash, "
                "'started', CAST(:input_facts AS jsonb), NULL, NULL, :owner_account_id, id, "
                ":credential_version, 'system_job', 'finance-company', '0.1.0', "
                "CURRENT_TIMESTAMP, NULL FROM owner_sessions "
                "WHERE owner_account_id = :owner_account_id ORDER BY created_at DESC LIMIT 1 "
                "ON CONFLICT (org_id, action_type, idempotency_key) DO NOTHING"
            ),
            {
                "id": uuid.uuid5(org_id, "migrate-single-database-action"),
                "org_id": org_id,
                "request_hash": request_hash,
                "input_facts": json.dumps(
                    {"backup_manifest_sha256": backup_manifest_sha256},
                    separators=(",", ":"),
                ),
                "owner_account_id": owner["id"],
                "credential_version": owner["credential_version"],
            },
        )


def _upgrade_existing_business(
    url: URL,
    *,
    org_id: uuid.UUID,
    database_identity: uuid.UUID,
    catalog_id: uuid.UUID,
) -> None:
    config = Config(str(_ROOT / "alembic.ini"))
    rendered = url.render_as_string(hide_password=False)
    config.set_main_option("sqlalchemy.url", rendered.replace("%", "%%"))
    config.attributes.update(
        {
            "database_url_override": rendered,
            "company_org_id": org_id,
            "company_database_identity": database_identity,
            "catalog_instance_id": catalog_id,
            "identity_split_verified": True,
            "identity_export_verified": True,
        }
    )
    command.upgrade(config, "head")


def _verify_source_after_cutover(
    engine: sa.Engine,
    precheck: dict[str, object],
    database_identity: uuid.UUID,
) -> None:
    counts = precheck["counts"]
    assert isinstance(counts, dict)
    with engine.connect() as connection:
        revision = connection.scalar(text("SELECT version_num FROM alembic_version"))
        if revision != "0002_multi_company_business":
            raise CompanyCliError("COMPANY_MIGRATION_BUSINESS_REVISION_INVALID")
        for table_name, expected in counts.items():
            actual = int(connection.scalar(text(f'SELECT COUNT(*) FROM "{table_name}"')) or 0)
            if actual != expected:
                raise CompanyCliError("COMPANY_MIGRATION_BUSINESS_COUNT_MISMATCH")
        binding = connection.execute(
            text(
                "SELECT database_identity FROM organization_database_metadata "
                "WHERE singleton_key = 1"
            )
        ).scalar_one_or_none()
        if binding != database_identity:
            raise CompanyCliError("COMPANY_MIGRATION_DATABASE_IDENTITY_MISMATCH")
        remaining = set(inspect(connection).get_table_names()) & set(_IDENTITY_TABLES)
        if remaining:
            raise CompanyCliError("COMPANY_MIGRATION_IDENTITY_TABLE_REMAINS")


def _table_digest(connection: sa.Connection, table_name: str) -> dict[str, object]:
    rows = connection.execute(text(f'SELECT * FROM "{table_name}" ORDER BY id')).mappings().all()
    payload = json.dumps(
        [dict(row) for row in rows],
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")
    return {
        "count": len(rows),
        "sha256": hashlib.sha256(payload).hexdigest(),
        "rows": rows,
    }


def _write_cutover_environment(
    env_file: Path,
    *,
    catalog_runtime_url: URL,
    company_runtime_url: URL,
) -> None:
    path = env_file.resolve(strict=True)
    content = path.read_text(encoding="utf-8")
    values = {
        "DATABASE_URL": catalog_runtime_url.render_as_string(hide_password=False),
        "FINANCE_COMPANY_DATABASE_URL": company_runtime_url.render_as_string(
            hide_password=False
        ),
    }
    lines = content.splitlines()
    for key, value in values.items():
        replacement = f"{key}={value}"
        for index, line in enumerate(lines):
            if line.startswith(f"{key}="):
                lines[index] = replacement
                break
        else:
            lines.append(replacement)
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    os.replace(temporary, path)


if __name__ == "__main__":
    main()
