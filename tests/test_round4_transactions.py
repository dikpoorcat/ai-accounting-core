"""PostgreSQL concurrency and bank-mirror regressions for round four."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
from _postgres_helpers import authenticated_business_database
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.database import make_session_factory
from ai_accounting.models import (
    BusinessEvent,
    EmployeePayrollProfileVersion,
    PayrollOpeningState,
    PayrollPolicyVersion,
)
from ai_accounting.schemas import (
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture
def postgres_engine() -> Iterator[object]:
    with authenticated_business_database("round4_transactions") as database:
        yield database


def _expense_request(org_id: object, *, key: str, evidence_id: object) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "posting_date": "2026-04-05",
            "components": [
                {
                    "key": "expense",
                    "kind": "expense",
                    "business_date": "2026-04-05",
                    "payment_date": "2026-04-05",
                    "amount_fen": 100,
                    "expense_class": "general_expense",
                    "payment_basis": "supplier_credit",
                    "evidence_references": [evidence_id],
                    "metadata": {"counterparty": {"kind": "supplier", "name": "R4 测试供应商"}},
                }
            ],
        }
    )


def _reverse_request(org_id: object, event_id: object, *, key: str) -> ReverseEventRequest:
    return ReverseEventRequest(
        org_id=org_id,
        event_id=event_id,
        idempotency_key=key,
        reason="R4 并发冲正",
        posting_date=date(2026, 4, 6),
    )


def _post_two_expenses(
    session: Session,
    *,
    org_id: object,
    evidence_id: object,
    authority: object,
    key_prefix: str,
) -> tuple[object, list[object]]:
    event_ids: list[object] = []
    for index in (1, 2):
        with authority.attributed_call(session, tool_name="finance_record_event"):
            posted = FinanceService(session).record_event(
                _expense_request(
                    org_id,
                    key=f"{key_prefix}-expense-{index}",
                    evidence_id=evidence_id,
                )
            )
        assert posted.status == "posted", posted.errors
        assert posted.event_id is not None
        event_ids.append(posted.event_id)
    return org_id, event_ids


def test_r4_007_two_sources_with_one_idempotency_key_return_a_stable_mismatch(
    postgres_engine: object,
) -> None:
    """Separate row locks must not leak a unique-index exception to callers."""

    postgres_engine, org_id, evidence_id, authority = postgres_engine
    factory = make_session_factory(postgres_engine)
    with factory.begin() as session:
        org_id, event_ids = _post_two_expenses(
            session,
            org_id=org_id,
            evidence_id=evidence_id,
            authority=authority,
            key_prefix="r4-idempotency-race",
        )

    requests = [
        _reverse_request(org_id, event_id, key="r4-shared-reversal-key") for event_id in event_ids
    ]
    barrier = Barrier(2)

    def reverse(request: ReverseEventRequest) -> object:
        barrier.wait(timeout=10)
        with factory.begin() as session:
            with authority.attributed_call(session, tool_name="finance_reverse_event"):
                return FinanceService(session).reverse_event(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reverse, requests))

    assert {result.status for result in results} == {"posted", "rejected"}
    rejected = next(result for result in results if result.status == "rejected")
    assert rejected.errors == ["PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"]

    with Session(postgres_engine) as session:
        events = [session.get(BusinessEvent, event_id) for event_id in event_ids]
        assert {event.status for event in events if event is not None} == {"posted", "reversed"}


def test_r4_009_postgresql_version_lineage_rejects_nonancestor_overlap_at_commit(
    postgres_engine: object,
) -> None:
    """The database catches non-ancestor version overlap even without the service."""

    postgres_engine, org_id, _evidence_id, authority = postgres_engine
    with Session(postgres_engine) as session:
        with authority.attributed_call(session, tool_name="finance_register_employee"):
            employee = FinanceService(session).register_employee(
                RegisterEmployeeRequest(
                    org_id=org_id,
                    employee_code="R4-009-EMPLOYEE",
                    name="版本链员工",
                    employment_start_date=date(2025, 7, 1),
                    status="active",
                )
            )
        assert employee["status"] == "registered"
        employee_id = uuid.UUID(employee["employee_id"])

        profile_a = EmployeePayrollProfileVersion(
            org_id=org_id,
            employee_id=employee_id,
            effective_from=date(2025, 7, 1),
            effective_to=date(2025, 12, 31),
            expense_role="payroll_management_expense",
            social_insurance_base_fen=100,
            housing_fund_base_fen=100,
            resident_employee=True,
        )
        profile_b = EmployeePayrollProfileVersion(
            org_id=org_id,
            employee_id=employee_id,
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 6, 30),
            expense_role="payroll_management_expense",
            social_insurance_base_fen=100,
            housing_fund_base_fen=100,
            resident_employee=True,
        )
        policy_a = PayrollPolicyVersion(
            org_id=org_id,
            region="R4-009",
            effective_from=date(2025, 7, 1),
            effective_to=date(2025, 12, 31),
            version="r4-009-policy-a",
            source_url="https://www.chinatax.gov.cn/",
            parameters={},
        )
        policy_b = PayrollPolicyVersion(
            org_id=org_id,
            region="R4-009",
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 6, 30),
            version="r4-009-policy-b",
            source_url="https://www.chinatax.gov.cn/",
            parameters={},
        )
        opening_a = PayrollOpeningState(
            org_id=org_id,
            employee_id=employee_id,
            tax_year=2026,
            through_month=8,
        )
        for tool_name, rows in (
            ("finance_register_employee_payroll_profile_version", [profile_a, profile_b]),
            ("finance_register_payroll_policy_version", [policy_a, policy_b]),
            ("finance_register_payroll_opening_state", [opening_a]),
        ):
            with authority.attributed_call(session, tool_name=tool_name):
                session.add_all(rows)
        session.commit()
        profile_a_id = profile_a.id
        policy_a_id = policy_a.id
        opening_a_id = opening_a.id

    with Session(postgres_engine) as session:
        with pytest.raises(DBAPIError, match="PAYROLL_PROFILE_VERSION_NON_ANCESTOR_OVERLAP"):
            with authority.attributed_call(
                session, tool_name="finance_register_employee_payroll_profile_version"
            ):
                session.add(
                    EmployeePayrollProfileVersion(
                        org_id=org_id,
                        employee_id=employee_id,
                        supersedes_id=profile_a_id,
                        effective_from=date(2025, 7, 1),
                        effective_to=date(2026, 6, 30),
                        expense_role="payroll_management_expense",
                        social_insurance_base_fen=101,
                        housing_fund_base_fen=101,
                        resident_employee=True,
                    )
                )
                session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        with pytest.raises(DBAPIError, match="PAYROLL_POLICY_VERSION_NON_ANCESTOR_OVERLAP"):
            with authority.attributed_call(
                session, tool_name="finance_register_payroll_policy_version"
            ):
                session.add(
                    PayrollPolicyVersion(
                        org_id=org_id,
                        region="R4-009",
                        supersedes_id=policy_a_id,
                        effective_from=date(2025, 7, 1),
                        effective_to=date(2026, 6, 30),
                        version="r4-009-policy-a-successor",
                        source_url="https://www.chinatax.gov.cn/",
                        parameters={},
                    )
                )
                session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        with pytest.raises(DBAPIError, match="PAYROLL_OPENING_STATE_NON_ANCESTOR_OVERLAP"):
            with authority.attributed_call(
                session, tool_name="finance_register_payroll_opening_state"
            ):
                session.add(
                    PayrollOpeningState(
                        org_id=org_id,
                        employee_id=employee_id,
                        tax_year=2026,
                        through_month=8,
                    )
                )
                session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        assert session.get(PayrollOpeningState, opening_a_id) is not None


def test_r4_009_concurrent_successors_replay_or_reject_without_unique_errors(
    postgres_engine: object,
) -> None:
    """A predecessor lock makes concurrent successor writes deterministic."""

    postgres_engine, org_id, _evidence_id, authority = postgres_engine
    factory = make_session_factory(postgres_engine)
    with factory.begin() as session:
        with authority.attributed_call(session, tool_name="finance_register_employee"):
            employee = FinanceService(session).register_employee(
                RegisterEmployeeRequest(
                    org_id=org_id,
                    employee_code="R4-009-CONCURRENCY",
                    name="并发版本员工",
                    employment_start_date=date(2025, 7, 1),
                    status="active",
                )
            )
        assert employee["status"] == "registered"
        employee_id = uuid.UUID(employee["employee_id"])
        first_predecessor = EmployeePayrollProfileVersion(
            org_id=org_id,
            employee_id=employee_id,
            effective_from=date(2026, 3, 1),
            effective_to=date(2026, 6, 30),
            expense_role="payroll_management_expense",
            social_insurance_base_fen=100,
            housing_fund_base_fen=100,
            resident_employee=True,
        )
        second_predecessor = EmployeePayrollProfileVersion(
            org_id=org_id,
            employee_id=employee_id,
            effective_from=date(2026, 7, 1),
            effective_to=date(2027, 6, 30),
            expense_role="payroll_management_expense",
            social_insurance_base_fen=100,
            housing_fund_base_fen=100,
            resident_employee=True,
        )
        with authority.attributed_call(
            session, tool_name="finance_register_employee_payroll_profile_version"
        ):
            session.add_all([first_predecessor, second_predecessor])
        first_predecessor_id = first_predecessor.id
        second_predecessor_id = second_predecessor.id

    def request(predecessor_id: object, *, social_base: int) -> object:
        is_first_predecessor = predecessor_id == first_predecessor_id
        effective_from = date(2026, 3 if is_first_predecessor else 7, 1)
        effective_to = date(2026, 6, 30) if is_first_predecessor else date(2026, 12, 31)
        return RegisterEmployeePayrollProfileVersionRequest(
            org_id=org_id,
            employee_id=employee_id,
            effective_from=effective_from,
            effective_to=effective_to,
            expense_role="payroll_management_expense",
            social_insurance_base_fen=social_base,
            housing_fund_base_fen=social_base,
            resident_employee=True,
            supersedes_profile_version_id=predecessor_id,
        )

    replay_barrier = Barrier(2)

    def register_successor(profile_request: object, barrier: Barrier) -> object:
        barrier.wait(timeout=10)
        with factory.begin() as session:
            with authority.attributed_call(
                session, tool_name="finance_register_employee_payroll_profile_version"
            ):
                return FinanceService(session).register_employee_payroll_profile_version(
                    profile_request
                )

    same_request = request(first_predecessor_id, social_base=101)
    with ThreadPoolExecutor(max_workers=2) as executor:
        same_results = list(
            executor.map(
                lambda _: register_successor(same_request, replay_barrier),
                range(2),
            )
        )
    assert {result["status"] for result in same_results} == {"registered"}
    assert len({result["profile_version_id"] for result in same_results}) == 1
    assert sum(bool(result.get("idempotent_replay")) for result in same_results) == 1

    mismatch_barrier = Barrier(2)
    different_requests = [
        request(second_predecessor_id, social_base=101),
        request(second_predecessor_id, social_base=102),
    ]
    with ThreadPoolExecutor(max_workers=2) as executor:
        mismatch_results = list(
            executor.map(
                lambda profile_request: register_successor(profile_request, mismatch_barrier),
                different_requests,
            )
        )
    assert {result["status"] for result in mismatch_results} == {"registered", "rejected"}
    rejected = next(result for result in mismatch_results if result["status"] == "rejected")
    assert rejected["errors"] == ["PROFILE_VERSION_SUCCESSOR_EXISTS"]
