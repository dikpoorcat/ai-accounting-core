"""Direct PostgreSQL regressions for current database contracts.

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
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_round4_event_integrity_postgres import (
    _confirmed_payroll_with_evidence,
)
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.database import make_session_factory
from ai_accounting.models import (
    EmployeePayrollProfileVersion,
    Evidence,
    PayrollOpeningState,
    PayrollPolicyVersion,
    PayrollVersionGuard,
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

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
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


def test_r5_002_sealed_evidence_blocks_every_content_and_identity_mutation(
    postgres_engine: object,
) -> None:
    """Final event/batch references seal content, location, hash, metadata and owner."""

    with Session(postgres_engine) as session:
        organization, _batch, _line, evidence, _event = _confirmed_payroll_with_evidence(
            session, key="r5-evidence-seal"
        )
        foreign_organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R5 密封证据外部企业",
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
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R5 版本锁并发企业",
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
