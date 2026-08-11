from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import (
    AccountingPeriodReviewFacts,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.models import (
    BankTransaction,
    Evidence,
    OpenItem,
    Organization,
    PayrollBatch,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RecordEventRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService


def payroll_parameters() -> dict[str, object]:
    return {
        "contribution_rules": [
            {
                "code": "pension",
                "base_kind": "social_insurance",
                "employee_rate": "0.08",
                "employer_rate": "0.16",
                "minimum_base_fen": 0,
                "maximum_base_fen": 10_000_000,
                "rounding_rule": "half_up",
            },
            {
                "code": "housing_fund",
                "base_kind": "housing_fund",
                "employee_rate": "0.07",
                "employer_rate": "0.07",
                "minimum_base_fen": 0,
                "maximum_base_fen": 10_000_000,
                "rounding_rule": "half_up",
            },
        ],
        "income_tax": {
            "version": "test-income-tax-2026",
            "primary_source_url": "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
            "legal_basis_source_url": "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "monthly_standard_deduction_fen": 500_000,
            "brackets": [
                {"upper_bound_fen": 3_600_000, "rate": "0.03", "quick_deduction_fen": 0},
                {"upper_bound_fen": None, "rate": "0.45", "quick_deduction_fen": 18_192_000},
            ],
        },
        "annual_bonus": {
            "version": "test-annual-bonus-2026",
            "primary_source_url": "https://m.mof.gov.cn/czxw/202308/t20230828_3904328.htm",
            "effective_from": "2026-01-01",
            "effective_to": "2027-12-31",
            "brackets": [
                {
                    "upper_monthly_average_fen": 3_000_000,
                    "rate": "0.03",
                    "quick_deduction_fen": 0,
                },
                {
                    "upper_monthly_average_fen": None,
                    "rate": "0.45",
                    "quick_deduction_fen": 18_192_000,
                },
            ],
        },
        "payment_targets": {
            "social_insurance": {"agency_code": "SOCIAL-01", "agency_name": "社保局"},
            "housing_fund": {"agency_code": "HOUSING-01", "agency_name": "公积金中心"},
            "individual_income_tax": {"agency_code": "TAX-01", "agency_name": "税务局"},
        },
    }


def register_payroll_facts(session: Session, organization: Organization) -> uuid.UUID:
    service = FinanceService(session)
    employee = service.register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="E-001",
            name="张三",
            employment_start_date=date(2026, 3, 1),
            status="active",
        )
    )
    employee_id = uuid.UUID(employee["employee_id"])
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
            RegisterPayrollPolicyVersionRequest(
                org_id=organization.id,
                region="测试地区",
                effective_from=date(2026, 1, 1),
                effective_to=date(2026, 12, 31),
                version="test-2026",
                source_url="https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
                parameters=payroll_parameters(),
            )
        )["status"]
        == "registered"
    )
    return employee_id


def preview_and_confirm(
    session: Session, organization: Organization
) -> tuple[FinanceService, object]:
    employee_id = register_payroll_facts(session, organization)
    service = FinanceService(session)
    preview = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "payroll-preview-1",
                "batch_kind": "regular",
                "payroll_period": "2026-03",
                "posting_date": "2026-03-05",
                "payment_date": "2026-03-05",
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
                ],
            }
        )
    )
    assert preview.status == "calculated"
    confirmed = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="payroll-confirm-1",
            confirmed_by="tester",
        )
    )
    assert confirmed.status == "posted"
    return service, confirmed


def test_payroll_preview_preserves_closed_period_error_without_calculated_batch(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ai_accounting.ledger.china_current_date", lambda: date(2026, 12, 31))
    organization = seed_organization(session, name="工资预览关闭期间")
    evidence = Evidence(
        org_id=organization.id,
        sha256="q" * 64,
        original_name="payroll-period.txt",
        source="test",
        size_bytes=1,
        storage_path="test/payroll-period.txt",
    )
    session.add(evidence)
    session.flush()
    period_service = AccountingPeriodService(session, current_date=date(2026, 12, 31))
    generated = period_service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-09",
            idempotency_key="payroll-period-generate",
            confirmed_by="reviewer",
            confirmation_note="生成工资月份",
            evidence_references=[evidence.id],
        )
    )
    close_facts = PreviewAccountingPeriodCloseRequest(
        org_id=organization.id,
        period_id=generated.period_id,
        closing_date=date(2026, 9, 30),
    )
    close_preview = period_service.preview_accounting_period_close(close_facts)
    close = period_service.confirm_accounting_period_close(
        ConfirmAccountingPeriodCloseRequest(
            **close_facts.model_dump(),
            calculation_hash=close_preview.calculation_hash,
            idempotency_key="payroll-period-close",
            review_facts=AccountingPeriodReviewFacts(
                voucher_completeness_reviewed=True,
                bank_reconciliation_reviewed=True,
                open_items_reviewed=True,
                payroll_and_statutory_items_reviewed=True,
                tax_items_reviewed=True,
                asset_and_borrowing_schedules_reviewed=True,
            ),
            confirmed_by="reviewer",
            confirmation_note="关闭工资月份",
            evidence_references=[evidence.id],
        )
    )
    assert close.status == "posted"
    employee_id = register_payroll_facts(session, organization)
    service = FinanceService(session)
    preview = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "period-payroll-preview",
                "batch_kind": "regular",
                "payroll_period": "2026-09",
                "posting_date": "2026-09-05",
                "payment_date": "2026-09-05",
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
                ],
            }
        )
    )
    assert preview.status == "rejected"
    assert preview.errors == ["ACCOUNTING_PERIOD_CLOSED"]
    assert preview.batch_id is None
    assert (
        session.scalar(
            select(PayrollBatch).where(
                PayrollBatch.org_id == organization.id,
                PayrollBatch.idempotency_key == "period-payroll-preview",
            )
        )
        is None
    )


def test_payroll_preview_preserves_not_generated_error_without_calculated_batch(
    session: Session,
) -> None:
    organization = seed_organization(session, name="工资预览未生成期间")
    employee_id = register_payroll_facts(session, organization)
    preview = FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "not-generated-payroll-preview",
                "batch_kind": "regular",
                "payroll_period": "2026-09",
                "posting_date": "2026-09-05",
                "payment_date": "2026-09-05",
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
                ],
            }
        )
    )

    assert preview.status == "rejected"
    assert preview.errors == ["ACCOUNTING_PERIOD_NOT_GENERATED"]
    assert preview.batch_id is None
    assert (
        session.scalar(
            select(PayrollBatch).where(
                PayrollBatch.org_id == organization.id,
                PayrollBatch.idempotency_key == "not-generated-payroll-preview",
            )
        )
        is None
    )


def add_bank_row(
    session: Session, organization: Organization, amount_fen: int, key: str
) -> BankTransaction:
    row = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint=(key * 64)[:64],
        booking_date=date(2026, 3, 5),
        amount_fen=amount_fen,
        currency="CNY",
        memo=key,
        source_sha256=("x" + key * 64)[:64],
    )
    session.add(row)
    session.flush()
    return row


def payment_request(
    organization: Organization,
    *,
    event_type: str,
    amount_fen: int,
    allocations: list[dict[str, object]],
    bank: BankTransaction,
    salary_withholdings: list[dict[str, object]] | None = None,
    key: str,
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "event_type": event_type,
            "business_dates": {
                "business_date": "2026-03-05",
                "payment_date": "2026-03-05",
                "posting_date": "2026-03-05",
            },
            "amounts": {
                "amount_fen": amount_fen,
                **(
                    {"expense_account_role": "general_expense"}
                    if event_type in {"expense_cash", "expense_payable"}
                    else {}
                ),
            },
            "allocations": allocations,
            "salary_withholding_allocations": salary_withholdings or [],
            "bank_transaction_references": [{"id": bank.id}],
        }
    )


def assert_balanced(session: Session, voucher_id: uuid.UUID) -> None:
    lines = session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)).all()
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines)


def test_payroll_accrual_is_gross_salary_and_payment_events_are_category_bound(
    session: Session, organization: Organization
) -> None:
    service, confirmed = preview_and_confirm(session, organization)
    assert_balanced(session, confirmed.voucher_id)
    accrual_voucher = session.get(Voucher, confirmed.voucher_id)
    salary_credit = next(
        line
        for line in accrual_voucher.lines
        if line.account.system_role == "employee_salary_payable"
    )
    assert salary_credit.credit_fen == 1_000_000
    items = session.scalars(
        select(OpenItem).where(OpenItem.source_event_id == confirmed.event_id)
    ).all()
    salary = next(item for item in items if item.payable_category == "salary")
    assert salary.original_amount_fen == 1_000_000
    by_category = {
        item.payable_category: item for item in items if item.payable_category != "salary"
    }
    assert by_category["employer_social"].original_amount_fen == 160_000
    assert by_category["employer_housing"].original_amount_fen == 70_000

    salary_bank = add_bank_row(session, organization, -839_500, "salary")
    salary_payment = service.record_event(
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
            key="salary-payment-1",
        )
    )
    assert salary_payment.status == "posted"
    assert_balanced(session, salary_payment.voucher_id)
    assert salary_payment.trace[-1]["stage"] == "entries_derived"
    replay = service.record_event(
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
            key="salary-payment-1",
        )
    )
    assert replay.data["idempotent_replay"] is True

    payment_items = session.scalars(
        select(OpenItem).where(OpenItem.source_event_id == salary_payment.event_id)
    ).all()
    social = [
        item
        for item in [*items, *payment_items]
        if item.payable_category in {"employer_social", "withheld_employee_social"}
    ]
    housing = [
        item
        for item in [*items, *payment_items]
        if item.payable_category in {"employer_housing", "withheld_employee_housing"}
    ]
    tax = next(item for item in payment_items if item.payable_category == "individual_income_tax")
    posted_statutory_payments = []
    for event_type, payment_items_for_type, amount, key in (
        ("social_insurance_payment", social, 240_000, "social-payment-1"),
        ("housing_fund_payment", housing, 140_000, "housing-payment-1"),
        ("individual_income_tax_payment", [tax], 10_500, "tax-payment-1"),
    ):
        bank = add_bank_row(session, organization, -amount, key)
        result = service.record_event(
            payment_request(
                organization,
                event_type=event_type,
                amount_fen=amount,
                allocations=[
                    {"open_item_id": item.id, "amount_fen": item.original_amount_fen}
                    for item in payment_items_for_type
                ],
                bank=bank,
                key=key,
            )
        )
        assert result.status == "posted"
        assert_balanced(session, result.voucher_id)
        posted_statutory_payments.append(result)

    for payment in posted_statutory_payments:
        reversed_payment = service.reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=payment.event_id,
                idempotency_key=f"reverse-{payment.event_id}",
                reason="测试冲正",
                posting_date=date(2026, 9, 6),
            )
        )
        assert reversed_payment.status == "posted"
        assert_balanced(session, reversed_payment.voucher_id)

    reversed_salary = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=salary_payment.event_id,
            idempotency_key="reverse-salary-payment",
            reason="测试冲正",
            posting_date=date(2026, 9, 6),
        )
    )
    assert reversed_salary.status == "posted"
    assert_balanced(session, reversed_salary.voucher_id)

    reversed_accrual = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=confirmed.event_id,
            idempotency_key="reverse-payroll-accrual",
            reason="测试工资计提冲正",
            posting_date=date(2026, 9, 6),
        )
    )
    assert reversed_accrual.status == "posted"
    assert_balanced(session, reversed_accrual.voucher_id)
    original_batch = session.get(PayrollBatch, confirmed.batch_id)
    reversal_batch = session.scalar(
        select(PayrollBatch).where(PayrollBatch.reversal_of_batch_id == original_batch.id)
    )
    assert original_batch.status == "reversed"
    assert reversal_batch.status == "posted"
    assert reversal_batch.business_event_id == reversed_accrual.event_id
    assert reversal_batch.calculation_hash != original_batch.calculation_hash
    assert reversal_batch.idempotency_key != original_batch.idempotency_key
    assert reversal_batch.version > original_batch.version
