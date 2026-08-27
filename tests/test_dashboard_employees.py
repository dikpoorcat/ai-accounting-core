from __future__ import annotations

from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.dashboard_employees import build_employees_data, load_employees_dashboard
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.models import (
    Account,
    AccountingPeriod,
    BusinessEvent,
    Counterparty,
    Employee,
    EmployeePayrollProfileVersion,
    Evidence,
    Organization,
    PayrollBatch,
    PayrollLine,
    PayrollPolicyVersion,
    Voucher,
    VoucherLine,
)


def test_employee_dashboard_loader_returns_empty_state_without_periods() -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        organization = seed_organization(
            session,
            taxpayer_identification_number="91330106MA1234567T",
            name="无期间员工看板测试公司",
            accounting_period_control_enabled=True,
        )

    payload = load_employees_dashboard(engine, org_id=organization.id)

    assert payload == {"schema_version": 1, "selected_period": None, "data": None}
    engine.dispose()


def _seed_period(session: Session) -> tuple[Organization, AccountingPeriod]:
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="员工看板测试公司",
        accounting_period_control_enabled=True,
    )
    evidence = Evidence(
        org_id=organization.id,
        sha256="7" * 64,
        original_name="期间生成依据.txt",
        source="test",
        size_bytes=1,
        storage_path="tests/dashboard-employees-period.txt",
    )
    session.add(evidence)
    session.flush()
    result = AccountingPeriodService(
        session, current_date=date(2026, 8, 27)
    ).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-02",
            idempotency_key="dashboard-employees-period-202602",
            confirmation_note="员工看板测试生成二月期间",
            evidence_references=[evidence.id],
        )
    )
    assert result.period_id is not None
    period = session.get(AccountingPeriod, result.period_id)
    assert period is not None
    return organization, period


def _add_employee(
    session: Session,
    organization: Organization,
    *,
    code: str,
    name: str,
    start_date: date,
    end_date: date | None = None,
) -> Employee:
    counterparty = Counterparty(
        org_id=organization.id,
        kind="employee",
        name=name,
        external_ref=code,
    )
    session.add(counterparty)
    session.flush()
    employee = Employee(
        org_id=organization.id,
        counterparty_id=counterparty.id,
        employee_code=code,
        name=name,
        employment_start_date=start_date,
        employment_end_date=end_date,
    )
    session.add(employee)
    session.flush()
    return employee


def _add_payroll(
    session: Session,
    organization: Organization,
    employee: Employee,
    *,
    gross_salary_fen: int,
    employer_social_insurance_fen: int,
) -> None:
    policy = PayrollPolicyVersion(
        org_id=organization.id,
        region="CN-330000",
        effective_from=date(2026, 1, 1),
        version="dashboard-employees-2026.1",
        source_url="https://www.chinatax.gov.cn/",
        parameters={},
    )
    profile = EmployeePayrollProfileVersion(
        org_id=organization.id,
        employee_id=employee.id,
        effective_from=date(2026, 1, 1),
        expense_role="payroll_management_expense",
        social_insurance_base_fen=500_000,
        housing_fund_base_fen=500_000,
        resident_employee=True,
    )
    session.add_all([policy, profile])
    session.flush()
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="dashboard-employees-payroll-event",
        event_type="payroll_accrual",
        status="posted",
        description="二月工资计提",
        facts={},
        business_date=date(2026, 2, 28),
        posting_date=date(2026, 2, 28),
        rule_trace=[],
    )
    session.add(event)
    session.flush()
    batch = PayrollBatch(
        org_id=organization.id,
        idempotency_key="dashboard-employees-payroll",
        batch_kind="regular",
        payroll_period="2026-02",
        version=1,
        status="posted",
        calculation_hash="8" * 64,
        calculation_input={"request": {}},
        calculation_trace=[],
        policy_snapshot={},
        policy_version_id=policy.id,
        posting_date=date(2026, 2, 28),
        payment_date=date(2026, 3, 5),
        business_event_id=event.id,
    )
    session.add(batch)
    session.flush()
    session.add(
        PayrollLine(
            org_id=organization.id,
            payroll_batch_id=batch.id,
            employee_id=employee.id,
            employee_payroll_profile_version_id=profile.id,
            wage_tax_declaration_state="declared",
            tax_reported_salary_fen=gross_salary_fen,
            employee_social_insurance_fen=10_000,
            employer_social_insurance_fen=employer_social_insurance_fen,
            individual_income_tax_fen=5_000,
            gross_salary_fen=gross_salary_fen,
            net_salary_fen=gross_salary_fen - 15_000,
        )
    )
    session.flush()

    expense = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "payroll_management_expense",
        )
    )
    bank = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "bank",
        )
    )
    assert expense is not None and bank is not None
    voucher = Voucher(
        org_id=organization.id,
        event_id=event.id,
        voucher_number="202602-0001",
        posting_date=date(2026, 2, 28),
        description=event.description,
        status="posted",
    )
    session.add(voucher)
    session.flush()
    company_cost_fen = gross_salary_fen + employer_social_insurance_fen
    session.add_all(
        [
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=1,
                account_id=expense.id,
                debit_fen=company_cost_fen,
                credit_fen=0,
                memo=event.description,
            ),
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=2,
                account_id=bank.id,
                debit_fen=0,
                credit_fen=company_cost_fen,
                memo=event.description,
            ),
        ]
    )
    session.flush()


def test_employee_dashboard_uses_explicit_payroll_dates_without_inference(
    session: Session,
) -> None:
    organization, period = _seed_period(session)
    _add_employee(
        session,
        organization,
        code="EMP-ACTIVE",
        name="核算中员工",
        start_date=date(2026, 1, 1),
    )
    _add_employee(
        session,
        organization,
        code="EMP-ENDED",
        name="已结束员工",
        start_date=date(2025, 1, 1),
        end_date=date(2026, 1, 31),
    )
    _add_employee(
        session,
        organization,
        code="EMP-FUTURE",
        name="尚未开始员工",
        start_date=date(2026, 3, 1),
    )

    data = build_employees_data(session, organization=organization, period=period)
    employees = data["employees"]

    assert employees["registered_count"] == 3
    assert employees["in_period_count"] == 1
    assert employees["without_payroll_count"] == 1
    assert employees["profile_missing_count"] == 1
    assert {item["code"]: item["period_state"] for item in employees["items"]} == {
        "EMP-ACTIVE": "in_period",
        "EMP-ENDED": "ended",
        "EMP-FUTURE": "not_started",
    }
    assert "不判断或证明劳动关系" in employees["identity_note"]
    assert data["workforce_cost"]["has_activity"] is False


def test_employee_dashboard_reconciles_payroll_details_to_ledger(session: Session) -> None:
    organization, period = _seed_period(session)
    employee = _add_employee(
        session,
        organization,
        code="EMP-001",
        name="测试员工",
        start_date=date(2026, 1, 1),
    )
    _add_payroll(
        session,
        organization,
        employee,
        gross_salary_fen=500_000,
        employer_social_insurance_fen=50_000,
    )

    data = build_employees_data(session, organization=organization, period=period)
    employees = data["employees"]
    employee_cost = data["workforce_cost"]["employee"]

    assert employees["payroll_count"] == 1
    assert employees["gross_salary_fen"] == 500_000
    assert employees["controlled_cost_fen"] == 550_000
    assert employees["ledger_cost_fen"] == 550_000
    assert employees["detail_reconciled"] is True
    assert employees["breakdown_available"] is True
    assert employees["items"][0]["declaration_state"] == "declared"
    assert employees["items"][0]["personal_deduction_fen"] == 15_000
    assert employees["items"][0]["net_salary_fen"] == 485_000
    assert employee_cost["gross_salary_fen"] == 500_000
    assert employee_cost["total_fen"] == 550_000
    assert data["workforce_cost"]["personal_labor"]["total_fen"] == 0
    assert data["workforce_cost"]["total_fen"] == 550_000


def test_employee_dashboard_marks_unexplained_payroll_ledger_cost_unavailable(
    session: Session,
) -> None:
    organization, period = _seed_period(session)
    expense = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "payroll_management_expense",
        )
    )
    bank = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "bank",
        )
    )
    assert expense is not None and bank is not None
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="dashboard-employees-unexplained-event",
        event_type="expense_cash",
        status="posted",
        description="缺少工资批次的职工薪酬费用",
        facts={},
        business_date=date(2026, 2, 20),
        posting_date=date(2026, 2, 20),
        rule_trace=[],
    )
    session.add(event)
    session.flush()
    voucher = Voucher(
        org_id=organization.id,
        event_id=event.id,
        voucher_number="202602-0002",
        posting_date=date(2026, 2, 20),
        description=event.description,
        status="posted",
    )
    session.add(voucher)
    session.flush()
    session.add_all(
        [
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=1,
                account_id=expense.id,
                debit_fen=1,
                credit_fen=0,
            ),
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=2,
                account_id=bank.id,
                debit_fen=0,
                credit_fen=1,
            ),
        ]
    )
    session.flush()

    data = build_employees_data(session, organization=organization, period=period)
    employees = data["employees"]

    assert employees["ledger_cost_fen"] == 1
    assert employees["breakdown_available"] is False
    assert employees["detail_reconciled"] is False
    assert "缺少受控工资批次关联" in employees["breakdown_reason"]
