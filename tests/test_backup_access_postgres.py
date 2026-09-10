from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic import SecretStr
from sqlalchemy import create_engine, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_multi_company_postgres import POSTGRES_IMAGE, _upgrade_catalog
from testcontainers.community.postgres import PostgresContainer

from ai_accounting import company_cli, replay_cli
from ai_accounting.backup import verify_portable_backup_archive
from ai_accounting.backup_access import reconcile_backup_access
from ai_accounting.backup_integration import BackupIntegrationError
from ai_accounting.close_backup import CloseBackupService
from ai_accounting.company_router import CompanyDatabaseRouter, grant_finance_database_access
from ai_accounting.company_schemas import ConfigureCloseBackupRequest, CreateCompanyRequest
from ai_accounting.company_service import CompanyService
from ai_accounting.config import Settings
from ai_accounting.identity import ExecutionContext, ExecutorKind
from ai_accounting.models import CompanyRegistry

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


def test_production_creation_repair_and_real_online_backup(tmp_path, monkeypatch):
    """Reproduce the real missing-CONNECT incident with restricted roles and a real ZIP."""
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        base = make_url(postgres.get_connection_url(driver="psycopg"))
        admin = create_engine(base, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.exec_driver_sql("CREATE ROLE finance_runtime LOGIN PASSWORD 'runtime-test-secret'")
            conn.exec_driver_sql(
                "CREATE ROLE finance_migrator LOGIN CREATEDB PASSWORD 'migrator-test-secret'"
            )
            conn.exec_driver_sql("CREATE ROLE finance_backup LOGIN PASSWORD 'backup-test-secret'")
            conn.exec_driver_sql("GRANT pg_read_all_data, pg_monitor TO finance_backup")
            conn.exec_driver_sql("CREATE DATABASE finance_catalog OWNER finance_migrator")
            for name in conn.scalars(text("SELECT datname FROM pg_database WHERE datallowconn")):
                quoted = conn.dialect.identifier_preparer.quote_identifier(name)
                conn.exec_driver_sql(f"REVOKE CONNECT ON DATABASE {quoted} FROM PUBLIC")
            conn.exec_driver_sql("GRANT CONNECT ON DATABASE postgres TO finance_migrator")

        migrator = base.set(username="finance_migrator", password="migrator-test-secret")
        runtime = base.set(username="finance_runtime", password="runtime-test-secret")
        catalog_id = uuid.uuid4()
        catalog_admin = migrator.set(database="finance_catalog")
        _upgrade_catalog(catalog_admin.render_as_string(hide_password=False), catalog_id)
        provisioning = create_engine(catalog_admin)
        with provisioning.begin() as conn:
            grant_finance_database_access(conn, "finance_runtime")
        storage = tmp_path / "storage"
        evidence = storage / "evidence"
        evidence.mkdir(parents=True)
        settings = Settings(
            _env_file=None,
            finance_environment="production",
            database_url=runtime.set(database="finance_catalog").render_as_string(
                hide_password=False
            ),
            finance_company_database_url=runtime.render_as_string(hide_password=False),
            finance_migration_database_url=migrator.render_as_string(hide_password=False),
            finance_provisioning_database_url=migrator.set(database="postgres").render_as_string(
                hide_password=False
            ),
            finance_storage_dir=storage,
            finance_evidence_dir=evidence,
            finance_service_lock_file=storage / "service.lock",
            finance_evidence_import_dir=tmp_path / "evidence-import",
            finance_bank_import_dir=tmp_path / "bank-import",
        )
        router = CompanyDatabaseRouter(settings)
        catalog = create_engine(settings.database_url)
        context = ExecutionContext(
            org_id=uuid.uuid4(),
            owner_account_id=uuid.uuid4(),
            owner_session_id=uuid.uuid4(),
            owner_credential_version=1,
            executor_kind=ExecutorKind.AI_AGENT,
            executor_name="backup-integration-test",
            executor_version="1",
            request_correlation_id=uuid.uuid4(),
            catalog_instance_id=catalog_id,
        )
        try:
            with Session(catalog) as session, session.begin():
                result = CompanyService(
                    session, context=context, database_router=router
                ).create_company(
                    CreateCompanyRequest(
                        idempotency_key="backup-ready-company",
                        name="备份测试公司",
                        taxpayer_identification_number="91330108MABXE0HA3F",
                        effective_from=date(2026, 8, 1),
                        filing_cycle="quarterly",
                        urban_maintenance_rate=Decimal("0.07"),
                        confirmation_note="隔离测试",
                    )
                )
                assert result["status"] == "created", result
                registry = session.scalar(select(CompanyRegistry))
                assert registry is not None
                context = replace(context, org_id=registry.org_id)
                db_name = registry.database_name

            # A new company already has backup access; read-only role cannot write.
            backup = create_engine(
                base.set(database=db_name, username="finance_backup", password="backup-test-secret")
            )
            with backup.connect() as conn:
                assert conn.scalar(text("SELECT count(*) FROM organizations")) == 1
                with pytest.raises(DBAPIError):
                    conn.execute(text("DELETE FROM organizations"))
                conn.rollback()
            backup.dispose()

            # Import and replay use the same production contract, including after ACL loss.
            quoted = admin.dialect.identifier_preparer.quote_identifier(db_name)
            monkeypatch.setattr(replay_cli, "get_settings", lambda: settings)
            for grant in (company_cli._grant_runtime_access, replay_cli._grant_runtime_access):
                with admin.connect() as conn:
                    conn.exec_driver_sql(f"REVOKE CONNECT ON DATABASE {quoted} FROM finance_backup")
                grant(migrator.set(database=db_name), "finance_runtime")
                with admin.connect() as conn:
                    assert conn.scalar(
                        text("SELECT has_database_privilege('finance_backup', :db, 'CONNECT')"),
                        {"db": db_name},
                    )

            class PasswordStore:
                def load_password(self):
                    return SecretStr("backup-test-secret")

            monkeypatch.setattr(
                "ai_accounting.close_backup.WindowsFinanceBackupCredentialStore", PasswordStore
            )
            with Session(catalog) as session, session.begin():
                service = CloseBackupService(session, context=context, settings=settings)
                # The real PostgreSQL 17 client and Windows durable publisher are used.
                service.configure(
                    ConfigureCloseBackupRequest(
                        org_id=context.org_id,
                        backup_directory=str(tmp_path / "backups"),
                        idempotency_key="backup-dir",
                        confirmation_note="隔离测试目录",
                    )
                )
                assert service.get_configuration()["readiness"] == "ready"

            with admin.connect() as conn:
                conn.exec_driver_sql(f"REVOKE CONNECT ON DATABASE {quoted} FROM finance_backup")
            with Session(catalog) as session, session.begin():
                service = CloseBackupService(session, context=context, settings=settings)
                readonly = service.get_configuration()
                assert readonly["readiness"] == "not_ready"
                assert readonly["readiness_errors"] == ["BACKUP_DATABASE_CONNECT_PERMISSION_DENIED"]
                assert service.prepare()["readiness"] == "ready"
                # Repeat preparation to prove idempotency, then exercise actual export/ZIP.
                assert service.prepare()["readiness"] == "ready"
                registry = session.get(CompanyRegistry, context.org_id)
                prepared = service.require_ready()
                archive = service._create_archive(
                    registry=registry,
                    close_id=uuid.uuid4(),
                    period_month="2026-08",
                    attempt_number=1,
                    runtime=prepared,
                )
                verified = verify_portable_backup_archive(archive.archive_file)
                assert verified.org_id == str(context.org_id)
                assert verified.database_identity == str(registry.database_identity)
                assert Path(verified.archive_file).is_file()

            # No partial repair if one missing target has the wrong catalog binding.
            with admin.connect() as conn:
                conn.exec_driver_sql(
                    "REVOKE CONNECT ON DATABASE finance_catalog FROM finance_backup"
                )
                conn.exec_driver_sql(f"REVOKE CONNECT ON DATABASE {quoted} FROM finance_backup")
            with Session(catalog) as session, session.begin():
                row = session.get(CompanyRegistry, context.org_id)
                wrong = SimpleNamespace(
                    org_id=row.org_id,
                    database_name=row.database_name,
                    database_identity=uuid.uuid4(),
                )
                with pytest.raises(BackupIntegrationError, match="DATABASE_IDENTITY_MISMATCH"):
                    reconcile_backup_access(session, settings, [wrong])
                for name in ("finance_catalog", db_name):
                    assert not session.scalar(
                        text("SELECT has_database_privilege('finance_backup', :db, 'CONNECT')"),
                        {"db": name},
                    )
                reconcile_backup_access(session, settings, [row])
                assert (
                    CloseBackupService(session, context=context, settings=settings).prepare()[
                        "readiness"
                    ]
                    == "ready"
                )

            # Never conceal unexpected access by revoking it or expanding the allowlist.
            with admin.connect() as conn:
                conn.exec_driver_sql("GRANT CONNECT ON DATABASE postgres TO finance_backup")
                conn.exec_driver_sql(f"REVOKE CONNECT ON DATABASE {quoted} FROM finance_backup")
            with Session(catalog) as session, session.begin():
                rows = session.scalars(select(CompanyRegistry)).all()
                with pytest.raises(BackupIntegrationError, match="CONNECT_PRIVILEGES_INVALID"):
                    reconcile_backup_access(session, settings, rows)
                assert not session.scalar(
                    text("SELECT has_database_privilege('finance_backup', :db, 'CONNECT')"),
                    {"db": db_name},
                )
        finally:
            router.dispose()
            catalog.dispose()
            provisioning.dispose()
            admin.dispose()
