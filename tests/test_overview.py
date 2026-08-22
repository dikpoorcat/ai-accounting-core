from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.models import (
    Account,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Counterparty,
    Employee,
    EmployeePayrollProfileVersion,
    Evidence,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollLine,
    PayrollPolicyVersion,
    Settlement,
    Voucher,
    VoucherLine,
)
from ai_accounting.overview import EVENT_PRESENTATIONS, build_overview_payload
from ai_accounting.overview_server import render_overview_document
from ai_accounting.schemas import EventType


def _seed_overview_month(session: Session) -> Organization:
    organization = seed_organization(
        session,
        name="经营概览测试公司",
        accounting_period_control_enabled=True,
    )
    evidence = Evidence(
        org_id=organization.id,
        sha256="d" * 64,
        original_name="股东投入确认.txt",
        source="test",
        size_bytes=10,
        storage_path="overview/owner.txt",
    )
    session.add(evidence)
    session.flush()
    generated = AccountingPeriodService(
        session,
        current_date=date(2026, 8, 17),
    ).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-02",
            idempotency_key="overview-period-202602",
            confirmation_note="经营概览单元测试生成二月期间",
            evidence_references=[evidence.id],
        )
    )
    assert generated.period_id is not None

    owner = Counterparty(
        org_id=organization.id,
        kind="owner",
        name="测试负责人",
    )
    session.add(owner)
    session.flush()
    bank = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "bank",
        )
    )
    capital = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "paid_in_capital",
        )
    )
    assert bank is not None and capital is not None
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="overview-owner-contribution",
        event_type="owner_contribution_received",
        status="posted",
        description="测试负责人投入启动资金",
        facts={},
        business_date=date(2026, 2, 9),
        posting_date=date(2026, 2, 9),
        rule_trace=[],
        evidence=[evidence],
    )
    session.add(event)
    session.flush()
    voucher = Voucher(
        org_id=organization.id,
        event_id=event.id,
        voucher_number="202602-0001",
        posting_date=date(2026, 2, 9),
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
                account_id=bank.id,
                counterparty_id=owner.id,
                debit_fen=10_000,
                credit_fen=0,
                memo=event.description,
            ),
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=2,
                account_id=capital.id,
                counterparty_id=owner.id,
                debit_fen=0,
                credit_fen=10_000,
                memo=event.description,
            ),
        ]
    )
    session.flush()
    return organization


def _add_test_voucher(
    session: Session,
    *,
    organization: Organization,
    number: str,
    event_type: str,
    description: str,
    facts: dict,
    lines: list[tuple[Account, Counterparty | None, int, int]],
    posting_date: date | None = None,
) -> tuple[BusinessEvent, Voucher]:
    posting_date = posting_date or date(2026, 2, int(number[-2:]))
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="overview-" + number,
        event_type=event_type,
        status="posted",
        description=description,
        facts=facts,
        business_date=posting_date,
        posting_date=posting_date,
        rule_trace=[],
    )
    session.add(event)
    session.flush()
    voucher = Voucher(
        org_id=organization.id,
        event_id=event.id,
        voucher_number=number,
        posting_date=posting_date,
        description=description,
        status="posted",
    )
    session.add(voucher)
    session.flush()
    session.add_all(
        [
            VoucherLine(
                org_id=organization.id,
                voucher_id=voucher.id,
                line_number=index,
                account_id=account.id,
                counterparty_id=counterparty.id if counterparty else None,
                debit_fen=debit_fen,
                credit_fen=credit_fen,
                memo=description,
            )
            for index, (account, counterparty, debit_fen, credit_fen) in enumerate(
                lines,
                start=1,
            )
        ]
    )
    session.flush()
    return event, voucher


def _add_controlled_payroll_accrual(
    session: Session,
    *,
    organization: Organization,
    employee: Employee,
    profile: EmployeePayrollProfileVersion,
    policy: PayrollPolicyVersion,
    accounts: dict[str, Account],
    number: str,
    posting_date: date,
    payroll_period: str,
    version: int,
    gross_salary_fen: int,
    employee_social_insurance_fen: int,
    employer_social_insurance_fen: int,
    reversal_of: PayrollBatch | None = None,
) -> PayrollBatch:
    net_salary_fen = gross_salary_fen - employee_social_insurance_fen
    total_expense_fen = gross_salary_fen + employer_social_insurance_fen
    if reversal_of is None:
        lines = [
            (accounts["expense"], employee.counterparty, total_expense_fen, 0),
            (accounts["salary"], employee.counterparty, 0, net_salary_fen),
            (accounts["withheld_social"], employee.counterparty, 0, employee_social_insurance_fen),
            (accounts["employer_social"], employee.counterparty, 0, employer_social_insurance_fen),
        ]
        event_type = "payroll_accrual"
    else:
        lines = [
            (accounts["expense"], employee.counterparty, 0, total_expense_fen),
            (accounts["salary"], employee.counterparty, net_salary_fen, 0),
            (accounts["withheld_social"], employee.counterparty, employee_social_insurance_fen, 0),
            (accounts["employer_social"], employee.counterparty, employer_social_insurance_fen, 0),
        ]
        event_type = "reversal"
    lines = [line for line in lines if line[2] or line[3]]
    event, _ = _add_test_voucher(
        session,
        organization=organization,
        number=number,
        event_type=event_type,
        description=f"{payroll_period} 职工薪酬",
        facts={},
        posting_date=posting_date,
        lines=lines,
    )
    if reversal_of is not None:
        reversal_of.status = "reversed"
    batch = PayrollBatch(
        org_id=organization.id,
        idempotency_key="payroll-" + number,
        batch_kind="regular",
        payroll_period=payroll_period,
        version=version,
        status="posted",
        calculation_hash=(number.replace("-", "") + "0" * 64)[:64],
        calculation_input={"request": {}},
        calculation_trace=[],
        policy_snapshot={"version": policy.version},
        policy_version_id=policy.id,
        posting_date=posting_date,
        payment_date=posting_date,
        business_event_id=event.id,
        reversal_of_batch_id=reversal_of.id if reversal_of else None,
    )
    session.add(batch)
    session.flush()
    session.add(
        PayrollLine(
            org_id=organization.id,
            payroll_batch_id=batch.id,
            employee_id=employee.id,
            employee_payroll_profile_version_id=profile.id,
            base_salary_fen=gross_salary_fen,
            employee_social_insurance_fen=employee_social_insurance_fen,
            employer_social_insurance_fen=employer_social_insurance_fen,
            gross_salary_fen=gross_salary_fen,
            net_salary_fen=net_salary_fen,
        )
    )
    session.flush()
    return batch


def test_overview_event_catalog_covers_public_and_internal_posting_types() -> None:
    assert {item.value for item in EventType} <= set(EVENT_PRESENTATIONS)
    assert {
        "payroll_accrual",
        "labor_remuneration_accrual",
        "unified_payout_run",
        "labor_withholding_tax_payment",
        "reversal",
    } <= set(EVENT_PRESENTATIONS)
    assert all("_" not in label for _, label in EVENT_PRESENTATIONS.values())


def test_overview_builds_balanced_month_without_internal_ids(session: Session) -> None:
    _seed_overview_month(session)

    payload = build_overview_payload(session)

    assert payload["schema_version"] == 2
    assert payload["company"] == "经营概览测试公司"
    assert payload["default_period"] == "2026-02"
    assert len(payload["months"]) == 1
    month = payload["months"][0]
    assert month["voucher_count"] == 1
    assert month["line_count"] == 2
    assert month["total_debit_fen"] == 10_000
    assert month["total_credit_fen"] == 10_000
    assert month["position"]["assets_fen"] == 10_000
    assert month["position"]["capital_fen"] == 10_000
    assert month["position"]["equation_valid"] is True
    assert month["checks"]["balanced"] is True
    assert month["represented_voucher_count"] == month["voucher_count"]
    assert len(month["activity_groups"]) == 1
    assert month["activity_groups"][0]["key"] == "financing_owner"
    assert month["activity_groups"][0]["event_count"] == 1
    assert month["vouchers"][0]["evidence"] == ["股东投入确认.txt"]
    assert month["vouchers"][0]["summary"] == "测试负责人投入启动资金"
    assert month["vouchers"][0]["list_summary"] == "测试负责人投入实收资本"
    assert month["activity_groups"][0]["rows"][0]["description"] == "测试负责人投入启动资金"
    serialized = json.dumps(payload, ensure_ascii=False)
    assert (
        re.search(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}",
            serialized,
        )
        is None
    )


def test_overview_subtracts_accumulated_depreciation_from_assets(
    session: Session,
) -> None:
    organization = _seed_overview_month(session)
    accounts = {
        role: session.scalar(
            select(Account).where(
                Account.org_id == organization.id,
                Account.system_role == role,
            )
        )
        for role in (
            "fixed_asset_cost",
            "accumulated_depreciation",
            "management_depreciation_expense",
            "paid_in_capital",
        )
    }
    assert all(accounts.values())
    fixed_asset = accounts["fixed_asset_cost"]
    accumulated_depreciation = accounts["accumulated_depreciation"]
    depreciation_expense = accounts["management_depreciation_expense"]
    capital = accounts["paid_in_capital"]
    assert fixed_asset and accumulated_depreciation and depreciation_expense and capital

    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0002",
        event_type="fixed_asset_depreciation",
        description="固定资产成本及本月折旧测试",
        facts={},
        lines=[
            (fixed_asset, None, 500_000, 0),
            (capital, None, 0, 500_000),
            (depreciation_expense, None, 111_897, 0),
            (accumulated_depreciation, None, 0, 111_897),
        ],
    )

    month = build_overview_payload(session)["months"][0]

    assert month["position"]["fixed_asset_fen"] == 500_000
    assert month["position"]["fixed_asset_net_fen"] == 388_103
    assert month["position"]["other_assets_fen"] == 0
    assert month["position"]["assets_fen"] == 398_103
    assert month["position"]["cumulative_result_fen"] == -111_897
    assert month["position"]["equation_valid"] is True


def test_overview_uses_current_bank_match_state_and_ignores_legacy_pointer(
    session: Session,
) -> None:
    organization = _seed_overview_month(session)
    event = session.scalar(
        select(BusinessEvent).where(
            BusinessEvent.org_id == organization.id,
            BusinessEvent.event_type == "owner_contribution_received",
        )
    )
    assert event is not None
    matched = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="a" * 64,
        booking_date=date(2026, 2, 9),
        amount_fen=10_000,
        source_sha256="b" * 64,
        matched_event_id=event.id,
    )
    stale = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="c" * 64,
        booking_date=date(2026, 2, 10),
        amount_fen=5_000,
        source_sha256="d" * 64,
        matched_event_id=event.id,
    )
    invalidator = BusinessEvent(
        org_id=organization.id,
        idempotency_key="overview-bank-match-invalidator",
        event_type="reversal",
        status="posted",
        description="使旧银行匹配失效",
        facts={},
        business_date=date(2026, 2, 10),
        posting_date=date(2026, 2, 10),
        rule_trace=[],
    )
    session.add_all([matched, stale, invalidator])
    session.flush()
    session.add_all(
        [
            BankTransactionMatch(
                org_id=organization.id,
                bank_transaction_id=matched.id,
                event_id=event.id,
            ),
            BankTransactionMatch(
                org_id=organization.id,
                bank_transaction_id=stale.id,
                event_id=event.id,
                invalidated_by_event_id=invalidator.id,
                invalidated_at=datetime.now(UTC),
            ),
        ]
    )
    session.flush()

    month = build_overview_payload(session)["months"][0]

    assert month["cash"]["ordinary_count"] == 2
    assert month["cash"]["matched_count"] == 1
    assert month["cash"]["unmatched_count"] == 1
    assert month["unmatched_bank_activity"]["count"] == 1
    assert month["unmatched_bank_activity"]["rows"][0]["amount_fen"] == 5_000
    assert month["validation"]["state"] == "attention"
    assert "1 笔流水待识别" in month["validation"]["summary"]


def test_overview_defaults_to_latest_generated_period_even_when_empty(
    session: Session,
) -> None:
    organization = _seed_overview_month(session)
    evidence = session.scalar(select(Evidence).where(Evidence.org_id == organization.id))
    assert evidence is not None
    AccountingPeriodService(session, current_date=date(2026, 8, 17)).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-03",
            idempotency_key="overview-period-202603",
            confirmation_note="经营概览单元测试生成三月空期间",
            evidence_references=[evidence.id],
        )
    )

    payload = build_overview_payload(session)

    assert payload["default_period"] == "2026-03"
    assert payload["months"][-1]["voucher_count"] == 0
    assert payload["months"][-1]["activity_groups"] == []


def test_overview_uses_employee_name_instead_of_internal_employee_code(
    session: Session,
) -> None:
    organization = _seed_overview_month(session)
    expense = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "general_expense",
        )
    )
    payable = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "employee_payable",
        )
    )
    assert expense is not None and payable is not None
    employee_party = Counterparty(
        org_id=organization.id,
        kind="employee",
        name="员工 EMP002",
        external_ref="EMP002",
    )
    session.add(employee_party)
    session.flush()
    session.add(
        Employee(
            org_id=organization.id,
            counterparty_id=employee_party.id,
            employee_code="EMP002",
            name="罗正宏",
            employment_start_date=date(2026, 1, 1),
            status="active",
        )
    )
    event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0002",
        event_type="payroll_accrual",
        description="2026年2月罗正宏工资计提完整说明",
        facts={},
        lines=[
            (expense, employee_party, 52_500, 0),
            (payable, employee_party, 0, 52_500),
        ],
    )
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=employee_party.id,
            source_event_id=event.id,
            item_type="payable",
            payable_category="salary",
            original_amount_fen=52_500,
            settled_amount_fen=0,
            status="open",
        )
    )
    session.flush()

    month = build_overview_payload(session)["months"][0]
    payroll_group = next(group for group in month["activity_groups"] if group["key"] == "payroll")
    payroll_voucher = next(
        voucher for voucher in month["vouchers"] if voucher["number"] == "202602-0002"
    )

    assert payroll_voucher["parties"] == ["罗正宏"]
    assert {line["party"] for line in payroll_voucher["lines"]} == {"罗正宏"}
    assert payroll_group["rows"][0]["party"] == "罗正宏"
    assert payroll_group["rows"][0]["description"] == "2026年2月罗正宏工资计提完整说明"
    assert month["open_items"]["payroll_payables"]["groups"][0]["party"] == "罗正宏"


def test_overview_splits_controlled_employee_compensation_without_double_counting(
    session: Session,
) -> None:
    organization = _seed_overview_month(session)
    evidence = session.scalar(select(Evidence).where(Evidence.org_id == organization.id))
    assert evidence is not None
    period_service = AccountingPeriodService(session, current_date=date(2026, 8, 17))
    for period_month in ("2026-03", "2026-04"):
        result = period_service.generate_accounting_period(
            GenerateAccountingPeriodRequest(
                org_id=organization.id,
                period_month=period_month,
                idempotency_key="overview-period-" + period_month.replace("-", ""),
                confirmation_note="经营概览职工薪酬拆分测试",
                evidence_references=[evidence.id],
            )
        )
        assert result.period_id is not None
    employee_party = Counterparty(
        org_id=organization.id,
        kind="employee",
        name="员工 EMP002",
        external_ref="EMP002",
    )
    session.add(employee_party)
    session.flush()
    employee = Employee(
        org_id=organization.id,
        counterparty_id=employee_party.id,
        employee_code="EMP002",
        name="罗正宏",
        employment_start_date=date(2026, 1, 1),
    )
    policy = PayrollPolicyVersion(
        org_id=organization.id,
        region="CN-330000",
        effective_from=date(2026, 1, 1),
        version="overview-2026.1",
        source_url="https://www.chinatax.gov.cn/",
        parameters={},
    )
    session.add_all([employee, policy])
    session.flush()
    profile = EmployeePayrollProfileVersion(
        org_id=organization.id,
        employee_id=employee.id,
        effective_from=date(2026, 1, 1),
        expense_role="payroll_management_expense",
        social_insurance_base_fen=0,
        housing_fund_base_fen=0,
    )
    session.add(profile)
    session.flush()
    roles = {
        "expense": "payroll_management_expense",
        "salary": "employee_salary_payable",
        "withheld_social": "withheld_employee_social_payable",
        "employer_social": "employer_social_payable",
        "bank": "bank",
    }
    accounts = {
        key: session.scalar(
            select(Account).where(
                Account.org_id == organization.id,
                Account.system_role == role,
            )
        )
        for key, role in roles.items()
    }
    assert all(accounts.values())
    typed_accounts = {key: value for key, value in accounts.items() if value is not None}
    _add_controlled_payroll_accrual(
        session,
        organization=organization,
        employee=employee,
        profile=profile,
        policy=policy,
        accounts=typed_accounts,
        number="202604-0001",
        posting_date=date(2026, 4, 1),
        payroll_period="2026-03",
        version=1,
        gross_salary_fen=105_000,
        employee_social_insurance_fen=105_000,
        employer_social_insurance_fen=264_000,
    )
    _add_test_voucher(
        session,
        organization=organization,
        number="202604-0002",
        event_type="social_insurance_payment",
        description="支付3月社保医保",
        facts={},
        posting_date=date(2026, 4, 2),
        lines=[
            (typed_accounts["withheld_social"], employee_party, 105_000, 0),
            (typed_accounts["employer_social"], employee_party, 264_000, 0),
            (typed_accounts["bank"], employee_party, 0, 369_000),
        ],
    )
    april_original = _add_controlled_payroll_accrual(
        session,
        organization=organization,
        employee=employee,
        profile=profile,
        policy=policy,
        accounts=typed_accounts,
        number="202604-0003",
        posting_date=date(2026, 4, 30),
        payroll_period="2026-04",
        version=1,
        gross_salary_fen=409_500,
        employee_social_insurance_fen=105_000,
        employer_social_insurance_fen=264_000,
    )
    _add_controlled_payroll_accrual(
        session,
        organization=organization,
        employee=employee,
        profile=profile,
        policy=policy,
        accounts=typed_accounts,
        number="202604-0004",
        posting_date=date(2026, 4, 30),
        payroll_period="2026-04",
        version=2,
        gross_salary_fen=409_500,
        employee_social_insurance_fen=105_000,
        employer_social_insurance_fen=264_000,
        reversal_of=april_original,
    )
    _add_controlled_payroll_accrual(
        session,
        organization=organization,
        employee=employee,
        profile=profile,
        policy=policy,
        accounts=typed_accounts,
        number="202604-0005",
        posting_date=date(2026, 4, 30),
        payroll_period="2026-04",
        version=3,
        gross_salary_fen=409_500,
        employee_social_insurance_fen=105_000,
        employer_social_insurance_fen=264_000,
    )
    session.flush()

    april = next(
        month for month in build_overview_payload(session)["months"] if month["key"] == "2026-04"
    )
    compensation = april["employee_compensation"]

    assert compensation["breakdown_available"] is True
    assert compensation["gross_salary_fen"] == 514_500
    assert compensation["employer_social_insurance_fen"] == 528_000
    assert compensation["total_fen"] == 1_042_500
    assert compensation["personal_withholding_fen"] == 210_000
    assert compensation["total_fen"] == (
        compensation["gross_salary_fen"]
        + compensation["employer_social_insurance_fen"]
        + compensation["employer_housing_fund_fen"]
    )
    assert [(item["payroll_period"], item["total_fen"]) for item in compensation["periods"]] == [
        ("2026-03", 369_000),
        ("2026-04", 673_500),
    ]
    assert compensation["periods"][1]["has_reversal"] is True


def test_overview_falls_back_when_employee_compensation_has_no_controlled_basis(
    session: Session,
) -> None:
    organization = _seed_overview_month(session)
    expense = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "payroll_management_expense",
        )
    )
    salary = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "employee_salary_payable",
        )
    )
    assert expense is not None and salary is not None
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0002",
        event_type="payroll_accrual",
        description="历史工资计提",
        facts={},
        lines=[
            (expense, None, 100_000, 0),
            (salary, None, 0, 100_000),
        ],
    )

    compensation = build_overview_payload(session)["months"][0]["employee_compensation"]

    assert compensation["has_activity"] is True
    assert compensation["breakdown_available"] is False
    assert compensation["total_fen"] == 100_000
    assert compensation["gross_salary_fen"] is None
    assert compensation["periods"] == []
    assert "明细不可拆" in compensation["reason"]


def test_overview_separates_employee_payables_and_refundable_deposits(
    session: Session,
) -> None:
    organization = _seed_overview_month(session)
    accounts = {
        role: session.scalar(
            select(Account).where(
                Account.org_id == organization.id,
                Account.system_role == role,
            )
        )
        for role in (
            "bank",
            "paid_in_capital",
            "general_expense",
            "employee_payable",
            "employee_receivable",
        )
    }
    assert all(accounts.values())
    bank = accounts["bank"]
    capital = accounts["paid_in_capital"]
    expense = accounts["general_expense"]
    employee_payable = accounts["employee_payable"]
    employee_receivable = accounts["employee_receivable"]
    assert bank and capital and expense and employee_payable and employee_receivable

    employee = Counterparty(org_id=organization.id, kind="employee", name="测试员工")
    deposit_holder = Counterparty(org_id=organization.id, kind="other", name="出租方")
    deposit_supplier = Counterparty(
        org_id=organization.id,
        kind="supplier",
        name="保证金对方",
    )
    settled_supplier = Counterparty(
        org_id=organization.id,
        kind="supplier",
        name="已退回保证金对方",
    )
    other_receivable_party = Counterparty(
        org_id=organization.id,
        kind="other",
        name="其他应收对方",
    )
    other_payable_party = Counterparty(
        org_id=organization.id,
        kind="supplier",
        name="其他应付对方",
    )
    session.add_all(
        [
            employee,
            deposit_holder,
            deposit_supplier,
            settled_supplier,
            other_receivable_party,
            other_payable_party,
        ]
    )
    session.flush()

    employee_event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0002",
        event_type="employee_reimbursement",
        description="员工垫付费用",
        facts={"derived": {"reimbursement_kind": "expense"}},
        lines=[
            (expense, employee, 10_000, 0),
            (employee_payable, employee, 0, 10_000),
        ],
    )
    employee_item = OpenItem(
        org_id=organization.id,
        counterparty_id=employee.id,
        source_event_id=employee_event.id,
        item_type="payable",
        original_amount_fen=10_000,
        settled_amount_fen=10_000,
        status="settled",
    )
    session.add(employee_item)
    session.flush()
    february_payment, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0010",
        event_type="employee_reimbursement_payment",
        description="2 月部分支付员工报销款",
        facts={},
        lines=[
            (employee_payable, employee, 2_500, 0),
            (bank, employee, 0, 2_500),
        ],
    )
    march_payment, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202603-0001",
        event_type="employee_reimbursement_payment",
        description="3 月支付剩余员工报销款",
        facts={},
        posting_date=date(2026, 3, 1),
        lines=[
            (employee_payable, employee, 7_500, 0),
            (bank, employee, 0, 7_500),
        ],
    )
    session.add_all(
        [
            Settlement(
                org_id=organization.id,
                open_item_id=employee_item.id,
                payment_event_id=february_payment.id,
                amount_fen=2_500,
            ),
            Settlement(
                org_id=organization.id,
                open_item_id=employee_item.id,
                payment_event_id=march_payment.id,
                amount_fen=7_500,
            ),
        ]
    )

    deposit_event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0003",
        event_type="refundable_deposit_paid",
        description="公对公支付可退保证金",
        facts={"derived": {"refundable_deposit_paid_fen": 20_000}},
        lines=[
            (employee_receivable, deposit_supplier, 20_000, 0),
            (bank, deposit_supplier, 0, 20_000),
        ],
    )
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=deposit_supplier.id,
            source_event_id=deposit_event.id,
            item_type="receivable",
            original_amount_fen=20_000,
            settled_amount_fen=5_000,
            status="partial",
        )
    )
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0004",
        event_type="refundable_deposit_return_received",
        description="部分收回可退保证金",
        facts={"derived": {"refundable_deposit_return_fen": 5_000}},
        lines=[
            (bank, deposit_supplier, 5_000, 0),
            (employee_receivable, deposit_supplier, 0, 5_000),
        ],
    )
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0005",
        event_type="employee_reimbursement",
        description="员工垫付可退租赁押金",
        facts={"derived": {"reimbursement_kind": "refundable_deposit"}},
        lines=[
            (employee_receivable, deposit_holder, 3_000, 0),
            (employee_payable, employee, 0, 3_000),
        ],
    )

    other_receivable_event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0006",
        event_type="unclassified_receivable",
        description="其他应收事项",
        facts={},
        lines=[
            (employee_receivable, other_receivable_party, 4_000, 0),
            (capital, other_receivable_party, 0, 4_000),
        ],
    )
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=other_receivable_party.id,
            source_event_id=other_receivable_event.id,
            item_type="receivable",
            original_amount_fen=4_000,
            settled_amount_fen=0,
            status="open",
        )
    )
    other_payable_event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0007",
        event_type="unclassified_payable",
        description="其他应付事项",
        facts={},
        lines=[
            (expense, other_payable_party, 5_000, 0),
            (employee_payable, other_payable_party, 0, 5_000),
        ],
    )
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=other_payable_party.id,
            source_event_id=other_payable_event.id,
            item_type="payable",
            original_amount_fen=5_000,
            settled_amount_fen=0,
            status="open",
        )
    )

    settled_event, _ = _add_test_voucher(
        session,
        organization=organization,
        number="202602-0008",
        event_type="refundable_deposit_paid",
        description="支付后已全额收回的保证金",
        facts={"derived": {"refundable_deposit_paid_fen": 7_000}},
        lines=[
            (employee_receivable, settled_supplier, 7_000, 0),
            (bank, settled_supplier, 0, 7_000),
        ],
    )
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=settled_supplier.id,
            source_event_id=settled_event.id,
            item_type="receivable",
            original_amount_fen=7_000,
            settled_amount_fen=7_000,
            status="settled",
        )
    )
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0009",
        event_type="refundable_deposit_return_received",
        description="全额收回可退保证金",
        facts={"derived": {"refundable_deposit_return_fen": 7_000}},
        lines=[
            (bank, settled_supplier, 7_000, 0),
            (employee_receivable, settled_supplier, 0, 7_000),
        ],
    )
    session.flush()

    month = build_overview_payload(session)["months"][0]
    employee_items = month["open_items"]["employee_payables"]
    deposits = month["open_items"]["refundable_deposit_receivables"]
    other_receivables = month["open_items"]["other_receivables"]
    other_payables = month["open_items"]["other_payables"]

    assert (employee_items["count"], employee_items["outstanding_fen"]) == (1, 7_500)
    assert (deposits["count"], deposits["outstanding_fen"]) == (2, 18_000)
    assert {group["party"] for group in deposits["groups"]} == {
        "出租方",
        "保证金对方",
    }
    assert (other_receivables["count"], other_receivables["outstanding_fen"]) == (
        1,
        4_000,
    )
    assert (other_payables["count"], other_payables["outstanding_fen"]) == (0, 0)
    supplier_payables = month["open_items"]["supplier_payables"]
    assert (supplier_payables["count"], supplier_payables["outstanding_fen"]) == (1, 5_000)
    assert month["open_items"]["receivable_fen"] == 22_000
    assert month["open_items"]["payable_fen"] == 12_500
    assert month["represented_voucher_count"] == month["voucher_count"]
    fund_group = next(
        group for group in month["activity_groups"] if group["key"] == "fund_movement"
    )
    assert fund_group["event_count"] == 5
    assert "员工垫付可退保证金" in {row["title"] for row in fund_group["rows"]}
    other_group = next(group for group in month["activity_groups"] if group["key"] == "other")
    assert other_group["event_count"] == 2
    assert {row["title"] for row in other_group["rows"]} == {"其他业务"}
    assert "已退回保证金对方" not in {group["party"] for group in deposits["groups"]}


def test_overview_document_embeds_data_without_script_breakout(session: Session) -> None:
    _seed_overview_month(session)
    payload = build_overview_payload(session)
    payload["company"] = "</script><script>alert('x')</script>"
    payload["months"][0]["position"]["assets_fen"] = 9_007_199_254_740_993

    document = render_overview_document(payload)

    assert document.startswith("<!doctype html>")
    assert "__OVERVIEW_DATA__" not in document
    assert "</script><script>alert" not in document
    assert "\\u003c/script>\\u003cscript>alert" in document
    assert "本月发生了什么" in document
    assert "本月一句话" in document
    assert "待识别资金动向" in document
    assert "本月职工薪酬" in document
    assert "公司承担社保医保" in document
    assert "不会在付款时再次计入费用" in document
    assert "借方合计" in document
    assert "voucher-ledger" in document
    assert "分录摘要" not in document
    assert "期末待收 / 待付" in document
    assert "期末往来事项" in document
    assert 'id="long-assets-card"' in document
    assert 'aria-label="查看期末待收待付事项"' in document
    assert 'focusSection(byId("activity-detail"))' not in document
    assert "BigInt" in document
    assert '"total_debit_fen":"10000"' in document
    assert '"assets_fen":"9007199254740993"' in document
