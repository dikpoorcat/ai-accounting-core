from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import date

import pytest
from alembic.config import Config
from conftest import prepare_authenticated_bank_account
from sqlalchemy import create_engine, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import get_account_by_role, seed_organization
from ai_accounting.ledger import Entry, create_voucher
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventDependency,
    Counterparty,
    Employee,
    EmployeePayrollProfileVersion,
    OpenItem,
    PayrollBatch,
    PayrollEventLink,
    PayrollLine,
    PayrollOpeningState,
    PayrollPolicyVersion,
    PayrollTaxStateSlot,
    PayrollWithholdingEntitlement,
    PayrollWithholdingPaymentAllocation,
    Settlement,
    Voucher,
    VoucherLine,
)
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


@pytest.fixture(scope="module")
def postgres_engine() -> Iterator[object]:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
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


@pytest.fixture
def isolated_postgres_engine() -> Iterator[object]:
    """Isolate the one owner-mode event-state regression from legacy tests."""

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:
        url = postgres.get_connection_url(driver="psycopg")
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", url)
        command.upgrade(config, "head")
        engine = create_engine(url)
        try:
            yield engine
        finally:
            engine.dispose()


def _policy(session: Session, org_id: object, *, version: str = "2025.1") -> PayrollPolicyVersion:
    policy = PayrollPolicyVersion(
        org_id=org_id,
        region="CN-310000",
        effective_from=date(2025, 7, 1),
        version=version,
        source_url="https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
        parameters={"social_insurance": {}, "housing_fund": {}},
    )
    session.add(policy)
    session.flush()
    return policy


def _employee_profile(
    session: Session, org_id: object, *, code: str = "E-001"
) -> tuple[Employee, EmployeePayrollProfileVersion]:
    counterparty = Counterparty(org_id=org_id, kind="employee", name=f"员工 {code}")
    session.add(counterparty)
    session.flush()
    employee = Employee(
        org_id=org_id,
        counterparty_id=counterparty.id,
        employee_code=code,
        name=f"员工 {code}",
        employment_start_date=date(2025, 7, 1),
    )
    session.add(employee)
    session.flush()
    profile = EmployeePayrollProfileVersion(
        org_id=org_id,
        employee_id=employee.id,
        effective_from=date(2025, 7, 1),
        expense_role="payroll_management_expense",
        social_insurance_base_fen=0,
        housing_fund_base_fen=0,
        resident_employee=True,
    )
    session.add(profile)
    session.flush()
    return employee, profile


def _line(
    *, org_id: object, batch_id: object, employee: Employee, profile: EmployeePayrollProfileVersion
) -> PayrollLine:
    return PayrollLine(
        org_id=org_id,
        payroll_batch_id=batch_id,
        employee_id=employee.id,
        employee_payroll_profile_version_id=profile.id,
        tax_reported_salary_fen=10_000,
        gross_salary_fen=10_000,
        net_salary_fen=10_000,
    )


def _event(
    session: Session, org_id: object, key: str, *, event_type: str = "expense_payable"
) -> BusinessEvent:
    event = BusinessEvent(
        org_id=org_id,
        idempotency_key=key,
        event_type=event_type,
        status="draft",
        description="工资计提",
        facts={},
        business_date=date(2026, 2, 28),
        posting_date=date(2026, 2, 28),
        rule_trace=[],
    )
    session.add(event)
    session.flush()
    return event


def _make_final_batch(
    session: Session,
    org_id: object,
    policy: PayrollPolicyVersion,
    employee: Employee,
    profile: EmployeePayrollProfileVersion,
    *,
    key: str,
    version: int = 1,
    reversal_of: PayrollBatch | None = None,
    payroll_period: str = "2026-02",
) -> PayrollBatch:
    batch = PayrollBatch(
        org_id=org_id,
        idempotency_key=f"batch-{key}",
        batch_kind="regular",
        payroll_period=payroll_period,
        version=version,
        status="calculated",
        calculation_hash=(key * 64)[:64],
        request_payload_hash=("r" + key * 63)[:64],
        calculation_input={"employee_items": []},
        calculation_trace=[],
        policy_snapshot={"policy_version": policy.version},
        policy_version_id=policy.id,
        posting_date=date(2026, 2, 28),
        payment_date=date(2026, 3, 5),
        reversal_of_batch_id=reversal_of.id if reversal_of else None,
    )
    session.add(batch)
    session.flush()
    session.add(_line(org_id=org_id, batch_id=batch.id, employee=employee, profile=profile))
    if reversal_of is None:
        tax_year, tax_month = (int(value) for value in batch.payroll_period.split("-"))
        session.add(
            PayrollTaxStateSlot(
                org_id=org_id,
                employee_id=employee.id,
                tax_year=tax_year,
                tax_month=tax_month,
                regular_batch_id=batch.id,
                final_batch_id=batch.id,
            )
        )
    event = _event(session, org_id, f"event-{key}", event_type="payroll_accrual")
    original_event = (
        session.get(BusinessEvent, reversal_of.business_event_id)
        if reversal_of is not None and reversal_of.business_event_id is not None
        else None
    )
    original_voucher = (
        session.scalar(select(Voucher).where(Voucher.event_id == original_event.id))
        if original_event is not None
        else None
    )
    if original_event is not None:
        event.facts = {"original_event_id": str(original_event.id)}
    create_voucher(
        session,
        event=event,
        posting_date=date(2026, 2, 28),
        description="工资计提",
        entries=(
            [
                Entry(account_role="employee_salary_payable", debit_fen=10_000),
                Entry(account_role="payroll_management_expense", credit_fen=10_000),
            ]
            if original_event is not None
            else [
                Entry(account_role="payroll_management_expense", debit_fen=10_000),
                Entry(account_role="employee_salary_payable", credit_fen=10_000),
            ]
        ),
        reversal_of=original_voucher,
    )
    batch.business_event_id = event.id
    session.add(
        PayrollEventLink(
            org_id=org_id,
            event_id=event.id,
            payroll_batch_id=batch.id,
            source_payment_event_id=original_event.id if original_event is not None else None,
            link_kind="reversal" if original_event is not None else "payroll_accrual",
        )
    )
    session.flush()
    event.status = "posted"
    batch.status = "posted"
    if original_event is not None:
        original_event.status = "reversed"
        original_event.reversed_by_event_id = event.id
        reversal_of.status = "reversed"
    session.flush()
    return batch


def _make_final_withholding_batch(
    session: Session,
    org_id: object,
    policy: PayrollPolicyVersion,
    employee: Employee,
    profile: EmployeePayrollProfileVersion,
    *,
    key: str,
) -> tuple[PayrollBatch, PayrollLine, PayrollWithholdingEntitlement]:
    """Create a complete posted regular batch with one immutable pension fact."""

    batch = PayrollBatch(
        org_id=org_id,
        idempotency_key=f"withholding-{key}",
        batch_kind="regular",
        payroll_period="2026-02",
        version=1,
        status="calculated",
        calculation_hash=(key * 64)[:64],
        calculation_input={},
        calculation_trace=[],
        policy_snapshot={},
        policy_version_id=policy.id,
        posting_date=date(2026, 2, 28),
        payment_date=date(2026, 3, 5),
    )
    session.add(batch)
    session.flush()
    line = _line(org_id=org_id, batch_id=batch.id, employee=employee, profile=profile)
    line.employee_social_insurance_fen = 80
    line.employee_social_insurance_items = {"pension": 80}
    line.net_salary_fen = 9_920
    session.add(line)
    session.flush()
    entitlement = PayrollWithholdingEntitlement(
        org_id=org_id,
        payroll_line_id=line.id,
        contribution_group="employee_social_insurance",
        insurance_kind="pension",
        amount_fen=80,
    )
    session.add(entitlement)
    tax_year, tax_month = (int(value) for value in batch.payroll_period.split("-"))
    session.add(
        PayrollTaxStateSlot(
            org_id=org_id,
            employee_id=employee.id,
            tax_year=tax_year,
            tax_month=tax_month,
            regular_batch_id=batch.id,
            final_batch_id=batch.id,
        )
    )
    event = _event(session, org_id, f"withholding-event-{key}", event_type="payroll_accrual")
    create_voucher(
        session,
        event=event,
        posting_date=date(2026, 2, 28),
        description="工资计提",
        entries=[
            Entry(account_role="payroll_management_expense", debit_fen=10_000),
            Entry(account_role="employee_salary_payable", credit_fen=10_000),
        ],
    )
    batch.business_event_id = event.id
    session.add(
        PayrollEventLink(
            org_id=org_id,
            event_id=event.id,
            payroll_batch_id=batch.id,
            link_kind="payroll_accrual",
        )
    )
    session.flush()
    event.status = "posted"
    batch.status = "posted"
    session.flush()
    return batch, line, entitlement


def test_pay_014_final_payroll_batches_and_lines_are_immutable(postgres_engine: object) -> None:
    with Session(postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="PAY-014 不变量",
        )
        policy = _policy(session, organization.id)
        employee, profile = _employee_profile(session, organization.id)
        batch = _make_final_batch(session, organization.id, policy, employee, profile, key="a")
        session.commit()
        batch_id = batch.id
        line_id = session.scalar(
            select(PayrollLine.id).where(PayrollLine.payroll_batch_id == batch_id)
        )

    with Session(postgres_engine) as session:
        batch = session.get(PayrollBatch, batch_id)
        assert batch is not None
        batch.calculation_input = {"tampered": True}
        with pytest.raises(DBAPIError, match="posted payroll batches are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        batch = session.get(PayrollBatch, batch_id)
        line = session.get(PayrollLine, line_id)
        assert batch is not None and line is not None
        session.add(
            _line(
                org_id=batch.org_id,
                batch_id=batch.id,
                employee=session.get(Employee, line.employee_id),  # type: ignore[arg-type]
                profile=session.get(  # type: ignore[arg-type]
                    EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
                ),
            )
        )
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        line = session.get(PayrollLine, line_id)
        assert line is not None
        line.tax_reported_salary_fen = 10_001
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        line = session.get(PayrollLine, line_id)
        assert line is not None
        session.delete(line)
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        batch = session.get(PayrollBatch, batch_id)
        line = session.get(PayrollLine, line_id)
        assert batch is not None and line is not None
        draft = PayrollBatch(
            org_id=batch.org_id,
            idempotency_key="move-into-final",
            batch_kind="annual_bonus",
            payroll_period=batch.payroll_period,
            version=1,
            status="calculated",
            calculation_hash="m" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=batch.policy_version_id,
            posting_date=batch.posting_date,
            payment_date=batch.payment_date,
        )
        session.add(draft)
        session.flush()
        employee = session.get(Employee, line.employee_id)
        profile = session.get(
            EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
        )
        assert employee is not None and profile is not None
        draft_line = _line(
            org_id=batch.org_id,
            batch_id=draft.id,
            employee=employee,
            profile=profile,
        )
        session.add(draft_line)
        session.flush()
        draft_line.payroll_batch_id = batch.id
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        batch = session.get(PayrollBatch, batch_id)
        line = session.get(PayrollLine, line_id)
        assert batch is not None and line is not None
        draft = PayrollBatch(
            org_id=batch.org_id,
            idempotency_key="move-out-of-final",
            batch_kind="annual_bonus",
            payroll_period=batch.payroll_period,
            version=1,
            status="calculated",
            calculation_hash="n" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=batch.policy_version_id,
            posting_date=batch.posting_date,
            payment_date=batch.payment_date,
        )
        session.add(draft)
        session.flush()
        line.payroll_batch_id = draft.id
        with pytest.raises(DBAPIError, match="final payroll lines are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        original = session.get(PayrollBatch, batch_id)
        assert original is not None
        policy = session.get(PayrollPolicyVersion, original.policy_version_id)
        line = session.scalar(
            select(PayrollLine).where(PayrollLine.payroll_batch_id == original.id)
        )
        assert policy is not None and line is not None
        employee = session.get(Employee, line.employee_id)
        profile = session.get(
            EmployeePayrollProfileVersion, line.employee_payroll_profile_version_id
        )
        assert employee is not None and profile is not None
        _make_final_batch(
            session,
            original.org_id,
            policy,
            employee,
            profile,
            key="b",
            version=2,
            reversal_of=original,
        )
        original.status = "reversed"
        session.commit()
        assert original.status == "reversed"


def test_pay_015_organization_links_and_final_shape_are_database_enforced(
    postgres_engine: object,
) -> None:
    with Session(postgres_engine) as session:
        organization_a = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="PAY-015 企业 A",
        )
        organization_b = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="PAY-015 企业 B",
        )
        policy_a = _policy(session, organization_a.id, version="A")
        policy_b = _policy(session, organization_b.id, version="B")
        employee_a, profile_a = _employee_profile(session, organization_a.id, code="A")
        _, profile_b = _employee_profile(session, organization_b.id, code="B")
        organization_a_id = organization_a.id
        policy_a_id = policy_a.id
        policy_b_id = policy_b.id
        employee_a_id = employee_a.id
        profile_b_id = profile_b.id
        session.commit()

    with Session(postgres_engine) as session:
        batch = PayrollBatch(
            org_id=organization_a_id,
            idempotency_key="cross-policy",
            batch_kind="regular",
            payroll_period="2026-02",
            version=1,
            status="calculated",
            calculation_hash="c" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=policy_b_id,
            posting_date=date(2026, 2, 28),
            payment_date=date(2026, 3, 5),
        )
        session.add(batch)
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(postgres_engine) as session:
        batch = PayrollBatch(
            org_id=organization_a_id,
            idempotency_key="profile-parent",
            batch_kind="regular",
            payroll_period="2026-03",
            version=1,
            status="calculated",
            calculation_hash="d" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=policy_a_id,
            posting_date=date(2026, 3, 31),
            payment_date=date(2026, 4, 5),
        )
        session.add(batch)
        session.flush()
        session.add(
            _line(
                org_id=organization_a_id,
                batch_id=batch.id,
                employee=session.get(Employee, employee_a_id),  # type: ignore[arg-type]
                profile=session.get(EmployeePayrollProfileVersion, profile_b_id),  # type: ignore[arg-type]
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(postgres_engine) as session:
        incomplete = PayrollBatch(
            org_id=organization_a_id,
            idempotency_key="missing-event-and-lines",
            batch_kind="regular",
            payroll_period="2026-04",
            version=1,
            status="posted",
            calculation_hash="e" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=policy_a_id,
            posting_date=date(2026, 4, 30),
            payment_date=date(2026, 5, 5),
        )
        session.add(incomplete)
        with pytest.raises(DBAPIError, match="final payroll batch"):
            session.commit()


def test_pay_016_final_voucher_lines_reject_insert_update_and_delete(
    postgres_engine: object,
) -> None:
    with Session(postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="PAY-016 凭证不可变",
        )
        event = _event(session, organization.id, "pay-016-source")
        voucher = create_voucher(
            session,
            event=event,
            posting_date=date(2026, 2, 28),
            description="正式凭证",
            entries=[
                Entry(account_role="bank", debit_fen=100),
                Entry(account_role="service_revenue", credit_fen=100),
            ],
        )
        event.status = "posted"
        session.commit()
        voucher_id = voucher.id

    with Session(postgres_engine) as session:
        voucher = session.get(Voucher, voucher_id)
        assert voucher is not None
        bank = get_account_by_role(session, voucher.org_id, "bank")
        session.add(
            VoucherLine(
                org_id=voucher.org_id,
                voucher_id=voucher.id,
                line_number=3,
                account_id=bank.id,
                debit_fen=1,
            )
        )
        with pytest.raises(DBAPIError, match="final voucher"):
            session.flush()

    with Session(postgres_engine) as session:
        line = session.scalar(
            select(VoucherLine)
            .where(VoucherLine.voucher_id == voucher_id)
            .order_by(VoucherLine.line_number)
        )
        assert line is not None
        line.memo = "篡改"
        with pytest.raises(DBAPIError, match="final voucher"):
            session.flush()

    with Session(postgres_engine) as session:
        line = session.scalar(
            select(VoucherLine)
            .where(VoucherLine.voucher_id == voucher_id)
            .order_by(VoucherLine.line_number)
        )
        assert line is not None
        session.delete(line)
        with pytest.raises(DBAPIError, match="final voucher"):
            session.flush()


def test_pay_017_open_item_settlement_conservation_and_org_links(postgres_engine: object) -> None:
    with Session(postgres_engine) as session:
        organization_a = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="PAY-017 企业 A",
        )
        organization_b = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="PAY-017 企业 B",
        )
        counterparty_a = Counterparty(org_id=organization_a.id, kind="supplier", name="机构 A")
        counterparty_b = Counterparty(org_id=organization_b.id, kind="supplier", name="机构 B")
        session.add_all([counterparty_a, counterparty_b])
        event_a = _event(session, organization_a.id, "pay-017-a")
        event_c = _event(session, organization_a.id, "pay-017-c")
        event_b = _event(session, organization_b.id, "pay-017-b")
        item = OpenItem(
            org_id=organization_a.id,
            counterparty_id=counterparty_a.id,
            source_event_id=event_a.id,
            item_type="payable",
            original_amount_fen=100,
            settled_amount_fen=0,
            status="open",
        )
        session.add(item)
        session.commit()
        item_id = item.id
        event_a_id = event_a.id
        event_c_id = event_c.id
        event_b_id = event_b.id

    with Session(postgres_engine) as session:
        item = session.get(OpenItem, item_id)
        assert item is not None
        item.settled_amount_fen = 20
        item.status = "partial"
        with pytest.raises(DBAPIError, match="settlement total"):
            session.commit()

    with Session(postgres_engine) as session:
        item = session.get(OpenItem, item_id)
        assert item is not None
        item.settled_amount_fen = 40
        item.status = "partial"
        session.add(
            Settlement(
                org_id=item.org_id,
                open_item_id=item.id,
                payment_event_id=event_c_id,
                amount_fen=40,
            )
        )
        session.commit()

    with Session(postgres_engine) as session:
        item = session.get(OpenItem, item_id)
        assert item is not None
        item.settled_amount_fen = 100
        item.status = "settled"
        session.add(
            Settlement(
                org_id=item.org_id,
                open_item_id=item.id,
                payment_event_id=event_a_id,
                amount_fen=60,
            )
        )
        session.commit()

    with Session(postgres_engine) as session:
        item = session.get(OpenItem, item_id)
        settlement = session.scalar(
            select(Settlement)
            .where(Settlement.open_item_id == item_id)
            .order_by(Settlement.amount_fen)
        )
        assert item is not None and settlement is not None
        settlement.reversed = True
        item.settled_amount_fen = 60
        item.status = "partial"
        with pytest.raises(DBAPIError, match="ck_settlement_reversal_audit"):
            session.commit()

    with Session(postgres_engine) as session:
        item = session.get(OpenItem, item_id)
        assert item is not None
        session.add(
            Settlement(
                org_id=item.org_id,
                open_item_id=item.id,
                payment_event_id=event_b_id,
                amount_fen=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_r2_003_per_insurance_withholding_cannot_be_reallocated(
    postgres_engine: object,
) -> None:
    """A direct PostgreSQL write cannot spend the pension entitlement as medical."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R2-003",
        )
        policy = _policy(session, organization.id, version="r2-003")
        employee, profile = _employee_profile(session, organization.id, code="R2-003")
        batch = PayrollBatch(
            org_id=organization.id,
            idempotency_key="r2-003-batch",
            batch_kind="regular",
            payroll_period="2026-02",
            version=1,
            status="calculated",
            calculation_hash="3" * 64,
            calculation_input={},
            calculation_trace=[],
            policy_snapshot={},
            policy_version_id=policy.id,
            posting_date=date(2026, 2, 28),
            payment_date=date(2026, 3, 5),
        )
        session.add(batch)
        session.flush()
        line = _line(org_id=organization.id, batch_id=batch.id, employee=employee, profile=profile)
        session.add(line)
        session.flush()
        pension = PayrollWithholdingEntitlement(
            org_id=organization.id,
            payroll_line_id=line.id,
            contribution_group="employee_social_insurance",
            insurance_kind="pension",
            amount_fen=80,
        )
        medical = PayrollWithholdingEntitlement(
            org_id=organization.id,
            payroll_line_id=line.id,
            contribution_group="employee_social_insurance",
            insurance_kind="medical",
            amount_fen=20,
        )
        first_payment = _event(session, organization.id, "r2-003-payment-1")
        session.add_all([pension, medical])
        session.flush()
        session.add(
            PayrollWithholdingPaymentAllocation(
                org_id=organization.id,
                entitlement_id=pension.id,
                payment_event_id=first_payment.id,
                amount_fen=80,
            )
        )
        with pytest.raises(DBAPIError, match="final non-reversal payroll line"):
            session.commit()


def test_r3_003_posted_withholding_entitlements_and_allocations_are_append_only(
    postgres_engine: object,
) -> None:
    """Direct SQL cannot forge a zero-line entitlement or edit an active allocation."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R3-003",
        )
        policy = _policy(session, organization.id, version="r3-003")
        employee, profile = _employee_profile(session, organization.id, code="R3-003")
        _batch, _line, entitlement = _make_final_withholding_batch(
            session, organization.id, policy, employee, profile, key="r3w"
        )
        session.commit()
        organization_id = organization.id
        entitlement_id = entitlement.id

    with Session(postgres_engine) as session:
        entitlement = session.get(PayrollWithholdingEntitlement, entitlement_id)
        assert entitlement is not None
        entitlement.amount_fen = 81
        with pytest.raises(DBAPIError, match="entitlements are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        entitlement = session.get(PayrollWithholdingEntitlement, entitlement_id)
        assert entitlement is not None
        session.delete(entitlement)
        with pytest.raises(DBAPIError, match="entitlements are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        entitlement = session.get(PayrollWithholdingEntitlement, entitlement_id)
        assert entitlement is not None
        forged = PayrollWithholdingEntitlement(
            org_id=organization_id,
            payroll_line_id=entitlement.payroll_line_id,
            contribution_group="employee_social_insurance",
            insurance_kind="medical",
            amount_fen=100,
        )
        session.add(forged)
        with pytest.raises(DBAPIError, match="entitlements are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        payment = _event(session, organization_id, "r3-003-salary")
        payment.event_type = "salary_payment"
        create_voucher(
            session,
            event=payment,
            posting_date=date(2026, 3, 5),
            description="工资支付",
            entries=[
                Entry(account_role="employee_salary_payable", debit_fen=80),
                Entry(account_role="bank", credit_fen=80),
            ],
        )
        payment.status = "posted"
        allocation = PayrollWithholdingPaymentAllocation(
            org_id=organization_id,
            entitlement_id=entitlement_id,
            payment_event_id=payment.id,
            amount_fen=80,
        )
        session.add(allocation)
        session.commit()
        allocation_id = allocation.id

    with Session(postgres_engine) as session:
        allocation = session.get(PayrollWithholdingPaymentAllocation, allocation_id)
        assert allocation is not None
        allocation.amount_fen = 81
        with pytest.raises(DBAPIError, match="immutable except formal reversal"):
            session.flush()

    with Session(postgres_engine) as session:
        allocation = session.get(PayrollWithholdingPaymentAllocation, allocation_id)
        assert allocation is not None
        allocation.reversed = True
        with pytest.raises(DBAPIError, match="immutable except formal reversal"):
            session.flush()


def test_r3_004_final_event_state_requires_draft_and_keeps_refund_original_posted(
    isolated_postgres_engine: object,
) -> None:
    """A direct reversed insert fails; refund reversal never rewrites the advance state."""

    with Session(isolated_postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R3-004",
        )
        direct = BusinessEvent(
            org_id=organization.id,
            idempotency_key="r3-004-direct-reversed",
            event_type="reversal",
            status="reversed",
            description="非法",
            facts={},
            business_date=date(2026, 3, 1),
            posting_date=date(2026, 3, 1),
            rule_trace=[],
        )
        session.add(direct)
        with pytest.raises(DBAPIError, match="created as draft"):
            session.flush()

    with Session(isolated_postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R3-004-refund",
        )
        prepare_authenticated_bank_account(session, organization)
        advance = _event(session, organization.id, "r3-004-advance")
        advance.event_type = "customer_advance"
        advance.facts = {
            "amounts": {"gross_amount_fen": 100, "currency": "CNY"},
            "business_dates": {
                "business_date": "2026-03-01",
                "payment_date": "2026-03-01",
                "posting_date": "2026-03-01",
            },
            "bank_account_code": "1002",
        }
        create_voucher(
            session,
            event=advance,
            posting_date=date(2026, 3, 1),
            description="客户预收",
            entries=[
                Entry(account_role="bank", debit_fen=100),
                Entry(account_role="contract_liability", credit_fen=100),
            ],
        )
        advance.status = "posted"
        refund = _event(session, organization.id, "r3-004-refund-event")
        refund.event_type = "customer_refund"
        refund.facts = {
            "amounts": {"amount_fen": 100, "currency": "CNY"},
            "business_dates": {
                "business_date": "2026-03-02",
                "payment_date": "2026-03-02",
                "posting_date": "2026-03-02",
            },
            "bank_account_code": "1002",
            "details": {"original_event_id": str(advance.id), "refund_kind": "advance"},
        }
        refund_voucher = create_voucher(
            session,
            event=refund,
            posting_date=date(2026, 3, 2),
            description="客户退款",
            entries=[
                Entry(account_role="contract_liability", debit_fen=100),
                Entry(account_role="bank", credit_fen=100),
            ],
        )
        session.add(
            BusinessEventDependency(
                org_id=organization.id,
                parent_event_id=advance.id,
                child_event_id=refund.id,
                dependency_kind="advance_refund",
                amount_fen=100,
            )
        )
        session.flush()
        refund.status = "posted"
        refund_reversal = _event(session, organization.id, "r3-004-refund-reversal")
        refund_reversal.event_type = "reversal"
        refund_reversal.facts = {"original_event_id": str(refund.id)}
        create_voucher(
            session,
            event=refund_reversal,
            posting_date=date(2026, 3, 3),
            description="冲正客户退款",
            entries=[
                Entry(account_role="bank", debit_fen=100),
                Entry(account_role="contract_liability", credit_fen=100),
            ],
            reversal_of=refund_voucher,
        )
        refund_reversal.status = "posted"
        refund.status = "reversed"
        refund.reversed_by_event_id = refund_reversal.id
        session.commit()
        assert advance.status == "posted"
        assert refund.status == "reversed"


def test_r3_002_tax_state_slot_rejects_cross_employee_and_arbitrary_mutation(
    postgres_engine: object,
) -> None:
    """A slot is bound to the posted regular line's employee and payroll period."""

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R3-002",
        )
        policy = _policy(session, organization.id, version="r3-002")
        employee_a, profile_a = _employee_profile(session, organization.id, code="R3-002-A")
        employee_b, profile_b = _employee_profile(session, organization.id, code="R3-002-B")
        regular_a = _make_final_batch(
            session, organization.id, policy, employee_a, profile_a, key="r3-002-a"
        )
        regular_b = _make_final_batch(
            session,
            organization.id,
            policy,
            employee_b,
            profile_b,
            key="r3-002-b",
            version=2,
            payroll_period="2026-03",
        )
        session.commit()
        organization_id = organization.id
        employee_a_id = employee_a.id
        employee_b_id = employee_b.id
        regular_a_id = regular_a.id
        regular_b_id = regular_b.id

    with Session(postgres_engine) as session:
        session.add(
            PayrollTaxStateSlot(
                org_id=organization_id,
                employee_id=employee_b_id,
                tax_year=2026,
                tax_month=8,
                regular_batch_id=regular_a_id,
                final_batch_id=regular_a_id,
            )
        )
        with pytest.raises(DBAPIError, match="same-employee regular payroll"):
            session.commit()

    with Session(postgres_engine) as session:
        slot_id = session.scalar(
            select(PayrollTaxStateSlot.id).where(
                PayrollTaxStateSlot.org_id == organization_id,
                PayrollTaxStateSlot.employee_id == employee_a_id,
                PayrollTaxStateSlot.regular_batch_id == regular_a_id,
            )
        )
        assert slot_id is not None

    with Session(postgres_engine) as session:
        slot = session.get(PayrollTaxStateSlot, slot_id)
        assert slot is not None
        slot.tax_month = 8
        with pytest.raises(DBAPIError, match="identity and regular batch are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        slot = session.get(PayrollTaxStateSlot, slot_id)
        assert slot is not None
        slot.final_batch_id = regular_b_id
        with pytest.raises(DBAPIError, match="same tax month"):
            session.commit()

    with Session(postgres_engine) as session:
        slot = session.get(PayrollTaxStateSlot, slot_id)
        assert slot is not None
        session.delete(slot)
        with pytest.raises(DBAPIError, match="tax state slot|requires exactly one"):
            session.commit()


def test_r2_005_final_vouchers_and_business_events_are_database_immutable(
    postgres_engine: object,
) -> None:
    with Session(postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R2-005",
        )
        event = _event(session, organization.id, "r2-005-empty")
        session.add(
            Voucher(
                org_id=organization.id,
                event_id=event.id,
                voucher_number="202508-r2-empty",
                posting_date=date(2026, 2, 28),
                description="empty",
                status="posted",
            )
        )
        with pytest.raises(DBAPIError, match="at least two balanced nonzero"):
            session.commit()

    with Session(postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R2-005-event",
        )
        original = _event(session, organization.id, "r2-005-original")
        original_voucher = create_voucher(
            session,
            event=original,
            posting_date=date(2026, 2, 28),
            description="正式事件",
            entries=[
                Entry(account_role="bank", debit_fen=1),
                Entry(account_role="service_revenue", credit_fen=1),
            ],
        )
        original.status = "posted"
        session.commit()
        original_id = original.id
        organization_id = organization.id

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, original_id)
        assert original is not None
        original.facts = {"tampered": True}
        with pytest.raises(DBAPIError, match="final business events are immutable"):
            session.flush()

    with Session(postgres_engine) as session:
        original = session.get(BusinessEvent, original_id)
        assert original is not None
        reversal = _event(session, organization_id, "r2-005-reversal")
        reversal.event_type = "reversal"
        reversal.facts = {"original_event_id": str(original.id)}
        create_voucher(
            session,
            event=reversal,
            posting_date=date(2026, 2, 28),
            description="正式冲正",
            entries=[
                Entry(account_role="service_revenue", debit_fen=1),
                Entry(account_role="bank", credit_fen=1),
            ],
            reversal_of=original_voucher,
        )
        reversal.status = "posted"
        original.status = "reversed"
        original.reversed_by_event_id = reversal.id
        session.commit()


def test_r2_006_voucher_line_composite_organization_foreign_keys(
    postgres_engine: object,
) -> None:
    with Session(postgres_engine) as session:
        organization_a = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R2-006-A",
        )
        organization_b = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R2-006-B",
        )
        event_a = _event(session, organization_a.id, "r2-006-event-a")
        event_b = _event(session, organization_b.id, "r2-006-event-b")
        voucher_a = Voucher(
            org_id=organization_a.id,
            event_id=event_a.id,
            voucher_number="202508-r2-006-a",
            posting_date=date(2026, 2, 28),
            description="draft",
            status="draft",
        )
        voucher_b = Voucher(
            org_id=organization_b.id,
            event_id=event_b.id,
            voucher_number="202508-r2-006-b",
            posting_date=date(2026, 2, 28),
            description="draft",
            status="draft",
        )
        foreign_counterparty = Counterparty(
            org_id=organization_b.id, kind="supplier", name="R2-006 foreign"
        )
        session.add_all([voucher_a, voucher_b, foreign_counterparty])
        session.flush()
        organization_a_id = organization_a.id
        organization_b_id = organization_b.id
        voucher_a_id = voucher_a.id
        voucher_b_id = voucher_b.id
        foreign_counterparty_id = foreign_counterparty.id
        session.commit()

    with Session(postgres_engine) as session:
        foreign_account = get_account_by_role(session, organization_b_id, "bank")
        session.add(
            VoucherLine(
                org_id=organization_a_id,
                voucher_id=voucher_a_id,
                line_number=1,
                account_id=foreign_account.id,
                debit_fen=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(postgres_engine) as session:
        local_account = get_account_by_role(session, organization_a_id, "bank")
        session.add(
            VoucherLine(
                org_id=organization_a_id,
                voucher_id=voucher_a_id,
                line_number=1,
                account_id=local_account.id,
                counterparty_id=foreign_counterparty_id,
                debit_fen=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()

    with Session(postgres_engine) as session:
        local_account = get_account_by_role(session, organization_a_id, "bank")
        session.add(
            VoucherLine(
                org_id=organization_a_id,
                voucher_id=voucher_b_id,
                line_number=1,
                account_id=local_account.id,
                debit_fen=1,
            )
        )
        with pytest.raises(IntegrityError):
            session.flush()


def test_r2_010_explicit_version_successors_allow_only_their_own_overlap(
    postgres_engine: object,
) -> None:
    with Session(postgres_engine) as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
            name="R2-010",
        )
        organization_id = organization.id
        policy = _policy(session, organization.id, version="r2-010-root")
        employee, profile = _employee_profile(session, organization.id, code="R2-010")
        profile_successor = EmployeePayrollProfileVersion(
            org_id=organization.id,
            employee_id=employee.id,
            supersedes_id=profile.id,
            effective_from=profile.effective_from,
            expense_role=profile.expense_role,
            social_insurance_base_fen=0,
            housing_fund_base_fen=0,
            resident_employee=True,
        )
        policy_successor = PayrollPolicyVersion(
            org_id=organization.id,
            region=policy.region,
            supersedes_id=policy.id,
            effective_from=policy.effective_from,
            version="r2-010-successor",
            source_url=policy.source_url,
            parameters=policy.parameters,
        )
        opening = PayrollOpeningState(
            org_id=organization.id,
            employee_id=employee.id,
            tax_year=2026,
            through_month=7,
        )
        session.add_all([profile_successor, policy_successor, opening])
        session.flush()
        session.add(
            PayrollOpeningState(
                org_id=organization.id,
                employee_id=employee.id,
                tax_year=2026,
                through_month=7,
                supersedes_id=opening.id,
            )
        )
        session.commit()

    with Session(postgres_engine) as session:
        employee = session.scalar(select(Employee).where(Employee.org_id == organization_id))
        assert employee is not None
        session.add(
            EmployeePayrollProfileVersion(
                org_id=organization_id,
                employee_id=employee.id,
                effective_from=date(2025, 7, 1),
                expense_role="payroll_management_expense",
                social_insurance_base_fen=0,
                housing_fund_base_fen=0,
                resident_employee=True,
            )
        )
        with pytest.raises(DBAPIError, match="NON_ANCESTOR_OVERLAP|explicit supersession"):
            session.commit()
