"""Direct PostgreSQL and migration regressions for R5 database contracts.

Every mutation in this module intentionally bypasses ``FinanceService``.  A
failure therefore demonstrates the deferred database closure, not a service
precondition or an MCP validation path.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine, inspect, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_round4_event_integrity_postgres import (
    _confirmed_payroll_with_evidence,
    _salary_payment_with_unsettled_statutory_sources,
)
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.database import make_session_factory
from ai_accounting.models import (
    BusinessEvent,
    Counterparty,
    Employee,
    EmployeePayrollProfileVersion,
    Evidence,
    OpenItem,
    PayrollBatch,
    PayrollOpeningState,
    PayrollPolicyVersion,
    PayrollVersionGuard,
    Settlement,
)
from ai_accounting.schemas import RegisterEmployeeRequest
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _alembic_config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[object]:
    """A clean current-head database for commit-boundary attacks."""

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = _alembic_config(database_url)
        command.upgrade(config, "head")
        command.check(config)
        engine = create_engine(database_url)
        try:
            yield engine
        finally:
            engine.dispose()


def _assert_commit_rejects(
    engine: object,
    statement: sa.TextClause,
    parameters: dict[str, object],
    *,
    code: str,
) -> None:
    with Session(engine) as session:
        with pytest.raises(DBAPIError, match=code):
            session.execute(statement, parameters)
            session.commit()
        session.rollback()


def test_r5_001_final_payroll_settlement_rejects_update_delete_and_forged_reversal(
    postgres_engine: object,
) -> None:
    """A final salary PEL loses neither its settlement nor its reversal audit edge."""

    with Session(postgres_engine) as session:
        identifiers = _salary_payment_with_unsettled_statutory_sources(
            session, key="r5-settlement-closure"
        )
        salary_event_id = session.scalar(
            select(OpenItem.source_event_id).where(
                OpenItem.org_id == identifiers["org_id"],
                OpenItem.id == identifiers["withheld_social_item_id"],
            )
        )
        assert salary_event_id is not None
        settlement_id = session.scalar(
            select(Settlement.id).where(
                Settlement.org_id == identifiers["org_id"],
                Settlement.payment_event_id == salary_event_id,
                Settlement.reversed.is_(False),
            )
        )
        assert settlement_id is not None
        other_event_id = session.scalar(
            select(PayrollBatch.business_event_id).where(
                PayrollBatch.id == identifiers["source_batch_id"],
                PayrollBatch.org_id == identifiers["org_id"],
            )
        )
        assert other_event_id is not None
        identifiers.update(
            {
                "salary_event_id": salary_event_id,
                "settlement_id": settlement_id,
                "other_event_id": other_event_id,
                "other_open_item_id": identifiers["withheld_housing_item_id"],
            }
        )
        session.commit()

    protected_attacks = (
        sa.text("UPDATE settlements SET amount_fen = amount_fen + 1 WHERE id = :settlement_id"),
        sa.text(
            "UPDATE settlements SET payment_event_id = :other_event_id WHERE id = :settlement_id"
        ),
        sa.text(
            "UPDATE settlements SET open_item_id = :other_open_item_id WHERE id = :settlement_id"
        ),
        sa.text("DELETE FROM settlements WHERE id = :settlement_id"),
    )
    for attack in protected_attacks:
        _assert_commit_rejects(
            postgres_engine,
            attack,
            identifiers,
            code="R5_FINAL_PAYROLL_SOURCE_SETTLEMENT_IMMUTABLE",
        )

    _assert_commit_rejects(
        postgres_engine,
        sa.text(
            "WITH removed AS ("
            "  DELETE FROM settlements WHERE id = :settlement_id "
            "  RETURNING org_id, open_item_id, payment_event_id, amount_fen, reversed"
            ") "
            "INSERT INTO settlements "
            "(id, org_id, open_item_id, payment_event_id, amount_fen, reversed) "
            "SELECT :replacement_id, org_id, open_item_id, payment_event_id, amount_fen, reversed "
            "FROM removed"
        ),
        {**identifiers, "replacement_id": uuid.uuid4()},
        code="R5_FINAL_PAYROLL_SOURCE_SETTLEMENT_IMMUTABLE",
    )

    _assert_commit_rejects(
        postgres_engine,
        sa.text(
            "UPDATE settlements SET reversed = TRUE, reversed_by_event_id = :salary_event_id "
            "WHERE id = :settlement_id"
        ),
        identifiers,
        code="R5_SETTLEMENT_REVERSAL_AUDIT_VIOLATION",
    )

    with Session(postgres_engine) as session:
        settlement = session.get(Settlement, settlement_id)
        assert settlement is not None
        assert settlement.reversed is False
        assert settlement.reversed_by_event_id is None


def test_r5_002_sealed_evidence_blocks_every_content_and_identity_mutation(
    postgres_engine: object,
) -> None:
    """Final event/batch references seal content, location, hash, metadata and owner."""

    with Session(postgres_engine) as session:
        organization, _batch, _line, evidence, _event = _confirmed_payroll_with_evidence(
            session, key="r5-evidence-seal"
        )
        foreign_organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R5 密封证据外部企业"
        )
        draft_evidence = Evidence(
            org_id=organization.id,
            sha256="d" * 64,
            original_name="draft.txt",
            media_type="text/plain",
            source="r5-draft",
            size_bytes=1,
            storage_path="/r5/draft.txt",
            metadata_json={"draft": True},
        )
        session.add(draft_evidence)
        session.commit()
        identifiers = {
            "evidence_id": evidence.id,
            "foreign_org_id": foreign_organization.id,
            "draft_evidence_id": draft_evidence.id,
        }

    # The guard is deliberately final-reference scoped: ordinary draft uploads
    # remain editable until they become accounting evidence.
    with Session(postgres_engine) as session:
        session.execute(
            sa.text(
                "UPDATE evidence SET metadata = CAST(:metadata AS json), "
                "storage_path = :storage_path WHERE id = :draft_evidence_id"
            ),
            {**identifiers, "metadata": '{"draft": false}', "storage_path": "/r5/moved.txt"},
        )
        session.commit()

    content_attacks = (
        (
            sa.text("UPDATE evidence SET sha256 = :sha256 WHERE id = :evidence_id"),
            {"sha256": "f" * 64},
        ),
        (
            sa.text("UPDATE evidence SET original_name = :name WHERE id = :evidence_id"),
            {"name": "replaced.txt"},
        ),
        (
            sa.text("UPDATE evidence SET media_type = :media_type WHERE id = :evidence_id"),
            {"media_type": "application/pdf"},
        ),
        (
            sa.text("UPDATE evidence SET source = :source WHERE id = :evidence_id"),
            {"source": "forged-source"},
        ),
        (
            sa.text("UPDATE evidence SET size_bytes = size_bytes + 1 WHERE id = :evidence_id"),
            {},
        ),
        (
            sa.text("UPDATE evidence SET storage_path = :storage_path WHERE id = :evidence_id"),
            {"storage_path": "/r5/replaced.bin"},
        ),
        (
            sa.text(
                "UPDATE evidence SET metadata = CAST(:metadata AS json) WHERE id = :evidence_id"
            ),
            {"metadata": '{"forged": true}'},
        ),
        (
            sa.text("UPDATE evidence SET org_id = :foreign_org_id WHERE id = :evidence_id"),
            {},
        ),
        (sa.text("DELETE FROM evidence WHERE id = :evidence_id"), {}),
    )
    for attack, extra_parameters in content_attacks:
        _assert_commit_rejects(
            postgres_engine,
            attack,
            {**identifiers, **extra_parameters},
            code="R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE",
        )


def test_r5_003_persistent_version_guards_serialize_direct_overlapping_inserts(
    postgres_engine: object,
) -> None:
    """A concurrent direct insert cannot write-skew any guarded version dimension."""

    factory = make_session_factory(postgres_engine)
    with factory.begin() as session:
        organization = seed_organization(
            session, accounting_period_control_enabled=False, name="R5 版本锁并发企业"
        )
        employee = FinanceService(session).register_employee(
            RegisterEmployeeRequest(
                org_id=organization.id,
                employee_code="R5-VERSION-GUARD",
                name="版本锁员工",
                employment_start_date=date(2025, 7, 1),
                status="active",
            )
        )
        assert employee["status"] == "registered"
        org_id = organization.id
        employee_id = uuid.UUID(employee["employee_id"])

    VersionFactory = Callable[[int], object]
    cases: tuple[tuple[str, str, VersionFactory], ...] = (
        (
            "profile",
            f"profile:{employee_id}",
            lambda variant: EmployeePayrollProfileVersion(
                org_id=org_id,
                employee_id=employee_id,
                effective_from=date(2025, 7, 1),
                effective_to=date(2026, 6, 30),
                expense_role="payroll_management_expense",
                social_insurance_base_fen=100 + variant,
                housing_fund_base_fen=100 + variant,
                resident_employee=True,
            ),
        ),
        (
            "policy",
            "policy:R5-GUARD-REGION",
            lambda variant: PayrollPolicyVersion(
                org_id=org_id,
                region="R5-GUARD-REGION",
                effective_from=date(2025, 7, 1),
                effective_to=date(2026, 6, 30),
                version=f"r5-guard-policy-{variant}",
                source_url="https://www.chinatax.gov.cn/",
                parameters={},
            ),
        ),
        (
            "opening",
            f"opening:{employee_id}:2026:9",
            lambda variant: PayrollOpeningState(
                org_id=org_id,
                employee_id=employee_id,
                tax_year=2026,
                through_month=9,
                cumulative_income_fen=variant,
            ),
        ),
    )
    codes = {
        "profile": "PAYROLL_PROFILE_VERSION_NON_ANCESTOR_OVERLAP",
        "policy": "PAYROLL_POLICY_VERSION_NON_ANCESTOR_OVERLAP",
        "opening": "PAYROLL_OPENING_STATE_NON_ANCESTOR_OVERLAP",
    }

    for kind, dimension_key, create_version in cases:
        barrier = Barrier(2)

        def insert_direct(
            variant: int,
            *,
            synchronization: Barrier = barrier,
            create: VersionFactory = create_version,
        ) -> str:
            session = factory()
            try:
                synchronization.wait(timeout=10)
                session.add(create(variant))
                session.commit()
                return "posted"
            except DBAPIError as exc:
                session.rollback()
                return str(exc)
            finally:
                session.close()

        with ThreadPoolExecutor(max_workers=2) as executor:
            outcomes = list(executor.map(insert_direct, (1, 2)))
        assert outcomes.count("posted") == 1
        rejected = next(outcome for outcome in outcomes if outcome != "posted")
        assert codes[kind] in rejected

        with Session(postgres_engine) as session:
            guards = session.scalars(
                select(PayrollVersionGuard).where(
                    PayrollVersionGuard.org_id == org_id,
                    PayrollVersionGuard.guard_kind == kind,
                    PayrollVersionGuard.dimension_key == dimension_key,
                )
            ).all()
            assert len(guards) == 1


def test_r5_migration_preflights_0005_pollution_without_advancing_revision() -> None:
    """Each R5 legacy preflight fails before DDL and leaves the database at 0005."""

    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = _alembic_config(database_url)
        command.upgrade(config, "0005_payroll_round4_integrity")
        engine = create_engine(database_url)
        try:
            organization_id = uuid.uuid4()
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        "INSERT INTO organizations "
                        "(id, name, taxpayer_type, filing_cycle, jurisdiction, "
                        "urban_maintenance_rate, accounting_standard, created_at) VALUES "
                        "(:id, 'R5 旧版污染预检企业', 'small_scale', 'quarterly', 'CN', "
                        "0.07, 'small_enterprise', now())"
                    ),
                    {"id": organization_id},
                )
            with Session(engine) as session:
                evidence = Evidence(
                    org_id=organization_id,
                    sha256="too-short",
                    original_name="legacy.bin",
                    media_type="application/octet-stream",
                    source="legacy",
                    size_bytes=1,
                    storage_path="/legacy.bin",
                    metadata_json={},
                )
                counterparty = Counterparty(
                    org_id=organization_id, kind="supplier", name="R5 旧版供应商"
                )
                payment = BusinessEvent(
                    org_id=organization_id,
                    idempotency_key="r5-legacy-settlement-payment",
                    event_type="expense_cash",
                    status="draft",
                    description="R5 旧版结算污染",
                    facts={},
                    business_date=date(2025, 7, 1),
                    posting_date=date(2025, 7, 1),
                    rule_trace=[],
                )
                session.add_all([evidence, counterparty, payment])
                session.flush()
                open_item = OpenItem(
                    org_id=organization_id,
                    counterparty_id=counterparty.id,
                    source_event_id=payment.id,
                    item_type="payable",
                    original_amount_fen=100,
                    settled_amount_fen=0,
                    status="open",
                )
                session.add(open_item)
                session.commit()
                identifiers = {
                    "org_id": organization_id,
                    "evidence_id": evidence.id,
                    "payment_id": payment.id,
                    "open_item_id": open_item.id,
                }

            with pytest.raises(
                RuntimeError, match="R5_EVIDENCE_ORGANIZATION_OR_HASH_PRECHECK_FAILED"
            ):
                command.upgrade(config, "0006_payroll_round5_integrity")
            with engine.connect() as connection:
                revision = connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                assert revision == "0005_payroll_round4_integrity"
                assert "reversed_by_event_id" not in {
                    column["name"] for column in inspect(engine).get_columns("settlements")
                }

            with engine.begin() as connection:
                connection.execute(
                    sa.text("UPDATE evidence SET sha256 = :sha256 WHERE id = :evidence_id"),
                    {"sha256": "e" * 64, "evidence_id": identifiers["evidence_id"]},
                )
                connection.execute(
                    sa.text(
                        "INSERT INTO settlements "
                        "(id, org_id, open_item_id, payment_event_id, amount_fen, reversed) "
                        "VALUES (:id, :org_id, :open_item_id, :payment_id, 10, TRUE)"
                    ),
                    {"id": uuid.uuid4(), **identifiers},
                )
            with pytest.raises(RuntimeError, match="R5_SETTLEMENT_REVERSAL_PRECHECK_FAILED"):
                command.upgrade(config, "0006_payroll_round5_integrity")
            with engine.begin() as connection:
                connection.execute(
                    sa.text("DELETE FROM settlements WHERE org_id = :org_id"), identifiers
                )

            with Session(engine) as session:
                employee_counterparty = Counterparty(
                    org_id=identifiers["org_id"], kind="employee", name="R5 旧版版本员工"
                )
                session.add(employee_counterparty)
                session.flush()
                employee = Employee(
                    org_id=identifiers["org_id"],
                    counterparty_id=employee_counterparty.id,
                    employee_code="R5-LEGACY-VERSION",
                    name="R5 旧版版本员工",
                    employment_start_date=date(2025, 7, 1),
                    status="active",
                )
                session.add(employee)
                session.commit()
                identifiers["employee_id"] = employee.id

            with engine.begin() as connection:
                connection.execute(
                    sa.text("ALTER TABLE employee_payroll_profile_versions DISABLE TRIGGER ALL")
                )
                for effective_from, effective_to, base in (
                    (date(2025, 7, 1), date(2025, 12, 31), 100),
                    (date(2025, 11, 1), date(2026, 6, 30), 101),
                ):
                    connection.execute(
                        sa.text(
                            "INSERT INTO employee_payroll_profile_versions "
                            "(id, org_id, employee_id, supersedes_id, effective_from, "
                            "effective_to, "
                            "expense_role, social_insurance_base_fen, housing_fund_base_fen, "
                            "resident_employee, created_at) "
                            "VALUES (:id, :org_id, :employee_id, NULL, :effective_from, "
                            ":effective_to, "
                            "'payroll_management_expense', :base, :base, TRUE, now())"
                        ),
                        {
                            "id": uuid.uuid4(),
                            "org_id": identifiers["org_id"],
                            "employee_id": identifiers["employee_id"],
                            "effective_from": effective_from,
                            "effective_to": effective_to,
                            "base": base,
                        },
                    )
                connection.execute(
                    sa.text("ALTER TABLE employee_payroll_profile_versions ENABLE TRIGGER ALL")
                )
            with pytest.raises(RuntimeError, match="R5_VERSION_LINEAGE_PRECHECK_FAILED"):
                command.upgrade(config, "0006_payroll_round5_integrity")
            with engine.connect() as connection:
                revision = connection.execute(
                    sa.text("SELECT version_num FROM alembic_version")
                ).scalar_one()
                assert revision == "0005_payroll_round4_integrity"
        finally:
            engine.dispose()
