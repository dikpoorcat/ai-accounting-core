from __future__ import annotations

import json
import re
from datetime import UTC, date, datetime
from decimal import Decimal

import pytest
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
    LaborRemunerationBatch,
    LaborRemunerationEventLink,
    LaborRemunerationLine,
    LaborRemunerationTaxPolicyVersion,
    LaborServicePerson,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollLine,
    PayrollPolicyVersion,
    Settlement,
    UnifiedPayoutRun,
    UnifiedPayoutRunItem,
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
            tax_reported_salary_fen=gross_salary_fen,
            employee_social_insurance_fen=employee_social_insurance_fen,
            employer_social_insurance_fen=employer_social_insurance_fen,
            gross_salary_fen=gross_salary_fen,
            net_salary_fen=net_salary_fen,
        )
    )
    session.flush()
    return batch


def _add_controlled_labor_accrual(
    session: Session,
    *,
    organization: Organization,
    labor_person: LaborServicePerson,
    policy: LaborRemunerationTaxPolicyVersion,
    accounts: dict[str, Account],
    number: str,
    posting_date: date,
    remuneration_period: str,
    fixed_fee_fen: int,
    commission_fen: int,
    withholding_tax_fen: int,
) -> tuple[LaborRemunerationBatch, LaborRemunerationLine, OpenItem]:
    gross_fen = fixed_fee_fen + commission_fen
    counterparty = session.get(Counterparty, labor_person.counterparty_id)
    assert counterparty is not None
    event, _ = _add_test_voucher(
        session,
        organization=organization,
        number=number,
        event_type="labor_remuneration_accrual",
        description=f"{remuneration_period} 非员工个人劳务计提",
        facts={},
        posting_date=posting_date,
        lines=[
            (accounts["expense"], counterparty, gross_fen, 0),
            (accounts["payable"], counterparty, 0, gross_fen),
        ],
    )
    batch = LaborRemunerationBatch(
        org_id=organization.id,
        idempotency_key="labor-" + number,
        request_payload_hash=("request" + number.replace("-", "") + "0" * 64)[:64],
        remuneration_period=remuneration_period,
        status="posted",
        calculation_hash=("calculation" + number.replace("-", "") + "0" * 64)[:64],
        calculation_input={"request": {}},
        calculation_trace=[],
        policy_version_id=policy.id,
        policy_snapshot={"version": policy.version},
        business_date=posting_date,
        posting_date=posting_date,
        planned_payment_date=posting_date,
        business_event_id=event.id,
    )
    session.add(batch)
    session.flush()
    line = LaborRemunerationLine(
        org_id=organization.id,
        batch_id=batch.id,
        labor_person_id=labor_person.id,
        counterparty_id=labor_person.counterparty_id,
        service_start_date=posting_date.replace(day=1),
        service_end_date=posting_date,
        fixed_fee_fen=fixed_fee_fen,
        commission_fen=commission_fen,
        gross_remuneration_fen=gross_fen,
        expense_role=accounts["expense"].system_role,
        tax_identity="resident",
        income_grouping="continuous_monthly",
        is_full_time_student=False,
        expense_deduction_fen=0,
        taxable_income_fen=gross_fen,
        withholding_rate=Decimal("0"),
        quick_deduction_fen=0,
        withholding_tax_fen=withholding_tax_fen,
        net_payment_fen=gross_fen - withholding_tax_fen,
        external_declaration_status="not_due",
    )
    open_item = OpenItem(
        org_id=organization.id,
        counterparty_id=labor_person.counterparty_id,
        source_event_id=event.id,
        item_type="payable",
        payable_category="labor_remuneration",
        original_amount_fen=gross_fen,
        settled_amount_fen=0,
        status="open",
    )
    session.add_all(
        [
            line,
            open_item,
            LaborRemunerationEventLink(
                org_id=organization.id,
                event_id=event.id,
                batch_id=batch.id,
                link_kind="accrual",
            ),
        ]
    )
    session.flush()
    return batch, line, open_item


def _add_controlled_labor_reversal(
    session: Session,
    *,
    organization: Organization,
    batch: LaborRemunerationBatch,
    line: LaborRemunerationLine,
    accounts: dict[str, Account],
    number: str,
    posting_date: date,
) -> None:
    original = session.get(BusinessEvent, batch.business_event_id)
    counterparty = session.get(Counterparty, line.counterparty_id)
    assert original is not None and counterparty is not None
    reversal, _ = _add_test_voucher(
        session,
        organization=organization,
        number=number,
        event_type="reversal",
        description=f"冲正 {batch.remuneration_period} 非员工个人劳务计提",
        facts={},
        posting_date=posting_date,
        lines=[
            (accounts["payable"], counterparty, line.gross_remuneration_fen, 0),
            (accounts["expense"], counterparty, 0, line.gross_remuneration_fen),
        ],
    )
    original.status = "reversed"
    original.reversed_by_event_id = reversal.id
    batch.status = "reversed"
    session.add(
        LaborRemunerationEventLink(
            org_id=organization.id,
            event_id=reversal.id,
            batch_id=batch.id,
            source_payment_event_id=original.id,
            link_kind="reversal",
        )
    )
    session.flush()


def _add_gross_labor_payout_without_withholding(
    session: Session,
    *,
    organization: Organization,
    line: LaborRemunerationLine,
    open_item: OpenItem,
    accounts: dict[str, Account],
    number: str,
    posting_date: date,
) -> None:
    counterparty = session.get(Counterparty, line.counterparty_id)
    assert counterparty is not None
    event, _ = _add_test_voucher(
        session,
        organization=organization,
        number=number,
        event_type="unified_payout_run",
        description="个人劳务按毛额支付，未代扣个人所得税",
        facts={},
        posting_date=posting_date,
        lines=[
            (accounts["payable"], counterparty, line.gross_remuneration_fen, 0),
            (accounts["bank"], counterparty, 0, line.gross_remuneration_fen),
        ],
    )
    bank_transaction = BankTransaction(
        org_id=organization.id,
        bank_account_code=accounts["bank"].code,
        fingerprint=(number.replace("-", "") + "4" * 64)[:64],
        booking_date=posting_date,
        amount_fen=-line.gross_remuneration_fen,
        source_sha256=(number.replace("-", "") + "5" * 64)[:64],
        matched_event_id=event.id,
    )
    session.add(bank_transaction)
    session.flush()
    run = UnifiedPayoutRun(
        org_id=organization.id,
        idempotency_key="payout-" + number,
        request_payload_hash=(number.replace("-", "") + "6" * 64)[:64],
        status="posted",
        calculation_hash=(number.replace("-", "") + "7" * 64)[:64],
        calculation_input={"request": {}},
        calculation_trace=[],
        bank_account_code=accounts["bank"].code,
        bank_transaction_id=bank_transaction.id,
        business_date=posting_date,
        payment_date=posting_date,
        posting_date=posting_date,
        gross_total_fen=line.gross_remuneration_fen,
        withholding_total_fen=0,
        net_total_fen=line.gross_remuneration_fen,
        business_event_id=event.id,
        confirmed_at=datetime.now(UTC),
    )
    session.add(run)
    session.flush()
    session.add(
        UnifiedPayoutRunItem(
            org_id=organization.id,
            payout_run_id=run.id,
            item_kind="labor",
            source_open_item_id=open_item.id,
            labor_line_id=line.id,
            counterparty_id=line.counterparty_id,
            settlement_mode="gross_paid_without_withholding",
            gross_amount_fen=line.gross_remuneration_fen,
            individual_income_tax_fen=0,
            theoretical_individual_income_tax_fen=line.withholding_tax_fen,
            unwithheld_individual_income_tax_fen=line.withholding_tax_fen,
            net_amount_fen=line.gross_remuneration_fen,
            withholding_components={},
        )
    )
    open_item.settled_amount_fen = open_item.original_amount_fen
    open_item.status = "settled"
    session.flush()


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
    assert month["workforce_cost"]["has_activity"] is False
    assert month["workforce_cost"]["total_fen"] == 0
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


def test_overview_combines_controlled_workforce_costs_without_double_counting(
    session: Session,
) -> None:
    organization = _seed_overview_month(session)
    evidence = session.scalar(select(Evidence).where(Evidence.org_id == organization.id))
    assert evidence is not None
    period_service = AccountingPeriodService(session, current_date=date(2026, 8, 17))
    for period_month in ("2026-03", "2026-04", "2026-05"):
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

    labor_party = Counterparty(
        org_id=organization.id,
        kind="labor_person",
        name="杨彦",
        external_ref="LAB001",
    )
    labor_policy = LaborRemunerationTaxPolicyVersion(
        code="overview-labor-tax",
        version="2026.1",
        effective_from=date(2026, 1, 1),
        primary_source_url="https://www.chinatax.gov.cn/",
        invoice_withholding_source_url="https://www.chinatax.gov.cn/",
        legal_filing_source_url="https://www.chinatax.gov.cn/",
        parameters={},
    )
    session.add_all([labor_party, labor_policy])
    session.flush()
    labor_person = LaborServicePerson(
        org_id=organization.id,
        counterparty_id=labor_party.id,
        person_code="LAB001",
        name="杨彦",
        relationship_start_date=date(2026, 1, 1),
        status="active",
        idempotency_key="overview-labor-person",
        request_payload_hash="1" * 64,
    )
    session.add(labor_person)
    session.flush()
    labor_accounts = {
        key: session.scalar(
            select(Account).where(
                Account.org_id == organization.id,
                Account.system_role == role,
            )
        )
        for key, role in {
            "expense": "labor_management_expense",
            "payable": "labor_remuneration_payable",
            "bank": "bank",
        }.items()
    }
    assert all(labor_accounts.values())
    typed_labor_accounts = {
        key: value for key, value in labor_accounts.items() if value is not None
    }
    _, march_labor_line, march_labor_open_item = _add_controlled_labor_accrual(
        session,
        organization=organization,
        labor_person=labor_person,
        policy=labor_policy,
        accounts=typed_labor_accounts,
        number="202604-0010",
        posting_date=date(2026, 4, 1),
        remuneration_period="2026-03",
        fixed_fee_fen=966_200,
        commission_fen=0,
        withholding_tax_fen=193_240,
    )
    _add_gross_labor_payout_without_withholding(
        session,
        organization=organization,
        line=march_labor_line,
        open_item=march_labor_open_item,
        accounts=typed_labor_accounts,
        number="202604-0011",
        posting_date=date(2026, 4, 4),
    )
    april_labor_original, april_labor_line, _ = _add_controlled_labor_accrual(
        session,
        organization=organization,
        labor_person=labor_person,
        policy=labor_policy,
        accounts=typed_labor_accounts,
        number="202604-0012",
        posting_date=date(2026, 4, 30),
        remuneration_period="2026-04",
        fixed_fee_fen=900_000,
        commission_fen=271_085,
        withholding_tax_fen=234_217,
    )
    _add_controlled_labor_reversal(
        session,
        organization=organization,
        batch=april_labor_original,
        line=april_labor_line,
        accounts=typed_labor_accounts,
        number="202604-0013",
        posting_date=date(2026, 4, 30),
    )
    _, april_replacement_line, april_replacement_open_item = _add_controlled_labor_accrual(
        session,
        organization=organization,
        labor_person=labor_person,
        policy=labor_policy,
        accounts=typed_labor_accounts,
        number="202604-0014",
        posting_date=date(2026, 4, 30),
        remuneration_period="2026-04",
        fixed_fee_fen=900_000,
        commission_fen=271_085,
        withholding_tax_fen=234_217,
    )
    before_may_payment = next(
        month for month in build_overview_payload(session)["months"] if month["key"] == "2026-04"
    )["workforce_cost"]["personal_labor"]
    assert before_may_payment["withholding_status"] == "partially_settled"
    assert before_may_payment["actual_withholding_tax_fen"] == 0
    assert before_may_payment["unwithheld_tax_fen"] == 193_240
    assert before_may_payment["pending_theoretical_tax_fen"] == 234_217
    _add_gross_labor_payout_without_withholding(
        session,
        organization=organization,
        line=april_replacement_line,
        open_item=april_replacement_open_item,
        accounts=typed_labor_accounts,
        number="202605-0001",
        posting_date=date(2026, 5, 6),
    )
    session.flush()

    payload = build_overview_payload(session)
    april = next(month for month in payload["months"] if month["key"] == "2026-04")
    workforce = april["workforce_cost"]
    compensation = workforce["employee"]
    personal_labor = workforce["personal_labor"]

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
    assert personal_labor["breakdown_available"] is True
    assert personal_labor["gross_remuneration_fen"] == 2_137_285
    assert personal_labor["total_fen"] == 2_137_285
    assert personal_labor["theoretical_withholding_tax_fen"] == 427_457
    assert personal_labor["actual_withholding_tax_fen"] == 0
    assert personal_labor["unwithheld_tax_fen"] == 427_457
    assert personal_labor["pending_theoretical_tax_fen"] == 0
    assert personal_labor["withholding_status"] == "not_withheld"
    assert personal_labor["settlement_modes"] == ["gross_paid_without_withholding"]
    assert [
        (item["remuneration_period"], item["total_fen"]) for item in personal_labor["periods"]
    ] == [
        ("2026-03", 966_200),
        ("2026-04", 1_171_085),
    ]
    assert personal_labor["periods"][1]["has_reversal"] is True
    assert workforce["total_fen"] == 3_179_785
    assert workforce["total_fen"] == compensation["total_fen"] + personal_labor["total_fen"]
    document = render_overview_document(payload)
    assert "劳务批次计算的个人所得税" not in document
    assert "劳务报酬已按毛额支付，实际未代扣个人所得税" in document
    assert '"actual_withholding_tax_fen":"0"' in document


@pytest.mark.parametrize(
    ("expense_role", "payable_role", "event_type", "active_key", "inactive_key"),
    [
        (
            "payroll_management_expense",
            "employee_salary_payable",
            "payroll_accrual",
            "employee",
            "personal_labor",
        ),
        (
            "labor_management_expense",
            "labor_remuneration_payable",
            "labor_remuneration_accrual",
            "personal_labor",
            "employee",
        ),
    ],
)
def test_overview_handles_only_one_workforce_cost_kind(
    session: Session,
    expense_role: str,
    payable_role: str,
    event_type: str,
    active_key: str,
    inactive_key: str,
) -> None:
    organization = _seed_overview_month(session)
    expense = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == expense_role,
        )
    )
    payable = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == payable_role,
        )
    )
    assert expense is not None and payable is not None
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0002",
        event_type=event_type,
        description="单一用工成本类型测试",
        facts={},
        lines=[
            (expense, None, 100_000, 0),
            (payable, None, 0, 100_000),
        ],
    )

    workforce = build_overview_payload(session)["months"][0]["workforce_cost"]

    assert workforce["has_activity"] is True
    assert workforce["total_fen"] == 100_000
    assert workforce[active_key]["has_activity"] is True
    assert workforce[active_key]["total_fen"] == 100_000
    assert workforce[inactive_key]["has_activity"] is False
    assert workforce[inactive_key]["total_fen"] == 0


def test_overview_falls_back_when_workforce_costs_have_no_controlled_basis(
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
    labor_expense = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "labor_management_expense",
        )
    )
    labor_payable = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "labor_remuneration_payable",
        )
    )
    assert all((expense, salary, labor_expense, labor_payable))
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
    _add_test_voucher(
        session,
        organization=organization,
        number="202602-0003",
        event_type="labor_remuneration_accrual",
        description="历史个人劳务计提",
        facts={},
        lines=[
            (labor_expense, None, 250_000, 0),
            (labor_payable, None, 0, 250_000),
        ],
    )

    workforce = build_overview_payload(session)["months"][0]["workforce_cost"]
    compensation = workforce["employee"]
    personal_labor = workforce["personal_labor"]

    assert compensation["has_activity"] is True
    assert compensation["breakdown_available"] is False
    assert compensation["total_fen"] == 100_000
    assert compensation["gross_salary_fen"] is None
    assert compensation["periods"] == []
    assert "明细不可拆" in compensation["reason"]
    assert personal_labor["has_activity"] is True
    assert personal_labor["breakdown_available"] is False
    assert personal_labor["total_fen"] == 250_000
    assert personal_labor["gross_remuneration_fen"] is None
    assert personal_labor["periods"] == []
    assert "明细不可拆" in personal_labor["reason"]
    assert workforce["total_fen"] == 350_000


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
    assert "本月账面损益" in document
    assert "按业务归属月，不按收付款月" in document
    assert "本月收入费用差额" not in document
    assert "待识别资金动向" in document
    assert "本月用工成本" in document
    assert "正式员工" in document
    assert "非员工个人劳务" in document
    assert "个人劳务报酬/佣金毛额" in document
    assert "公司承担社保医保" in document
    assert "不会在付款时再次计入用工成本" in document
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
