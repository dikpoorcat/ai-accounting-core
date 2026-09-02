"""Offline multi-company handoff-import command."""

from __future__ import annotations

import argparse
import hashlib
import sys
import uuid
from collections.abc import Sequence
from pathlib import Path

from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import URL, make_url
from sqlalchemy.orm import Session

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
class CompanyCliError(ValueError):
    pass


def main(argv: Sequence[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Offline company-database lifecycle utility")
    commands = parser.add_subparsers(dest="command", required=True)
    import_company = commands.add_parser(
        "import-company",
        help="restore and register one independently exported handoff company",
    )
    import_company.add_argument("--backup-root", type=Path, required=True)
    import_company.add_argument("--backup-directory", type=Path, required=True)
    import_company.add_argument("--pg-bin-dir", type=Path, required=True)
    try:
        args = parser.parse_args(argv)
        if args.command == "import-company":
            _import_company(args)
    except (BackupError, CompanyCliError, IdentityError) as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1) from None
    except Exception:
        print("COMPANY_LOCAL_COMMAND_FAILED", file=sys.stderr)
        raise SystemExit(1) from None


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


if __name__ == "__main__":
    main()
