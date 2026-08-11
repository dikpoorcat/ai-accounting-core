"""PostgreSQL concurrency and bank-mirror regressions for round four."""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.database import make_session_factory
from ai_accounting.models import (
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    EmployeePayrollProfileVersion,
    PayrollOpeningState,
    PayrollPolicyVersion,
)
from ai_accounting.schemas import (
    RecordEventRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[object]:
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        command.check(config)
        engine = create_engine(database_url)
        try:
            yield engine
        finally:
            engine.dispose()


def _bank_transaction(org_id: object, *, amount_fen: int, key: str) -> BankTransaction:
    return BankTransaction(
        org_id=org_id,
        bank_account_code="1002",
        fingerprint=(key * 64)[:64],
        booking_date=date(2026, 4, 5),
        amount_fen=amount_fen,
        currency="CNY",
        memo=key,
        source_sha256=("round4-source-" + key * 64)[:64],
    )


def _expense_request(org_id: object, bank_id: object, *, key: str) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "event_type": "expense_cash",
            "business_dates": {
                "business_date": "2026-04-05",
                "payment_date": "2026-04-05",
                "posting_date": "2026-04-05",
            },
            "amounts": {
                "gross_amount_fen": 100,
                "expense_account_role": "general_expense",
            },
            "bank_transaction_references": [{"id": bank_id}],
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
    organization_name: str,
    key_prefix: str,
) -> tuple[object, list[object], list[object]]:
    organization = seed_organization(
        session, accounting_period_control_enabled=False, name=organization_name
    )
    bank_ids: list[object] = []
    event_ids: list[object] = []
    for index in (1, 2):
        bank = _bank_transaction(
            organization.id,
            amount_fen=-100,
            key=f"{key_prefix}-bank-{index}",
        )
        session.add(bank)
        session.flush()
        posted = FinanceService(session).record_event(
            _expense_request(
                organization.id,
                bank.id,
                key=f"{key_prefix}-expense-{index}",
            )
        )
        assert posted.status == "posted", posted.errors
        assert posted.event_id is not None
        bank_ids.append(bank.id)
        event_ids.append(posted.event_id)
    return organization.id, bank_ids, event_ids


def test_r4_007_two_sources_with_one_idempotency_key_return_a_stable_mismatch(
    postgres_engine: object,
) -> None:
    """Separate row locks must not leak a unique-index exception to callers."""

    factory = make_session_factory(postgres_engine)
    with factory.begin() as session:
        org_id, _, event_ids = _post_two_expenses(
            session,
            organization_name="R4-007 concurrent reversal organization",
            key_prefix="r4-idempotency-race",
        )

    requests = [
        _reverse_request(org_id, event_id, key="r4-shared-reversal-key") for event_id in event_ids
    ]
    barrier = Barrier(2)

    def reverse(request: ReverseEventRequest) -> object:
        barrier.wait(timeout=10)
        with factory.begin() as session:
            return FinanceService(session).reverse_event(request)

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(reverse, requests))

    assert {result.status for result in results} == {"posted", "rejected"}
    rejected = next(result for result in results if result.status == "rejected")
    assert rejected.errors == ["PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"]

    with Session(postgres_engine) as session:
        events = [session.get(BusinessEvent, event_id) for event_id in event_ids]
        assert {event.status for event in events if event is not None} == {"posted", "reversed"}


def test_r4_008_bank_history_and_current_pointer_reject_divergent_commit_attacks(
    postgres_engine: object,
) -> None:
    """Current bank mirrors cannot point at a historical or reversed edge."""

    factory = make_session_factory(postgres_engine)
    with factory.begin() as session:
        org_id, bank_ids, event_ids = _post_two_expenses(
            session,
            organization_name="R4-008 bank mirror organization",
            key_prefix="r4-bank-mirror",
        )
        original_event_id = event_ids[0]
        original_bank_id = bank_ids[0]
        reversal = FinanceService(session).reverse_event(
            _reverse_request(org_id, original_event_id, key="r4-bank-mirror-reversal")
        )
        assert reversal.status == "posted", reversal.errors

    with Session(postgres_engine) as session:
        original_match = session.scalar(
            select(BankTransactionMatch).where(
                BankTransactionMatch.org_id == org_id,
                BankTransactionMatch.bank_transaction_id == original_bank_id,
                BankTransactionMatch.event_id == original_event_id,
            )
        )
        assert original_match is not None and original_match.invalidated_by_event_id is not None
        session.execute(
            sa.update(BankTransaction)
            .where(BankTransaction.id == original_bank_id)
            .values(matched_event_id=original_event_id)
        )
        with pytest.raises(DBAPIError, match="BANK_TRANSACTION_POINTER_MIRROR_VIOLATION"):
            session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        second_bank = _bank_transaction(org_id, amount_fen=-100, key="r4-bank-reversed-edge")
        session.add(second_bank)
        session.flush()
        session.add(
            BankTransactionMatch(
                org_id=org_id,
                bank_transaction_id=second_bank.id,
                event_id=original_event_id,
            )
        )
        second_bank.matched_event_id = original_event_id
        with pytest.raises(DBAPIError, match="BANK_MATCH_CURRENT_EVENT_NOT_POSTED"):
            session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        original_bank = session.get(BankTransaction, original_bank_id)
        assert original_bank is not None and original_bank.matched_event_id is None


def test_r4_009_postgresql_version_lineage_rejects_nonancestor_overlap_at_commit(
    postgres_engine: object,
) -> None:
    """The database catches non-ancestor version overlap even without the service."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session,
            accounting_period_control_enabled=False,
            name="R4-009 version lineage organization",
        )
        org_id = organization.id
        employee = FinanceService(session).register_employee(
            RegisterEmployeeRequest(
                org_id=organization.id,
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
        session.add_all([profile_a, profile_b, policy_a, policy_b, opening_a])
        session.commit()
        profile_a_id = profile_a.id
        policy_a_id = policy_a.id
        opening_a_id = opening_a.id

    with Session(postgres_engine) as session:
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
        with pytest.raises(DBAPIError, match="PAYROLL_PROFILE_VERSION_NON_ANCESTOR_OVERLAP"):
            session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
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
        with pytest.raises(DBAPIError, match="PAYROLL_POLICY_VERSION_NON_ANCESTOR_OVERLAP"):
            session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        session.add(
            PayrollOpeningState(
                org_id=org_id,
                employee_id=employee_id,
                tax_year=2026,
                through_month=8,
            )
        )
        with pytest.raises(DBAPIError, match="PAYROLL_OPENING_STATE_NON_ANCESTOR_OVERLAP"):
            session.commit()
        session.rollback()

    with Session(postgres_engine) as session:
        assert session.get(PayrollOpeningState, opening_a_id) is not None


def test_r4_009_concurrent_successors_replay_or_reject_without_unique_errors(
    postgres_engine: object,
) -> None:
    """A predecessor lock makes concurrent successor writes deterministic."""

    factory = make_session_factory(postgres_engine)
    with factory.begin() as session:
        organization = seed_organization(
            session,
            accounting_period_control_enabled=False,
            name="R4-009 successor concurrency organization",
        )
        org_id = organization.id
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
        session.add_all([first_predecessor, second_predecessor])
        session.flush()
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
