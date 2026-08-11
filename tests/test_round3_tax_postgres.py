from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Event, Lock

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from test_payroll_service import payroll_parameters
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.database import make_session_factory
from ai_accounting.models import (
    BusinessEvent,
    Organization,
    PayrollBatch,
    PayrollTaxStateSlot,
    PayrollTaxYearGuard,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture(scope="module")
def postgres_engine() -> object:
    with PostgresContainer("postgres:17-alpine", driver="psycopg") as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        command.check(config)
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def _register_payroll_facts(
    session: Session, organization: Organization, employee_count: int = 1
) -> list[uuid.UUID]:
    service = FinanceService(session)
    employee_ids: list[uuid.UUID] = []
    for number in range(employee_count):
        employee = service.register_employee(
            RegisterEmployeeRequest(
                org_id=organization.id,
                employee_code=f"R3-TAX-{number + 1}",
                name=f"R3 税务员工 {number + 1}",
                employment_start_date=date(2026, 3, 1),
                status="active",
            )
        )
        employee_id = uuid.UUID(employee["employee_id"])
        employee_ids.append(employee_id)
        assert (
            service.register_employee_payroll_profile_version(
                RegisterEmployeePayrollProfileVersionRequest(
                    org_id=organization.id,
                    employee_id=employee_id,
                    effective_from=date(2026, 3, 1),
                    expense_role="payroll_management_expense",
                    social_insurance_base_fen=1_000_000,
                    housing_fund_base_fen=1_000_000,
                    resident_employee=True,
                )
            )["status"]
            == "registered"
        )
    assert (
        service.register_payroll_policy_version(
            RegisterPayrollPolicyVersionRequest.model_validate(
                {
                    "org_id": organization.id,
                    "region": "测试地区",
                    "effective_from": "2026-03-01",
                    "effective_to": "2026-07-31",
                    "version": "r3-tax-2026",
                    "source_url": (
                        "https://www.chinatax.gov.cn/chinatax/n810341/n810765/"
                        "n3359382/201812/c4182700/content.html"
                    ),
                    "parameters": payroll_parameters(),
                }
            )
        )["status"]
        == "registered"
    )
    return employee_ids


def _preview_regular(
    session: Session,
    org_id: uuid.UUID,
    employee_ids: list[uuid.UUID],
    *,
    payroll_month: int,
    key: str,
) -> object:
    payroll_date = date(2026, payroll_month, 5)
    return FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": key,
                "batch_kind": "regular",
                "payroll_period": f"2026-{payroll_month:02d}",
                "posting_date": payroll_date,
                "payment_date": payroll_date,
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "base_salary_fen": 1_000_000,
                        "performance_pay_fen": 0,
                        "taxable_allowance_fen": 0,
                        "tax_exempt_income_fen": 0,
                        "attendance_deduction_fen": 0,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                    for employee_id in employee_ids
                ],
            }
        )
    )


def _confirm_request(org_id: uuid.UUID, preview: object, key: str) -> ConfirmPayrollRequest:
    return ConfirmPayrollRequest(
        org_id=org_id,
        batch_id=preview.batch_id,
        calculation_hash=preview.calculation_hash,
        idempotency_key=key,
        confirmed_by="r3-concurrency",
    )


def _run_guard_race(
    monkeypatch: pytest.MonkeyPatch,
    factory: object,
    first: Callable[[Session], object],
    second: Callable[[Session], object],
) -> list[object]:
    """Run two independent PG transactions through the same locked guard.

    Both workers wait immediately before the production guard acquisition.  The
    first worker keeps its row lock until the main test releases it, proving the
    other transaction cannot pass an empty-range query and post concurrently.
    """

    original = FinanceService._lock_payroll_tax_year
    both_ready = Barrier(2)
    both_attempting = Barrier(2)
    first_guard_locked = Event()
    release_first = Event()
    counter_lock = Lock()
    passed_guard = 0

    def synchronized_guard(
        service: FinanceService, org_id: uuid.UUID, employee_ids: list[uuid.UUID], tax_year: int
    ) -> None:
        nonlocal passed_guard
        both_ready.wait(timeout=15)
        both_attempting.wait(timeout=15)
        original(service, org_id, employee_ids, tax_year)
        with counter_lock:
            passed_guard += 1
            first = passed_guard == 1
        if first:
            first_guard_locked.set()
            assert release_first.wait(timeout=15)

    monkeypatch.setattr(FinanceService, "_lock_payroll_tax_year", synchronized_guard)

    def invoke(operation: Callable[[Session], object]) -> object:
        with factory.begin() as worker:  # type: ignore[union-attr]
            return operation(worker)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke, operation) for operation in (first, second)]
        assert first_guard_locked.wait(timeout=15)
        with counter_lock:
            # The other worker crossed the deterministic barrier before either
            # invoked the DB guard, yet it cannot pass the same row lock.
            assert passed_guard == 1
        release_first.set()
        results = [future.result(timeout=20) for future in futures]
    return results


def test_r3_001_january_and_march_confirmations_are_linearized_by_tax_year_guard(
    postgres_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = make_session_factory(postgres_engine)  # type: ignore[arg-type]
    with factory.begin() as setup:
        organization = seed_organization(
            setup, accounting_period_control_enabled=False, name="R3-001 跨月并发企业"
        )
        employee_id = _register_payroll_facts(setup, organization)[0]
        january = _preview_regular(
            setup, organization.id, [employee_id], payroll_month=3, key="r3-001-jan-preview"
        )
        march = _preview_regular(
            setup, organization.id, [employee_id], payroll_month=5, key="r3-001-mar-preview"
        )
        assert january.status == march.status == "calculated"
        org_id = organization.id

    results = _run_guard_race(
        monkeypatch,
        factory,
        lambda session: FinanceService(session).confirm_payroll(
            _confirm_request(org_id, january, "r3-001-jan-confirm")
        ),
        lambda session: FinanceService(session).confirm_payroll(
            _confirm_request(org_id, march, "r3-001-mar-confirm")
        ),
    )
    assert sum(result.status == "posted" for result in results) == 1
    rejected = next(result for result in results if result.status == "rejected")
    assert rejected.errors in (["LATER_PAYROLL_TAX_STATE_EXISTS"], ["STALE_PAYROLL_CALCULATION"])

    with factory() as verification:
        posted = verification.scalars(
            select(PayrollBatch).where(PayrollBatch.id.in_([january.batch_id, march.batch_id]))
        ).all()
        assert sum(batch.status == "posted" for batch in posted) == 1
        assert verification.scalars(
            select(PayrollTaxYearGuard).where(
                PayrollTaxYearGuard.org_id == org_id,
                PayrollTaxYearGuard.employee_id == employee_id,
                PayrollTaxYearGuard.tax_year == 2026,
            )
        ).one()
        slots = verification.scalars(
            select(PayrollTaxStateSlot).where(
                PayrollTaxStateSlot.org_id == org_id,
                PayrollTaxStateSlot.employee_id == employee_id,
            )
        ).all()
        assert len(slots) == 1
        posted_events = verification.scalars(
            select(BusinessEvent).where(
                BusinessEvent.org_id == org_id,
                BusinessEvent.event_type == "payroll_accrual",
                BusinessEvent.status == "posted",
            )
        ).all()
        assert len(posted_events) == 1


def test_r3_001_confirmation_and_reversal_cannot_cross_the_same_tax_year_guard(
    postgres_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = make_session_factory(postgres_engine)  # type: ignore[arg-type]
    with factory.begin() as setup:
        organization = seed_organization(
            setup, accounting_period_control_enabled=False, name="R3-001 确认冲正并发企业"
        )
        employee_id = _register_payroll_facts(setup, organization)[0]
        january = _preview_regular(
            setup, organization.id, [employee_id], payroll_month=3, key="r3-001-reverse-jan"
        )
        january_confirmed = FinanceService(setup).confirm_payroll(
            _confirm_request(organization.id, january, "r3-001-reverse-jan-confirm")
        )
        assert january_confirmed.status == "posted", january_confirmed.errors
        march = _preview_regular(
            setup, organization.id, [employee_id], payroll_month=5, key="r3-001-reverse-mar"
        )
        assert march.status == "calculated", march.errors
        org_id = organization.id

    results = _run_guard_race(
        monkeypatch,
        factory,
        lambda session: FinanceService(session).reverse_event(
            ReverseEventRequest(
                org_id=org_id,
                event_id=january_confirmed.event_id,
                idempotency_key="r3-001-reverse-jan-event",
                reason="并发顺序测试",
                posting_date=date(2026, 6, 6),
            )
        ),
        lambda session: FinanceService(session).confirm_payroll(
            _confirm_request(org_id, march, "r3-001-reverse-mar-confirm")
        ),
    )
    assert sum(result.status == "posted" for result in results) == 1
    rejected = next(result for result in results if result.status == "rejected")
    assert rejected.errors in (
        ["REVERSE_DEPENDENT_PAYROLL_BATCHES_FIRST"],
        ["STALE_PAYROLL_CALCULATION"],
    )


def test_r3_001_multi_employee_guards_are_locked_in_employee_id_order(
    postgres_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    factory = make_session_factory(postgres_engine)  # type: ignore[arg-type]
    with factory.begin() as setup:
        organization = seed_organization(
            setup, accounting_period_control_enabled=False, name="R3-001 多员工锁顺序企业"
        )
        employee_ids = _register_payroll_facts(setup, organization, employee_count=2)
        january = _preview_regular(
            setup, organization.id, employee_ids, payroll_month=3, key="r3-001-order-jan"
        )
        # The second client sends the same employees in the opposite business
        # order.  The service must still lock guards by UUID, not request order.
        march = _preview_regular(
            setup,
            organization.id,
            list(reversed(employee_ids)),
            payroll_month=5,
            key="r3-001-order-mar",
        )
        org_id = organization.id

    results = _run_guard_race(
        monkeypatch,
        factory,
        lambda session: FinanceService(session).confirm_payroll(
            _confirm_request(org_id, january, "r3-001-order-jan-confirm")
        ),
        lambda session: FinanceService(session).confirm_payroll(
            _confirm_request(org_id, march, "r3-001-order-mar-confirm")
        ),
    )
    assert sum(result.status == "posted" for result in results) == 1
    assert all(
        result.status == "posted"
        or result.errors in (["LATER_PAYROLL_TAX_STATE_EXISTS"], ["STALE_PAYROLL_CALCULATION"])
        for result in results
    )
