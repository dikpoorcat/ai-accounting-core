from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Event, Lock

import pytest
from _postgres_helpers import authenticated_business_database
from sqlalchemy import select
from sqlalchemy.orm import Session
from test_payroll_service import payroll_parameters

from ai_accounting.database import make_session_factory
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
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

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture
def postgres_engine():
    with authenticated_business_database("round3_tax") as database:
        yield database


def _call(session, authority, method, request):
    with authority.attributed_call(session, tool_name="finance_" + method):
        return getattr(FinanceService(session), method)(request)


def _register_payroll_facts(
    session: Session, organization: Organization, authority, employee_count: int = 1
) -> list[uuid.UUID]:
    employee_ids: list[uuid.UUID] = []
    for number in range(employee_count):
        employee = _call(
            session,
            authority,
            "register_employee",
            RegisterEmployeeRequest(
                org_id=organization.id,
                employee_code=f"R3-TAX-{number + 1}",
                name=f"R3 税务员工 {number + 1}",
                employment_start_date=date(2026, 3, 1),
                tax_withholding_start_date=date(2026, 3, 1),
                status="active",
            ),
        )
        employee_id = uuid.UUID(employee["employee_id"])
        employee_ids.append(employee_id)
        assert (
            _call(
                session,
                authority,
                "register_employee_payroll_profile_version",
                RegisterEmployeePayrollProfileVersionRequest(
                    org_id=organization.id,
                    employee_id=employee_id,
                    effective_from=date(2026, 3, 1),
                    expense_role="payroll_management_expense",
                    social_insurance_base_fen=1_000_000,
                    housing_fund_base_fen=1_000_000,
                    resident_employee=True,
                ),
            )["status"]
            == "registered"
        )
    assert (
        _call(
            session,
            authority,
            "register_payroll_policy_version",
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
            ),
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
    authority,
    evidence_id,
    key: str,
) -> object:
    payroll_date = date(2026, payroll_month, 5)
    return _call(
        session,
        authority,
        "preview_payroll",
        PreviewPayrollRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": key,
                "evidence_references": [evidence_id],
                "batch_kind": "regular",
                "payroll_period": f"2026-{payroll_month:02d}",
                "posting_date": payroll_date,
                "payment_date": payroll_date,
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "tax_reported_salary_fen": 1_000_000,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                    for employee_id in employee_ids
                ],
            }
        ),
    )


def _confirm_request(org_id: uuid.UUID, preview: object, key: str) -> ConfirmPayrollRequest:
    return ConfirmPayrollRequest(
        org_id=org_id,
        batch_id=preview.batch_id,
        calculation_hash=preview.calculation_hash,
        idempotency_key=key,
    )


def _run_guard_race(
    monkeypatch: pytest.MonkeyPatch,
    factory: object,
    authority,
    first: Callable[[Session], object],
    second: Callable[[Session], object],
    *,
    first_tool_name="finance_confirm_payroll",
) -> list[object]:
    """Run two independent PG transactions through the same locked guard.

    Both workers start their operations together.  The first worker keeps its
    tax-year row lock until the main test releases it.  The common submitter may
    acquire its organization lock first, so synchronizing inside the year guard
    would deadlock the harness before either operation could acquire that guard.
    """

    original = FinanceService._lock_payroll_tax_year
    both_ready = Barrier(2)
    first_guard_locked = Event()
    release_first = Event()
    counter_lock = Lock()
    passed_guard = 0

    def synchronized_guard(
        service: FinanceService, org_id: uuid.UUID, employee_ids: list[uuid.UUID], tax_year: int
    ) -> None:
        nonlocal passed_guard
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
            tool_name = first_tool_name if operation is first else "finance_confirm_payroll"
            with authority.attributed_call(worker, tool_name=tool_name):
                both_ready.wait(timeout=15)
                return operation(worker)

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(invoke, operation) for operation in (first, second)]
        assert first_guard_locked.wait(timeout=15)
        with counter_lock:
            # Both calls started, yet the other writer cannot pass the locked
            # year guard while the first transaction still owns it.
            assert passed_guard == 1
        release_first.set()
        results = [future.result(timeout=20) for future in futures]
    return results


def test_r3_001_january_and_march_confirmations_are_linearized_by_tax_year_guard(
    postgres_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    postgres_engine, org_id, evidence_id, authority = postgres_engine
    factory = make_session_factory(postgres_engine)  # type: ignore[arg-type]
    with factory.begin() as setup:
        organization = setup.get(Organization, org_id)
        employee_id = _register_payroll_facts(setup, organization, authority)[0]
        january = _preview_regular(
            setup,
            organization.id,
            [employee_id],
            payroll_month=3,
            authority=authority,
            evidence_id=evidence_id,
            key="r3-001-jan-preview",
        )
        march = _preview_regular(
            setup,
            organization.id,
            [employee_id],
            payroll_month=5,
            authority=authority,
            evidence_id=evidence_id,
            key="r3-001-mar-preview",
        )
        assert january.status == march.status == "calculated"
        org_id = organization.id

    results = _run_guard_race(
        monkeypatch,
        factory,
        authority,
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
            select(BusinessEvent)
            .join(BusinessEventComponent, BusinessEventComponent.event_id == BusinessEvent.id)
            .where(
                BusinessEvent.org_id == org_id,
                BusinessEventComponent.kind == "payroll_accrual",
                BusinessEvent.status == "posted",
            )
        ).all()
        assert len(posted_events) == 1


def test_r3_001_confirmation_and_reversal_cannot_cross_the_same_tax_year_guard(
    postgres_engine: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    postgres_engine, org_id, evidence_id, authority = postgres_engine
    factory = make_session_factory(postgres_engine)  # type: ignore[arg-type]
    with factory.begin() as setup:
        organization = setup.get(Organization, org_id)
        employee_id = _register_payroll_facts(setup, organization, authority)[0]
        january = _preview_regular(
            setup,
            organization.id,
            [employee_id],
            payroll_month=3,
            authority=authority,
            evidence_id=evidence_id,
            key="r3-001-reverse-jan",
        )
        january_confirmed = _call(
            setup,
            authority,
            "confirm_payroll",
            _confirm_request(organization.id, january, "r3-001-reverse-jan-confirm"),
        )
        assert january_confirmed.status == "posted", january_confirmed.errors
        march = _preview_regular(
            setup,
            organization.id,
            [employee_id],
            payroll_month=5,
            authority=authority,
            evidence_id=evidence_id,
            key="r3-001-reverse-mar",
        )
        assert march.status == "calculated", march.errors
        org_id = organization.id

    results = _run_guard_race(
        monkeypatch,
        factory,
        authority,
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
        first_tool_name="finance_reverse_event",
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
    postgres_engine, org_id, evidence_id, authority = postgres_engine
    factory = make_session_factory(postgres_engine)  # type: ignore[arg-type]
    with factory.begin() as setup:
        organization = setup.get(Organization, org_id)
        employee_ids = _register_payroll_facts(setup, organization, authority, employee_count=2)
        january = _preview_regular(
            setup,
            organization.id,
            employee_ids,
            payroll_month=3,
            authority=authority,
            evidence_id=evidence_id,
            key="r3-001-order-jan",
        )
        # The second client sends the same employees in the opposite business
        # order.  The service must still lock guards by UUID, not request order.
        march = _preview_regular(
            setup,
            organization.id,
            list(reversed(employee_ids)),
            payroll_month=5,
            authority=authority,
            evidence_id=evidence_id,
            key="r3-001-order-mar",
        )
        org_id = organization.id

    results = _run_guard_race(
        monkeypatch,
        factory,
        authority,
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
