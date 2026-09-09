"""Real catalog authentication for isolated PostgreSQL business-baseline tests."""

import hashlib
import os
import uuid
from contextlib import contextmanager
from datetime import date

from alembic.config import Config
from conftest import AuthenticatedOwnerAuthority
from pydantic import SecretStr
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from ai_accounting.identity import ExecutorIdentity, ExecutorKind
from ai_accounting.identity_schemas import OwnerLoginRequest, OwnerProvisionRequest
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import CatalogMetadata, CompanyRegistry, OrganizationDatabaseMetadata
from alembic import command


@contextmanager
def catalog_owner_authority(business_session, organization, *, registry_database_name=None):
    """Keep an independent, genuinely authenticated catalog alive for the test."""
    url = business_session.get_bind().url
    if url.host not in {"localhost", "127.0.0.1", "::1"} or url.database in {
        "finance",
        "finance_catalog",
    }:
        raise AssertionError("catalog helper requires an isolated local test database")
    database = "component_catalog_" + uuid.uuid4().hex
    admin = create_engine(url.set(database="postgres"), isolation_level="AUTOCOMMIT")
    catalog = None
    try:
        with admin.connect() as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        catalog_url = url.set(database=database)
        config = Config("catalog_alembic.ini")
        config.set_main_option("sqlalchemy.url", catalog_url.render_as_string(hide_password=False))
        config.attributes["database_url_override"] = catalog_url.render_as_string(
            hide_password=False
        )
        command.upgrade(config, "head")
        catalog = create_engine(catalog_url)
        with Session(catalog, info={"catalog_mode": True}) as session:
            metadata = session.get(CatalogMetadata, 1)
            if metadata is None:
                metadata = CatalogMetadata(singleton_key=1, catalog_instance_id=uuid.uuid4())
                session.add(metadata)
            binding = business_session.get(OrganizationDatabaseMetadata, 1)
            if binding is None:
                binding = OrganizationDatabaseMetadata(
                    singleton_key=1,
                    org_id=organization.id,
                    database_identity=uuid.uuid4(),
                    current_catalog_instance_id=metadata.catalog_instance_id,
                    owner_approval_required=True,
                )
                business_session.add(binding)
                business_session.commit()
            elif binding.current_catalog_instance_id != metadata.catalog_instance_id:
                raise AssertionError("business database already bound to another catalog")
            session.add(
                CompanyRegistry(
                    org_id=organization.id,
                    database_name=(
                        registry_database_name
                        or (
                            url.database
                            if url.database.startswith("finance_company_")
                            else "finance"
                        )
                    ),
                    database_identity=binding.database_identity,
                    display_name=organization.name,
                    taxpayer_identification_number=organization.taxpayer_identification_number,
                    status="active",
                    is_primary=True,
                    filing_cycle=organization.filing_cycle,
                    profile_effective_from=date(2026, 1, 1),
                    urban_maintenance_rate=organization.urban_maintenance_rate,
                )
            )
            session.commit()
            password = SecretStr("Component-Catalog-Owner-2026!")
            identity = IdentityService(session)
            identity.provision_owner(
                OwnerProvisionRequest(
                    org_id=organization.id,
                    login_name="component-test-owner",
                    password=password,
                )
            )
            session.commit()
            login = identity.authenticate(
                OwnerLoginRequest(
                    login_name="component-test-owner",
                    password=password,
                )
            )
            context = identity.authorize_execution(
                session_token=login.session_token.get_secret_value(),
                executor=ExecutorIdentity(
                    kind=ExecutorKind.AI_AGENT,
                    executor_name="component-postgres-test",
                    executor_version="v1",
                ),
                request_correlation_id=uuid.uuid4(),
                expected_org_id=organization.id,
            )
            session.commit()
            yield AuthenticatedOwnerAuthority(
                context,
                catalog_url=catalog_url.render_as_string(hide_password=False),
                session_token=login.session_token,
            )
    finally:
        if catalog is not None:
            catalog.dispose()
        with admin.connect() as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        admin.dispose()


@contextmanager
def isolated_postgres_url(prefix="component_test"):
    """Use an explicitly configured test server, or a disposable PostgreSQL 17 container."""
    from sqlalchemy.engine import make_url
    from testcontainers.community.postgres import PostgresContainer

    configured = os.environ.get("FINANCE_TEST_POSTGRES_ADMIN_URL")
    if configured is None:
        with PostgresContainer(
            "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
            driver="psycopg",
        ) as postgres:
            admin_url = make_url(postgres.get_connection_url(driver="psycopg")).set(
                database="postgres"
            )
            with _isolated_database_on_server(admin_url, prefix) as database_url:
                yield database_url
        return
    url = make_url(configured)
    with _isolated_database_on_server(url, prefix) as database_url:
        yield database_url


@contextmanager
def _isolated_database_on_server(url, prefix):
    if url.database != "postgres" or not prefix.replace("_", "").isalnum():
        raise AssertionError("isolated tests require an admin URL and a safe database prefix")
    database = prefix + "_" + uuid.uuid4().hex
    admin = create_engine(url)
    try:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'CREATE DATABASE "{database}"'))
        yield url.set(database=database).render_as_string(hide_password=False)
    finally:
        with admin.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
            connection.execute(text(f'DROP DATABASE IF EXISTS "{database}" WITH (FORCE)'))
        admin.dispose()


@contextmanager
def authenticated_business_database(
    prefix="business_invariant", *, name="组件约束测试", revision="head"
):
    """Create an isolated business/catalog pair with explicit evidence and authority."""
    from ai_accounting.coa import seed_organization
    from ai_accounting.models import Evidence

    with isolated_postgres_url(prefix) as url:
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = url
        command.upgrade(config, revision)
        engine = create_engine(url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    name=name,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                )
                session.commit()
                org_id = organization.id
                with catalog_owner_authority(session, organization) as authority:
                    with authority.attributed_call(session, tool_name="finance_register_evidence"):
                        evidence = Evidence(
                            org_id=org_id,
                            sha256=hashlib.sha256(prefix.encode()).hexdigest(),
                            original_name=f"{prefix}.txt",
                            source="test",
                            size_bytes=len(prefix.encode()),
                            storage_path=f"tests/{prefix}.txt",
                        )
                        session.add(evidence)
                        session.flush()
                        evidence_id = evidence.id
                    session.commit()
                    yield engine, org_id, evidence_id, authority
        finally:
            engine.dispose()


def confirmed_payroll(session, org_id, evidence_id, authority, *, key="postgres-payroll"):
    """Post the standard March payroll through genuine attributed domain calls."""
    from test_payroll_service import payroll_parameters

    from ai_accounting.models import (
        BusinessEvent,
        Evidence,
        Organization,
        PayrollBatch,
        PayrollLine,
    )
    from ai_accounting.schemas import (
        ConfirmPayrollRequest,
        PreviewPayrollRequest,
        RegisterEmployeePayrollProfileVersionRequest,
        RegisterEmployeeRequest,
        RegisterPayrollPolicyVersionRequest,
    )
    from ai_accounting.service import FinanceService

    service = FinanceService(session)
    with authority.attributed_call(session, tool_name="finance_register_employee"):
        employee = service.register_employee(
            RegisterEmployeeRequest(
                org_id=org_id,
                employee_code=key,
                name="测试工资员工",
                employment_start_date=date(2026, 3, 1),
                tax_withholding_start_date=date(2026, 3, 1),
                status="active",
            )
        )
    assert employee["status"] == "registered", employee
    employee_id = uuid.UUID(employee["employee_id"])
    with authority.attributed_call(
        session, tool_name="finance_register_employee_payroll_profile_version"
    ):
        profile = service.register_employee_payroll_profile_version(
            RegisterEmployeePayrollProfileVersionRequest(
                org_id=org_id,
                employee_id=employee_id,
                effective_from=date(2026, 3, 1),
                expense_role="payroll_management_expense",
                social_insurance_base_fen=1_000_000,
                housing_fund_base_fen=1_000_000,
                resident_employee=True,
            )
        )
    assert profile["status"] == "registered", profile
    with authority.attributed_call(session, tool_name="finance_register_payroll_policy_version"):
        policy = service.register_payroll_policy_version(
            RegisterPayrollPolicyVersionRequest(
                org_id=org_id,
                region=key,
                effective_from=date(2026, 3, 1),
                version=key,
                source_url="https://www.chinatax.gov.cn/",
                parameters=payroll_parameters(),
            )
        )
    assert policy["status"] == "registered", policy
    with authority.attributed_call(session, tool_name="finance_preview_payroll"):
        preview = service.preview_payroll(
            PreviewPayrollRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": key + "-preview",
                    "batch_kind": "regular",
                    "payroll_period": "2026-03",
                    "posting_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "evidence_references": [evidence_id],
                    "employee_items": [
                        {
                            "employee_id": employee_id,
                            "tax_reported_salary_fen": 1_000_000,
                            "special_additional_deduction_fen": 0,
                            "other_legal_deduction_fen": 0,
                        }
                    ],
                }
            )
        )
    assert preview.status == "calculated", preview.errors
    with authority.attributed_call(session, tool_name="finance_confirm_payroll"):
        posted = service.confirm_payroll(
            ConfirmPayrollRequest(
                org_id=org_id,
                batch_id=preview.batch_id,
                calculation_hash=preview.calculation_hash,
                idempotency_key=key + "-confirm",
            )
        )
    assert posted.status == "posted", posted.errors
    return (
        session.get(Organization, org_id),
        session.get(PayrollBatch, preview.batch_id),
        session.query(PayrollLine).filter_by(payroll_batch_id=preview.batch_id).one(),
        session.get(Evidence, evidence_id),
        session.get(BusinessEvent, posted.event_id),
    )
