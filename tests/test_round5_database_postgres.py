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
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

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

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture
def postgres_engine() -> Iterator[object]:
    with authenticated_business_database("round5_database") as database:
        yield database


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

    postgres_engine, org_id, evidence_id, authority = postgres_engine
    with Session(postgres_engine) as session:
        organization, _batch, _line, evidence, _event = confirmed_payroll(
            session, org_id, evidence_id, authority, key="r5-evidence-seal"
        )
        draft_evidence = Evidence(
            org_id=org_id,
            sha256="d" * 64,
            original_name="draft.txt",
            media_type="text/plain",
            source="r5-draft",
            size_bytes=1,
            storage_path="/r5/draft.txt",
            metadata_json={"draft": True},
        )
        with authority.attributed_call(session, tool_name="finance_register_evidence"):
            session.add(draft_evidence)
        session.commit()
        identifiers = {
            "evidence_id": evidence.id,
            "foreign_org_id": uuid.uuid4(),
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
            code=(
                "BUSINESS_EXECUTION_ATTRIBUTION_MISMATCH"
                if "SET org_id" in str(attack)
                else "R5_SEALED_EVIDENCE_CONTENT_IMMUTABLE"
            ),
        )


def test_r5_003_persistent_version_guards_serialize_direct_overlapping_inserts(
    postgres_engine: object,
) -> None:
    """A concurrent direct insert cannot write-skew any guarded version dimension."""

    postgres_engine, org_id, _evidence_id, authority = postgres_engine
    factory = make_session_factory(postgres_engine)
    with factory.begin() as session:
        with authority.attributed_call(session, tool_name="finance_register_employee"):
            employee = FinanceService(session).register_employee(
                RegisterEmployeeRequest(
                    org_id=org_id,
                    employee_code="R5-VERSION-GUARD",
                    name="版本锁员工",
                    employment_start_date=date(2025, 7, 1),
                    status="active",
                )
            )
        assert employee["status"] == "registered"
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
            guard_kind: str = kind,
        ) -> str:
            session = factory()
            try:
                synchronization.wait(timeout=10)
                tools = {
                    "profile": "finance_register_employee_payroll_profile_version",
                    "policy": "finance_register_payroll_policy_version",
                    "opening": "finance_register_payroll_opening_state",
                }
                with authority.attributed_call(session, tool_name=tools[guard_kind]):
                    session.add(create(variant))
                    session.flush()
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
