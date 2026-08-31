from __future__ import annotations

import shutil
import uuid
from dataclasses import replace
from datetime import date
from decimal import Decimal

import pytest
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, inspect, select, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.company_cli import (
    _copy_catalog_identity,
    _source_precheck,
    _upgrade_existing_business,
    _verify_source_after_cutover,
)
from ai_accounting.company_router import (
    CompanyDatabaseRouter,
    CompanyRoutingError,
    assert_provisioning_role,
    assert_runtime_role,
    grant_runtime_database_access,
)
from ai_accounting.company_schemas import (
    ConfirmCompanyProfileChangeRequest,
    ConfirmCompanyStatusChangeRequest,
    CreateCompanyRequest,
    PreviewCompanyProfileChangeRequest,
    PreviewCompanyStatusChangeRequest,
)
from ai_accounting.company_service import CompanyLifecycleError, CompanyService
from ai_accounting.config import Settings
from ai_accounting.dashboard_server import load_multi_company_dashboard_context
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.identity import ExecutionContext, ExecutorIdentity, ExecutorKind
from ai_accounting.identity_schemas import OwnerLoginRequest, OwnerProvisionRequest
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import (
    BusinessEvent,
    CatalogMetadata,
    CompanyRegistry,
    Evidence,
    Organization,
    OrganizationDatabaseMetadata,
    OrganizationProfileVersion,
    OwnerAccount,
    OwnerSession,
)
from ai_accounting.organization_profiles import profile_as_of
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]

POSTGRES_IMAGE = "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193"  # noqa: E501


def _database_url(base_url: str, database_name: str) -> str:
    return make_url(base_url).set(database=database_name).render_as_string(hide_password=False)


def _upgrade_catalog(url: str, catalog_id: uuid.UUID) -> None:
    config = Config("catalog_alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["database_url_override"] = url
    config.attributes["catalog_instance_id"] = catalog_id
    command.upgrade(config, "head")


def _check_business_schema(url: str) -> None:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", url.replace("%", "%%"))
    config.attributes["database_url_override"] = url
    command.check(config)


def test_database_roles_enforce_runtime_and_provisioning_boundaries() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        base = make_url(postgres.get_connection_url(driver="psycopg"))
        runtime_role = "finance_runtime_test"
        provisioning_role = "finance_provision_test"
        role_database = "finance_role_test"
        admin = create_engine(base, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as connection:
                connection.exec_driver_sql(
                    f'CREATE ROLE "{runtime_role}" LOGIN PASSWORD \'runtime-test-password\' '
                    "NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
                connection.exec_driver_sql(
                    f'CREATE ROLE "{provisioning_role}" LOGIN '
                    "PASSWORD 'provision-test-password' CREATEDB NOSUPERUSER "
                    "NOCREATEROLE NOREPLICATION NOBYPASSRLS"
                )
                connection.exec_driver_sql(
                    f'CREATE DATABASE "{role_database}" OWNER "{provisioning_role}"'
                )
        finally:
            admin.dispose()

        runtime_url = base.set(
            username=runtime_role,
            password="runtime-test-password",
            database=role_database,
        )
        provisioning_url = base.set(
            username=provisioning_role,
            password="provision-test-password",
            database=role_database,
        )
        provisioning_engine = create_engine(provisioning_url)
        runtime_engine = create_engine(runtime_url)
        try:
            with provisioning_engine.begin() as connection:
                assert_provisioning_role(connection)
                connection.exec_driver_sql(
                    "CREATE TABLE role_boundary_probe (id integer PRIMARY KEY)"
                )
                grant_runtime_database_access(connection, runtime_role)
            with runtime_engine.begin() as connection:
                assert_runtime_role(connection)
                connection.exec_driver_sql("INSERT INTO role_boundary_probe (id) VALUES (1)")
                assert connection.scalar(text("SELECT id FROM role_boundary_probe")) == 1
        finally:
            runtime_engine.dispose()
            provisioning_engine.dispose()


def test_two_company_databases_isolate_identical_ids_and_idempotency_keys(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        base_url = postgres.get_connection_url(driver="psycopg")
        admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
        catalog_database_name = "finance_catalog"
        try:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{catalog_database_name}"')
        finally:
            admin.dispose()

        catalog_url = _database_url(base_url, catalog_database_name)
        catalog_id = uuid.uuid4()
        _upgrade_catalog(catalog_url, catalog_id)
        owner_account_id = uuid.uuid4()
        owner_session_id = uuid.uuid4()
        routing_settings = Settings(
            finance_environment="development",
            database_url=catalog_url,
            finance_company_database_url=base_url,
            finance_migration_database_url=base_url,
            finance_provisioning_database_url=base_url,
        )
        router = CompanyDatabaseRouter(routing_settings)
        context = ExecutionContext(
            org_id=uuid.uuid4(),
            owner_account_id=owner_account_id,
            owner_session_id=owner_session_id,
            owner_credential_version=1,
            executor_kind=ExecutorKind.AI_AGENT,
            executor_name="multi-company-test",
            executor_version="1.0.0",
            request_correlation_id=uuid.uuid4(),
            catalog_instance_id=catalog_id,
        )
        create_requests = [
            CreateCompanyRequest(
                idempotency_key=f"create-company-{index + 1}",
                name=f"隔离公司 {index + 1}",
                taxpayer_identification_number=taxpayer_id,
                effective_from=date(2026, 9, 1),
                filing_cycle="quarterly",
                urban_maintenance_rate=Decimal("0.07"),
                confirmation_note="多公司 PostgreSQL 隔离测试",
                make_primary=index == 1,
            )
            for index, taxpayer_id in enumerate(
                ("91330106MA1234567T", "91330106MA7654321P")
            )
        ]
        catalog_engine = create_engine(catalog_url)
        try:
            assert {
                "close_backup_location_versions",
                "accounting_period_close_backups",
            } <= set(inspect(catalog_engine).get_table_names())
            with catalog_engine.connect() as connection:
                assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                    "0004_company_backup_locations"
                )
            created: list[dict[str, object]] = []
            for request in create_requests:
                with Session(catalog_engine) as session, session.begin():
                    result = CompanyService(
                        session, context=context, database_router=router
                    ).create_company(request)
                    assert result["status"] == "created", result
                    created.append(result)
            with Session(catalog_engine) as session, session.begin():
                replay = CompanyService(
                    session, context=context, database_router=router
                ).create_company(create_requests[0])
                assert replay["status"] == "created"
                assert replay["idempotent_replay"] is True
            with Session(catalog_engine) as session, session.begin():
                duplicate_taxpayer = CompanyService(
                    session, context=context, database_router=router
                ).create_company(
                    create_requests[1].model_copy(
                        update={
                            "idempotency_key": "create-duplicate-taxpayer",
                            "taxpayer_identification_number": (
                                create_requests[0].taxpayer_identification_number
                            ),
                        }
                    )
                )
                assert duplicate_taxpayer["errors"] == [
                    "TAXPAYER_IDENTIFICATION_NUMBER_ALREADY_EXISTS"
                ]

            org_ids = [uuid.UUID(str(result["company"]["org_id"])) for result in created]  # type: ignore[index]
            with Session(catalog_engine) as session:
                registries = [session.get(CompanyRegistry, org_id) for org_id in org_ids]
                assert all(registry is not None for registry in registries)
                assert [registry.is_primary for registry in registries] == [False, True]  # type: ignore[union-attr]
                company_urls = [
                    router.company_url(registry.database_name).render_as_string(  # type: ignore[union-attr]
                        hide_password=False
                    )
                    for registry in registries
                ]

            shared_event_id = uuid.uuid4()
            profile_evidence_ids: list[uuid.UUID] = []
            for index, company_url in enumerate(company_urls):
                _check_business_schema(company_url)
                engine = create_engine(company_url)
                try:
                    assert not (
                        set(inspect(engine).get_table_names())
                        & {
                            "owner_accounts",
                            "owner_sessions",
                            "owner_recovery_codes",
                            "identity_audit_events",
                        }
                    )
                    with Session(engine) as session, session.begin():
                        organization = session.get(Organization, org_ids[index])
                        assert organization is not None
                        attributed_context = ExecutionContext(
                            org_id=organization.id,
                            owner_account_id=owner_account_id,
                            owner_session_id=owner_session_id,
                            owner_credential_version=1,
                            executor_kind=ExecutorKind.AI_AGENT,
                            executor_name="multi-company-test",
                            executor_version="1.0.0",
                            request_correlation_id=uuid.uuid4(),
                            catalog_instance_id=catalog_id,
                        )
                        with persist_execution_attribution(
                            session,
                            context=attributed_context,
                            tool_name="finance_generate_accounting_period",
                        ):
                            evidence = Evidence(
                                org_id=organization.id,
                                sha256=uuid.uuid5(
                                    organization.id, "period-evidence"
                                ).hex
                                * 2,
                                original_name="period-evidence.txt",
                                media_type="text/plain",
                                source="multi-company-test",
                                size_bytes=1,
                                storage_path=f"tests/{organization.id}/period.txt",
                                metadata_json={},
                            )
                            session.add(evidence)
                            session.flush()
                            profile_evidence_ids.append(evidence.id)
                            generated = AccountingPeriodService(
                                session, current_date=date.max
                            ).generate_accounting_period(
                                GenerateAccountingPeriodRequest(
                                    org_id=organization.id,
                                    period_month="2026-08",
                                    idempotency_key="same-period-key",
                                    confirmation_note="隔离测试期间",
                                    evidence_references=[evidence.id],
                                )
                            )
                            assert generated.status == "posted"
                        with persist_execution_attribution(
                            session,
                            context=replace(
                                attributed_context,
                                request_correlation_id=uuid.uuid4(),
                            ),
                            tool_name="finance_record_event",
                        ):
                            session.add(
                                BusinessEvent(
                                    id=shared_event_id,
                                    org_id=organization.id,
                                    idempotency_key="same-business-key",
                                    request_payload_hash="a" * 64,
                                    event_type="expense_payable",
                                    status="draft",
                                    description=f"company-{index + 1}",
                                    facts={},
                                    business_date=date(2026, 8, 1),
                                    posting_date=date(2026, 8, 1),
                                    rule_trace=[],
                                )
                            )
                finally:
                    engine.dispose()

            from ai_accounting import dashboard_server

            monkeypatch.setattr(dashboard_server, "get_settings", lambda: routing_settings)
            monkeypatch.setattr(dashboard_server, "company_router", router)
            dashboard_context = load_multi_company_dashboard_context(
                catalog_engine,
                query={"org_id": [str(org_ids[1])]},
                fixed_org_id=None,
            )
            assert dashboard_context["schema_version"] == 2
            assert dashboard_context["current_company"]["org_id"] == str(org_ids[1])
            assert {item["org_id"] for item in dashboard_context["companies"]} == {
                str(item) for item in org_ids
            }
            assert dashboard_context["companies"][0]["org_id"] == str(org_ids[1])
            default_dashboard_context = load_multi_company_dashboard_context(
                catalog_engine,
                query={},
                fixed_org_id=None,
            )
            assert default_dashboard_context["current_company"]["org_id"] == str(
                org_ids[1]
            )
            fixed_dashboard_context = load_multi_company_dashboard_context(
                catalog_engine,
                query={"org_id": [str(org_ids[0])]},
                fixed_org_id=org_ids[0],
            )
            assert [
                item["org_id"] for item in fixed_dashboard_context["companies"]
            ] == [str(org_ids[0])]

            with Session(catalog_engine) as catalog_session:
                assert catalog_session.get(CatalogMetadata, 1).catalog_instance_id == catalog_id
                registries = [
                    router.resolve(catalog_session, org_id, for_write=False)
                    for org_id in org_ids
                ]
                for index, registry in enumerate(registries):
                    with Session(router.engine_for(registry)) as business_session:
                        event = business_session.scalar(
                            select(BusinessEvent).where(
                                BusinessEvent.id == shared_event_id,
                                BusinessEvent.idempotency_key == "same-business-key",
                            )
                        )
                        assert event is not None
                        assert event.org_id == org_ids[index]
                        assert event.description == f"company-{index + 1}"
                lifecycle_context = replace(
                    context, request_correlation_id=uuid.uuid4()
                )
                company_service = CompanyService(
                    catalog_session, context=lifecycle_context, database_router=router
                )
                profile_preview_request = PreviewCompanyProfileChangeRequest(
                    org_id=org_ids[0],
                    name="隔离公司 1（新名称）",
                    taxpayer_identification_number="91330106MA1234567T",
                    effective_from=date(2026, 10, 1),
                    filing_cycle="quarterly",
                    urban_maintenance_rate=Decimal("0.05"),
                    confirmation_note="隔离测试资料变更",
                    evidence_references=[profile_evidence_ids[0]],
                )
                profile_preview = company_service.preview_profile_change(
                    profile_preview_request
                )
                rejected_profile = company_service.confirm_profile_change(
                    ConfirmCompanyProfileChangeRequest(
                        **profile_preview_request.model_dump(),
                        idempotency_key="change-company-1-profile-bad-hash",
                        calculation_hash="0" * 64,
                    )
                )
                assert rejected_profile["errors"] == ["CALCULATION_HASH_MISMATCH"]
                assert router.resolve(
                    catalog_session, org_ids[0], for_write=True
                ).status == "active"
                profile_confirm_request = ConfirmCompanyProfileChangeRequest(
                    **profile_preview_request.model_dump(),
                    idempotency_key="change-company-1-profile",
                    calculation_hash=profile_preview["calculation_hash"],
                )
                original_profile_preview = company_service._profile_preview_facts
                preview_calls = 0

                def fail_once_during_business_write(
                    business_session: Session,
                    change_request: PreviewCompanyProfileChangeRequest
                    | ConfirmCompanyProfileChangeRequest,
                ) -> dict[str, object]:
                    nonlocal preview_calls
                    preview_calls += 1
                    if preview_calls == 2:
                        raise CompanyLifecycleError("PROFILE_CHANGE_SIMULATED_FAILURE")
                    return original_profile_preview(business_session, change_request)

                monkeypatch.setattr(
                    company_service,
                    "_profile_preview_facts",
                    fail_once_during_business_write,
                )
                failed_profile = company_service.confirm_profile_change(
                    profile_confirm_request
                )
                assert failed_profile["errors"] == ["PROFILE_CHANGE_SIMULATED_FAILURE"]
                assert catalog_session.get(CompanyRegistry, org_ids[0]).status == (
                    "attention_required"
                )
                monkeypatch.setattr(
                    company_service,
                    "_profile_preview_facts",
                    original_profile_preview,
                )
                profile_confirmed = company_service.confirm_profile_change(
                    profile_confirm_request
                )
                assert profile_confirmed["status"] == "confirmed", profile_confirmed
                assert profile_confirmed["company"]["urban_maintenance_rate"] == "0.05"
                profile_engine = create_engine(company_urls[0])
                try:
                    with Session(profile_engine) as business_session:
                        assert profile_as_of(
                            business_session,
                            org_id=org_ids[0],
                            as_of=date(2026, 9, 30),
                        ).urban_maintenance_rate == Decimal("0.07000")
                        assert profile_as_of(
                            business_session,
                            org_id=org_ids[0],
                            as_of=date(2026, 10, 1),
                        ).urban_maintenance_rate == Decimal("0.05000")
                        with pytest.raises(
                            ValueError, match="ORGANIZATION_PROFILE_NOT_EFFECTIVE"
                        ):
                            profile_as_of(
                                business_session,
                                org_id=org_ids[0],
                                as_of=date(2026, 8, 31),
                            )
                    with pytest.raises(DBAPIError):
                        with profile_engine.begin() as connection:
                            connection.execute(
                                text(
                                    "UPDATE organization_profile_versions "
                                    "SET name = '非法改写' WHERE org_id = :org_id"
                                ),
                                {"org_id": org_ids[0]},
                            )
                    with pytest.raises(DBAPIError):
                        with profile_engine.begin() as connection:
                            connection.execute(
                                text(
                                    "UPDATE organizations SET name = '绕过资料版本' "
                                    "WHERE id = :org_id"
                                ),
                                {"org_id": org_ids[0]},
                            )
                finally:
                    profile_engine.dispose()
                profile_replay = company_service.confirm_profile_change(
                    profile_confirm_request
                )
                assert profile_replay["idempotent_replay"] is True
                preview_request = PreviewCompanyStatusChangeRequest(
                    org_id=org_ids[0],
                    target_status="archived",
                    confirmation_note="隔离测试归档",
                )
                preview = company_service.preview_status_change(preview_request)
                confirmed = company_service.confirm_status_change(
                    ConfirmCompanyStatusChangeRequest(
                        **preview_request.model_dump(),
                        idempotency_key="archive-company-1",
                        calculation_hash=preview["calculation_hash"],
                    )
                )
                assert confirmed["status"] == "confirmed"
                with pytest.raises(CompanyRoutingError) as caught:
                    router.resolve(catalog_session, org_ids[0], for_write=True)
                assert caught.value.code == "COMPANY_NOT_ACTIVE"
                assert (
                    router.resolve(catalog_session, org_ids[0], for_write=False).status
                    == "archived"
                )
                restore_preview_request = PreviewCompanyStatusChangeRequest(
                    org_id=org_ids[0],
                    target_status="active",
                    confirmation_note="隔离测试恢复",
                )
                restore_preview = company_service.preview_status_change(
                    restore_preview_request
                )
                restored = company_service.confirm_status_change(
                    ConfirmCompanyStatusChangeRequest(
                        **restore_preview_request.model_dump(),
                        idempotency_key="restore-company-1",
                        calculation_hash=restore_preview["calculation_hash"],
                    )
                )
                assert restored["status"] == "confirmed"
                assert router.resolve(
                    catalog_session, org_ids[0], for_write=True
                ).status == "active"
        finally:
            router.dispose()
            catalog_engine.dispose()


def test_single_database_cutover_copies_identity_and_preserves_business_history() -> None:
    with PostgresContainer(POSTGRES_IMAGE, driver="psycopg") as postgres:
        base_url = postgres.get_connection_url(driver="psycopg")
        source_database = "finance"
        catalog_database = "finance_catalog"
        admin = create_engine(base_url, isolation_level="AUTOCOMMIT")
        try:
            with admin.connect() as connection:
                connection.exec_driver_sql(f'CREATE DATABASE "{source_database}"')
                connection.exec_driver_sql(f'CREATE DATABASE "{catalog_database}"')
        finally:
            admin.dispose()

        source_url = _database_url(base_url, source_database)
        catalog_url = _database_url(base_url, catalog_database)
        source_config = Config("alembic.ini")
        source_config.set_main_option("sqlalchemy.url", source_url.replace("%", "%%"))
        source_config.attributes["database_url_override"] = source_url
        command.upgrade(source_config, "0001_formal_baseline")
        source_engine = create_engine(source_url)
        org_id = uuid.uuid4()
        try:
            with Session(source_engine) as session:
                organization = seed_organization(
                    session,
                    org_id=org_id,
                    name="迁移保留公司",
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                )
                session.add(
                    BusinessEvent(
                        id=uuid.uuid4(),
                        org_id=organization.id,
                        idempotency_key="migration-preserved-event",
                        request_payload_hash="d" * 64,
                        event_type="expense_payable",
                        status="draft",
                        description="迁移前历史事件",
                        facts={},
                        business_date=date(2026, 1, 1),
                        posting_date=date(2026, 1, 1),
                        rule_trace=[],
                    )
                )
                session.commit()
                identity = IdentityService(session)
                identity.provision_owner(
                    OwnerProvisionRequest(
                        org_id=org_id,
                        login_name="migration-owner",
                        password=SecretStr("Migration-Owner-2026!"),
                    )
                )
                session.commit()
                login = identity.authenticate(
                    OwnerLoginRequest(
                        login_name="migration-owner",
                        password=SecretStr("Migration-Owner-2026!"),
                    )
                )
                session.commit()

            precheck = _source_precheck(source_engine)
            assert precheck["counts"]["business_events"] == 1  # type: ignore[index]
            database_identity = uuid.uuid5(org_id, "finance-company-database")
            catalog_id = uuid.uuid4()
            _upgrade_catalog(catalog_url, catalog_id)
            catalog_engine = create_engine(catalog_url)
            try:
                _copy_catalog_identity(
                    source_engine=source_engine,
                    catalog_engine=catalog_engine,
                    org_id=org_id,
                    database_identity=database_identity,
                    precheck=precheck,
                    backup_manifest_sha256="c" * 64,
                )
                _upgrade_existing_business(
                    make_url(source_url),
                    org_id=org_id,
                    database_identity=database_identity,
                    catalog_id=catalog_id,
                )
                _verify_source_after_cutover(
                    source_engine, precheck, database_identity
                )
                assert not (
                    set(inspect(source_engine).get_table_names())
                    & {
                        "owner_accounts",
                        "owner_sessions",
                        "owner_recovery_codes",
                        "identity_audit_events",
                    }
                )
                with Session(source_engine) as session:
                    assert session.scalar(select(BusinessEvent)) is not None
                    assert session.get(OrganizationDatabaseMetadata, 1) is not None
                    assert session.scalar(select(OrganizationProfileVersion)) is not None
                with Session(catalog_engine) as session:
                    assert session.scalar(select(OwnerAccount)) is not None
                    assert session.scalar(select(OwnerSession)) is not None
                    registry = session.get(CompanyRegistry, org_id)
                    assert registry is not None
                    assert registry.database_name == "finance"
                    assert registry.status == "provisioning"
                    session.info["catalog_mode"] = True
                    migrated_context = IdentityService(session).authorize_execution(
                        session_token=login.session_token.get_secret_value(),
                        executor=ExecutorIdentity(
                            kind=ExecutorKind.SYSTEM_JOB,
                            executor_name="migration-token-test",
                            executor_version="1.0.0",
                        ),
                        request_correlation_id=uuid.uuid4(),
                        expected_org_id=org_id,
                    )
                    assert migrated_context.owner_account_id == login.owner_account_id
                    assert migrated_context.catalog_instance_id == catalog_id
            finally:
                catalog_engine.dispose()
        finally:
            source_engine.dispose()
