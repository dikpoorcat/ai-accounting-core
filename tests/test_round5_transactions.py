"""R5 PostgreSQL service-envelope regressions.

Every worker owns a database transaction and waits at a barrier before it
calls the public service entry point.  A raw SQLAlchemy exception therefore
fails the executor outright instead of being hidden by a shared test session.
"""

from __future__ import annotations

import shutil
import uuid
from collections.abc import Callable, Iterator
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier, Lock
from typing import Any

import pytest
from alembic.config import Config
from conftest import prepare_authenticated_bank_account
from sqlalchemy import event, select
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from test_payroll_service import (
    add_bank_row,
    payment_request,
    payroll_parameters,
    register_payroll_facts,
)
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.database import make_session_factory
from ai_accounting.models import (
    Counterparty,
    EmployeePayrollProfileVersion,
    OpenItem,
    PayrollBatch,
    PayrollLine,
    PayrollPolicyVersion,
    Settlement,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RecordEventRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollOpeningStateRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture
def postgres_engine() -> Iterator[Engine]:
    """Use one empty PostgreSQL 17 database for the R5 service concurrency matrix."""

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url)
        command.upgrade(config, "head")
        command.check(config)
        from sqlalchemy import create_engine

        engine = create_engine(database_url)
        try:
            yield engine
        finally:
            engine.dispose()


def _preview_request(
    org_id: uuid.UUID,
    employee_id: uuid.UUID,
    *,
    idempotency_key: str,
    period: str = "2026-03",
    tax_reported_salary_fen: int = 1_000_000,
) -> PreviewPayrollRequest:
    month = int(period[-2:])
    payment_date = date(2026, month, 5)
    return PreviewPayrollRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": idempotency_key,
            "batch_kind": "regular",
            "payroll_period": period,
            "posting_date": payment_date.isoformat(),
            "payment_date": payment_date.isoformat(),
            "employee_items": [
                {
                    "employee_id": employee_id,
                    "tax_reported_salary_fen": tax_reported_salary_fen,
                    "special_additional_deduction_fen": 0,
                    "other_legal_deduction_fen": 0,
                }
            ],
        }
    )


def _run_two_connections(
    factory: Any,
    requests: list[Any],
    operation: Callable[[FinanceService, Any], Any],
    *,
    reverse_submission_order: bool = False,
    authority: object | None = None,
    tool_name: str | None = None,
) -> list[Any]:
    """Execute public writes concurrently, with no session shared by workers."""

    barrier = Barrier(2)

    def worker(request: Any) -> Any:
        barrier.wait(timeout=15)
        with factory.begin() as session:
            if authority is None:
                return operation(FinanceService(session), request)
            assert tool_name is not None
            with authority.attributed_call(session, tool_name=tool_name):
                return operation(FinanceService(session), request)

    submitted = list(reversed(requests)) if reverse_submission_order else requests
    with ThreadPoolExecutor(max_workers=2) as executor:
        return list(executor.map(worker, submitted))


def _assert_replay(results: list[Any]) -> None:
    assert [result.status for result in results] == ["posted", "posted"]
    assert len({result.event_id for result in results}) == 1


def _assert_mismatch(results: list[Any]) -> None:
    assert {result.status for result in results} == {"posted", "rejected"}
    rejected = next(result for result in results if result.status == "rejected")
    assert rejected.errors == ["PAYROLL_IDEMPOTENCY_PAYLOAD_MISMATCH"]


def _assert_correction_blocked(result: dict[str, object], batch_id: uuid.UUID) -> None:
    assert result["status"] == "rejected"
    assert result["errors"] == ["PAYROLL_VERSION_CORRECTION_BLOCKED_BY_FINAL_FACTS"]
    assert result["data"] == {
        "correction_status": "blocked_by_final_facts",
        "blocking_batch_ids": [str(batch_id)],
        "activation_condition": "reverse_blocking_batches_then_rebuild_payroll",
    }


def _register_second_employee(session: Session, org_id: uuid.UUID) -> uuid.UUID:
    service = FinanceService(session)
    employee = service.register_employee(
        RegisterEmployeeRequest(
            org_id=org_id,
            employee_code="R5-CONCURRENT-E-002",
            name="R5 并发员工二",
            employment_start_date=date(2026, 3, 1),
            tax_withholding_start_date=date(2026, 3, 1),
            status="active",
        )
    )
    employee_id = uuid.UUID(employee["employee_id"])
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
    return employee_id


def _prepare_payment_requests(
    factory: Any,
    *,
    organization_name: str,
    event_type: str,
) -> tuple[RecordEventRequest, RecordEventRequest, object]:
    """Make two different banks for one still-open canonical payroll payable."""

    with factory() as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name=organization_name,
        )
        authority = prepare_authenticated_bank_account(session, organization)
        employee_id = register_payroll_facts(session, organization)
        service = FinanceService(session)
        preview = service.preview_payroll(
            _preview_request(organization.id, employee_id, idempotency_key="r5-payment-preview")
        )
        assert preview.status == "calculated", preview.errors
        confirmed = service.confirm_payroll(
            ConfirmPayrollRequest(
                org_id=organization.id,
                batch_id=preview.batch_id,
                calculation_hash=preview.calculation_hash,
                idempotency_key="r5-payment-confirm",
            )
        )
        assert confirmed.status == "posted", confirmed.errors
        salary = session.scalar(
            select(OpenItem).where(
                OpenItem.source_event_id == confirmed.event_id,
                OpenItem.payable_category == "salary",
            )
        )
        assert salary is not None

        # A normal salary payment creates the employee-withheld open items
        # that statutory payments must prove through their own source edges.
        if event_type != "salary_payment":
            salary_bank = add_bank_row(
                session, organization, -839_500, f"{organization_name}-salary-source"
            )
            salary_result = service.record_event(
                payment_request(
                    organization,
                    event_type="salary_payment",
                    amount_fen=839_500,
                    allocations=[{"open_item_id": salary.id, "amount_fen": 1_000_000}],
                    salary_withholdings=[
                        {
                            "open_item_id": salary.id,
                            "employee_social_insurance_items": {"pension": 80_000},
                            "employee_housing_fund_items": {"housing_fund": 70_000},
                            "individual_income_tax_fen": 10_500,
                        }
                    ],
                    bank=salary_bank,
                    key=f"{organization_name}-salary-source",
                )
            )
            assert salary_result.status == "posted", salary_result.errors

        categories = {
            "salary_payment": ("salary",),
            "social_insurance_payment": ("employer_social", "withheld_employee_social"),
            "housing_fund_payment": ("employer_housing", "withheld_employee_housing"),
            "individual_income_tax_payment": ("individual_income_tax",),
        }[event_type]
        items = session.scalars(
            select(OpenItem)
            .where(
                OpenItem.org_id == organization.id,
                OpenItem.payable_category.in_(categories),
                OpenItem.status.in_(("open", "partial")),
            )
            .order_by(OpenItem.payable_category, OpenItem.id)
        ).all()
        assert {item.payable_category for item in items} == set(categories)
        allocated_amount = sum(item.original_amount_fen - item.settled_amount_fen for item in items)
        salary_withholdings: list[dict[str, object]] | None = None
        if event_type == "salary_payment":
            assert allocated_amount == 1_000_000
            amount = 839_500
            salary_withholdings = [
                {
                    "open_item_id": salary.id,
                    "employee_social_insurance_items": {"pension": 80_000},
                    "employee_housing_fund_items": {"housing_fund": 70_000},
                    "individual_income_tax_fen": 10_500,
                }
            ]
        else:
            amount = allocated_amount
        first_bank = add_bank_row(session, organization, -amount, f"{organization_name}-first")
        second_bank = add_bank_row(session, organization, -amount, f"{organization_name}-second")
        request = payment_request(
            organization,
            event_type=event_type,
            amount_fen=amount,
            allocations=[
                {
                    "open_item_id": item.id,
                    "amount_fen": item.original_amount_fen - item.settled_amount_fen,
                }
                for item in items
            ],
            salary_withholdings=salary_withholdings,
            bank=first_bank,
            key=f"{organization_name}-same-key",
        )
        changed_data = request.model_dump(mode="json")
        changed_data["bank_transaction_references"] = [{"id": str(second_bank.id)}]
        changed_data["description"] = "different request payload"
        changed = RecordEventRequest.model_validate(changed_data)
        session.commit()
        return request, changed, authority


def test_r5_004_postgres_correction_barrier_reports_final_batch_and_unblocks_after_reverse(
    postgres_engine: Engine,
) -> None:
    """All version facts share one final-payroll correction barrier on PostgreSQL."""

    factory = make_session_factory(postgres_engine)
    with factory.begin() as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R5 PG correction barrier",
        )
        employee_id = register_payroll_facts(session, organization)
        service = FinanceService(session)
        opening_request = RegisterPayrollOpeningStateRequest(
            org_id=organization.id,
            employee_id=employee_id,
            tax_year=2026,
            through_month=2,
            cumulative_income_fen=0,
            cumulative_tax_exempt_income_fen=0,
            cumulative_basic_deduction_fen=0,
            cumulative_employee_social_insurance_fen=0,
            cumulative_employee_housing_fund_fen=0,
            cumulative_special_additional_deduction_fen=0,
            cumulative_other_legal_deduction_fen=0,
            cumulative_tax_relief_fen=0,
            cumulative_tax_withheld_fen=0,
        )
        opening = service.register_payroll_opening_state(opening_request)
        assert opening["status"] == "registered", opening
        preview = service.preview_payroll(
            _preview_request(organization.id, employee_id, idempotency_key="r5-pg-correction")
        )
        assert preview.status == "calculated", preview.errors
        confirmed = service.confirm_payroll(
            ConfirmPayrollRequest(
                org_id=organization.id,
                batch_id=preview.batch_id,
                calculation_hash=preview.calculation_hash,
                idempotency_key="r5-pg-correction-confirm",
            )
        )
        assert confirmed.status == "posted", confirmed.errors
        batch = session.get(PayrollBatch, preview.batch_id)
        line = session.scalar(
            select(PayrollLine).where(PayrollLine.payroll_batch_id == preview.batch_id)
        )
        assert batch is not None and line is not None
        profile = session.get(
            EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
        )
        policy = session.get(PayrollPolicyVersion, batch.policy_version_id)
        assert profile is not None and policy is not None
        profile_correction = RegisterEmployeePayrollProfileVersionRequest(
            org_id=organization.id,
            employee_id=employee_id,
            effective_from=date(2026, 3, 1),
            effective_to=date(2026, 3, 31),
            expense_role=profile.expense_role,
            social_insurance_base_fen=profile.social_insurance_base_fen + 1,
            housing_fund_base_fen=profile.housing_fund_base_fen + 1,
            resident_employee=True,
            supersedes_profile_version_id=profile.id,
        )
        _assert_correction_blocked(
            service.register_employee_payroll_profile_version(profile_correction), preview.batch_id
        )
        _assert_correction_blocked(
            service.register_payroll_policy_version(
                RegisterPayrollPolicyVersionRequest(
                    org_id=organization.id,
                    region=policy.region,
                    effective_from=date(2026, 3, 1),
                    effective_to=date(2026, 3, 31),
                    version="r5-pg-policy-correction",
                    source_url=policy.source_url,
                    parameters=payroll_parameters(),
                    supersedes_policy_version_id=policy.id,
                )
            ),
            preview.batch_id,
        )
        _assert_correction_blocked(
            service.register_payroll_opening_state(
                opening_request.model_copy(
                    update={
                        "cumulative_income_fen": 100,
                        "supersedes_opening_state_id": uuid.UUID(opening["opening_state_id"]),
                    }
                )
            ),
            preview.batch_id,
        )
        reversed_result = service.reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=confirmed.event_id,
                idempotency_key="r5-pg-correction-reverse",
                reason="R5 更正前规范冲正",
                posting_date=date(2026, 3, 6),
            )
        )
        assert reversed_result.status == "posted", reversed_result.errors
        profile_registration = service.register_employee_payroll_profile_version(profile_correction)
        assert profile_registration["status"] == "registered", profile_registration


def test_r5_006_preview_and_confirmation_use_the_same_idempotency_envelope(
    postgres_engine: Engine,
) -> None:
    factory = make_session_factory(postgres_engine)
    with factory.begin() as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R5 preview and confirm envelope",
        )
        employee_id = register_payroll_facts(session, organization)
        org_id = organization.id

    same_preview = _preview_request(org_id, employee_id, idempotency_key="r5-preview-same")
    preview_results = _run_two_connections(
        factory,
        [same_preview, same_preview],
        lambda service, request: service.preview_payroll(request),
    )
    assert [result.status for result in preview_results] == ["calculated", "calculated"]
    assert len({result.batch_id for result in preview_results}) == 1
    preview = preview_results[0]

    same_confirm = ConfirmPayrollRequest(
        org_id=org_id,
        batch_id=preview.batch_id,
        calculation_hash=preview.calculation_hash,
        idempotency_key="r5-confirm-same",
    )
    confirmation_results = _run_two_connections(
        factory,
        [same_confirm, same_confirm],
        lambda service, request: service.confirm_payroll(request),
    )
    assert [result.status for result in confirmation_results] == ["posted", "posted"]
    assert len({result.event_id for result in confirmation_results}) == 1

    # Build a fresh draft so the two workers reach a business-event unique
    # conflict through the confirmation path, not a stale-batch shortcut.
    with factory.begin() as session:
        next_preview = FinanceService(session).preview_payroll(
            _preview_request(
                org_id,
                employee_id,
                idempotency_key="r5-confirm-different",
                period="2026-04",
            )
        )
        assert next_preview.status == "calculated", next_preview.errors
    different_confirm = ConfirmPayrollRequest(
        org_id=org_id,
        batch_id=next_preview.batch_id,
        calculation_hash=next_preview.calculation_hash,
        idempotency_key="r5-confirm-different",
        confirmation_note="R5 并发确认负例二",
    )
    confirmation_mismatch = _run_two_connections(
        factory,
        [
            ConfirmPayrollRequest(
                org_id=org_id,
                batch_id=next_preview.batch_id,
                calculation_hash=next_preview.calculation_hash,
                idempotency_key="r5-confirm-different",
                confirmation_note="R5 并发确认负例一",
            ),
            different_confirm,
        ],
        lambda service, request: service.confirm_payroll(request),
        reverse_submission_order=True,
    )
    _assert_mismatch(confirmation_mismatch)


@pytest.mark.parametrize(
    "event_type",
    [
        "salary_payment",
        "social_insurance_payment",
        "housing_fund_payment",
        "individual_income_tax_payment",
    ],
)
def test_r5_006_every_payroll_payment_entry_replays_and_rejects_payload_mismatch(
    postgres_engine: Engine, event_type: str
) -> None:
    factory = make_session_factory(postgres_engine)
    original, changed, authority = _prepare_payment_requests(
        factory,
        organization_name=f"R5 {event_type}",
        event_type=event_type,
    )
    _assert_replay(
        _run_two_connections(
            factory,
            [original, original],
            lambda service, request: service.record_event(request),
            authority=authority,
            tool_name="finance_record_event",
        )
    )

    _assert_mismatch(
        _run_two_connections(
            factory,
            [original, changed],
            lambda service, request: service.record_event(request),
            reverse_submission_order=True,
            authority=authority,
            tool_name="finance_record_event",
        )
    )


def test_r5_006_first_shared_agency_is_safe_across_connections(
    postgres_engine: Engine,
) -> None:
    """Force two confirmations past the same empty-agency read, then reverse twice."""

    factory = make_session_factory(postgres_engine)
    with factory.begin() as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R5 shared agency construction",
        )
        first_employee_id = register_payroll_facts(session, organization)
        second_employee_id = _register_second_employee(session, organization.id)
        first_preview = FinanceService(session).preview_payroll(
            _preview_request(
                organization.id,
                first_employee_id,
                idempotency_key="r5-agency-preview-one",
                period="2026-03",
            )
        )
        second_preview = FinanceService(session).preview_payroll(
            _preview_request(
                organization.id,
                second_employee_id,
                idempotency_key="r5-agency-preview-two",
                period="2026-04",
            )
        )
        assert first_preview.status == second_preview.status == "calculated"
        org_id = organization.id

    # The hook fires after both workers queried the exact social-agency row
    # and before either can insert it.  It deterministically exercises the
    # nested savepoint/readback branch in _agency_counterparty.
    agency_read_barrier = Barrier(2)
    counter = 0
    counter_lock = Lock()

    def gate_empty_agency_reads(
        _conn: Any,
        _cursor: Any,
        statement: str,
        _parameters: Any,
        _context: Any,
        _executemany: bool,
    ) -> None:
        nonlocal counter
        if "FROM counterparties" not in statement or "counterparties.name" not in statement:
            return
        with counter_lock:
            if counter >= 2:
                return
            counter += 1
        agency_read_barrier.wait(timeout=15)

    event.listen(postgres_engine, "after_cursor_execute", gate_empty_agency_reads)
    try:
        confirmations = _run_two_connections(
            factory,
            [
                ConfirmPayrollRequest(
                    org_id=org_id,
                    batch_id=first_preview.batch_id,
                    calculation_hash=first_preview.calculation_hash,
                    idempotency_key="r5-agency-confirm-one",
                ),
                ConfirmPayrollRequest(
                    org_id=org_id,
                    batch_id=second_preview.batch_id,
                    calculation_hash=second_preview.calculation_hash,
                    idempotency_key="r5-agency-confirm-two",
                ),
            ],
            lambda service, request: service.confirm_payroll(request),
            reverse_submission_order=True,
        )
    finally:
        event.remove(postgres_engine, "after_cursor_execute", gate_empty_agency_reads)
    assert [result.status for result in confirmations] == ["posted", "posted"]
    assert counter == 2
    with Session(postgres_engine) as session:
        agencies = session.scalars(
            select(Counterparty).where(
                Counterparty.org_id == org_id,
                Counterparty.kind == "other",
                Counterparty.name == "法定缴费机构 社保局",
            )
        ).all()
        assert len(agencies) == 1


def test_r5_006_reverse_replays_and_rejects_payload_mismatch_across_connections(
    postgres_engine: Engine,
) -> None:
    factory = make_session_factory(postgres_engine)
    same, _changed, authority = _prepare_payment_requests(
        factory,
        organization_name="R5 reverse same",
        event_type="salary_payment",
    )
    with factory.begin() as session:
        with authority.attributed_call(session, tool_name="finance_record_event"):
            source = FinanceService(session).record_event(
                same.model_copy(update={"idempotency_key": "r5-reverse-source"})
            )
        assert source.status == "posted", source.errors
        source_event_id = source.event_id
        source_org_id = same.org_id

    reversal = ReverseEventRequest(
        org_id=source_org_id,
        event_id=source_event_id,
        idempotency_key="r5-reverse-same",
        reason="R5 同键冲正",
        posting_date=date(2026, 3, 6),
    )
    reverse_results = _run_two_connections(
        factory,
        [reversal, reversal],
        lambda service, request: service.reverse_event(request),
        reverse_submission_order=True,
        authority=authority,
        tool_name="finance_reverse_event",
    )
    _assert_replay(reverse_results)
    reversal_id = reverse_results[0].event_id
    with Session(postgres_engine) as session:
        settlements = session.scalars(
            select(Settlement).where(Settlement.payment_event_id == source_event_id)
        ).all()
        assert settlements
        assert all(
            item.reversed and item.reversed_by_event_id == reversal_id for item in settlements
        )

    mismatch = _run_two_connections(
        factory,
        [
            reversal,
            reversal.model_copy(update={"reason": "R5 不同原因"}),
        ],
        lambda service, request: service.reverse_event(request),
        reverse_submission_order=True,
        authority=authority,
        tool_name="finance_reverse_event",
    )
    _assert_mismatch(mismatch)
