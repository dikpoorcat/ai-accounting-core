from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from conftest import import_test_bank_transaction
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.accounting_period_schemas import (
    AccountingPeriodReviewFacts,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.financial_statement_schemas import (
    ConfirmEnterpriseIncomeTaxQuarterRequest,
    EnterpriseIncomeTaxTreatment,
)
from ai_accounting.financial_statements import FinancialStatementService
from ai_accounting.models import (
    AuditLog,
    BankTransaction,
    Employee,
    EmployeePayrollProfileVersion,
    Evidence,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollLine,
    PayrollTaxStateSlot,
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


def test_register_employee_can_add_tax_withholding_start_date_once(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    initial = service.register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="E-TAX-START-LATER",
            name="后补扣缴日期员工",
            employment_start_date=date(2026, 7, 1),
            tax_withholding_start_date=None,
        )
    )
    employee_id = uuid.UUID(initial["employee_id"])

    enriched_request = RegisterEmployeeRequest(
        org_id=organization.id,
        employee_code="E-TAX-START-LATER",
        name="后补扣缴日期员工",
        employment_start_date=date(2026, 7, 1),
        tax_withholding_start_date=date(2026, 7, 1),
    )
    enriched = service.register_employee(enriched_request)
    assert enriched == {
        "status": "registered",
        "employee_id": str(employee_id),
        "tax_withholding_start_date_registered": True,
    }
    assert session.get(Employee, employee_id).tax_withholding_start_date == date(2026, 7, 1)
    assert (
        session.scalar(
            select(AuditLog).where(
                AuditLog.org_id == organization.id,
                AuditLog.action == "payroll_employee_tax_withholding_start_registered",
            )
        )
        is not None
    )

    replay = service.register_employee(enriched_request)
    assert replay["idempotent_replay"] is True

    conflicting = service.register_employee(
        enriched_request.model_copy(update={"tax_withholding_start_date": date(2026, 8, 1)})
    )
    assert conflicting == {
        "status": "rejected",
        "errors": ["EMPLOYEE_CODE_ALREADY_EXISTS"],
    }


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
            tax_withholding_start_date=date(2026, 3, 1),
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
                        "tax_reported_salary_fen": 1_000_000,
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
        )
    )
    assert confirmed.status == "posted"
    return service, confirmed


def test_payroll_preview_preserves_closed_period_error_without_calculated_batch(
    session: Session,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ai_accounting.ledger.china_current_date", lambda: date(2026, 12, 31))
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="工资预览关闭期间",
    )
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
            confirmation_note="生成工资月份",
            evidence_references=[evidence.id],
        )
    )
    configured_at = datetime.now(UTC)
    set_committed_value(
        organization,
        "bank_reconciliation_scope_current_action_id",
        uuid.uuid4(),
    )
    set_committed_value(
        organization,
        "bank_reconciliation_scope_confirmed_at",
        configured_at,
    )
    income_tax = FinancialStatementService(session).confirm_enterprise_income_tax(
        ConfirmEnterpriseIncomeTaxQuarterRequest(
            org_id=organization.id,
            year=2026,
            quarter=3,
            treatment=EnterpriseIncomeTaxTreatment.ZERO,
            amount_fen=0,
            idempotency_key="payroll-period-q3-income-tax",
            confirmation_note="明确确认第三季度企业所得税费用为零",
            evidence_references=[evidence.id],
        )
    )
    assert income_tax.status == "posted"
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
            management_commentary_context_hash=close_preview.data[
                "assistant_review_checklist"
            ]["management_commentary"]["context_hash"],
            management_commentary="九月经营情况已基于关账上下文完成分析。",
            idempotency_key="payroll-period-close",
            review_facts=AccountingPeriodReviewFacts(
                voucher_completeness_reviewed=True,
                bank_reconciliation_reviewed=True,
                open_items_reviewed=True,
                payroll_and_statutory_items_reviewed=True,
                tax_items_reviewed=True,
                asset_and_borrowing_schedules_reviewed=True,
            ),
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
                        "tax_reported_salary_fen": 1_000_000,
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
    organization = seed_organization(
        session,
        taxpayer_identification_number="91330106MA1234567T",
        name="工资预览未生成期间",
    )
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
                        "tax_reported_salary_fen": 1_000_000,
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
    session: Session,
    organization: Organization,
    amount_fen: int,
    key: str,
    *,
    booking_date: date = date(2026, 3, 5),
) -> BankTransaction:
    if session.get_bind().dialect.name == "postgresql":
        return import_test_bank_transaction(
            session,
            organization,
            amount_fen=amount_fen,
            key=key,
            booking_date=booking_date,
        )
    row = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint=(key * 64)[:64],
        booking_date=booking_date,
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
    bank: BankTransaction | None,
    salary_withholdings: list[dict[str, object]] | None = None,
    key: str,
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "event_type": event_type,
            "bank_account_code": "1002",
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
            **({"bank_transaction_references": [{"id": bank.id}]} if bank is not None else {}),
        }
    )


def assert_balanced(session: Session, voucher_id: uuid.UUID) -> None:
    lines = session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)).all()
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines)


def test_zero_tax_reported_salary_posts_company_borne_social_in_payroll_period(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    employee_result = service.register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="FINAL-WAGE-001",
            name="最终工资员工",
            employment_start_date=date(2026, 2, 1),
            tax_withholding_start_date=date(2026, 3, 1),
        )
    )
    employee_id = uuid.UUID(employee_result["employee_id"])
    assert (
        service.register_employee_payroll_profile_version(
            RegisterEmployeePayrollProfileVersionRequest(
                org_id=organization.id,
                employee_id=employee_id,
                effective_from=date(2026, 3, 1),
                expense_role="payroll_management_expense",
                social_insurance_base_fen=500_000,
                housing_fund_base_fen=0,
                resident_employee=True,
            )
        )["status"]
        == "registered"
    )
    parameters = payroll_parameters()
    parameters["employee_contribution_shortfall_treatment"] = "employer_borne"
    parameters["contribution_rules"] = [
        {
            "code": code,
            "base_kind": "social_insurance",
            "employee_rate": employee_rate,
            "employer_rate": employer_rate,
            "minimum_base_fen": 0,
            "maximum_base_fen": 10_000_000,
            "rounding_rule": "half_up",
        }
        for code, employee_rate, employer_rate in (
            ("pension", "0.08", "0.16"),
            ("medical", "0.02", "0.095"),
            ("unemployment", "0.005", "0.005"),
            ("work_injury", "0", "0.004"),
        )
    ]
    assert (
        service.register_payroll_policy_version(
            RegisterPayrollPolicyVersionRequest.model_validate(
                {
                    "org_id": organization.id,
                    "region": "杭州",
                    "effective_from": "2026-01-01",
                    "effective_to": "2026-12-31",
                    "version": "final-wage-2026",
                    "source_url": "https://www.chinatax.gov.cn/",
                    "parameters": parameters,
                }
            )
        )["status"]
        == "registered"
    )

    preview = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "final-wage-march-preview",
                "batch_kind": "regular",
                "payroll_period": "2026-03",
                "posting_date": "2026-03-31",
                "payment_date": "2026-04-15",
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "tax_reported_salary_fen": 0,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                ],
            }
        )
    )
    assert preview.status == "calculated", preview.model_dump(mode="json")
    assert preview.data["summary"] == {
        "gross_salary_fen": 0,
        "net_salary_fen": 0,
        "employer_social_insurance_fen": 184_500,
        "employer_housing_fund_fen": 0,
        "individual_income_tax_fen": 0,
    }
    confirmed = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="final-wage-march-confirm",
        )
    )
    assert confirmed.status == "posted", confirmed.errors
    line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == preview.batch_id)
    )
    assert line is not None
    assert line.tax_reported_salary_fen == 0
    assert line.employee_social_insurance_fen == 0
    slot = session.scalar(
        select(PayrollTaxStateSlot).where(PayrollTaxStateSlot.regular_batch_id == preview.batch_id)
    )
    assert slot is not None
    assert (slot.tax_year, slot.tax_month) == (2026, 3)
    assert_balanced(session, confirmed.voucher_id)


def test_evidenced_accounting_wage_can_differ_from_tax_reported_salary(
    session: Session, organization: Organization
) -> None:
    employee_id = register_payroll_facts(session, organization)
    evidence = Evidence(
        org_id=organization.id,
        sha256="a" * 64,
        original_name="wage-tax-difference.txt",
        media_type="text/plain",
        source="owner_confirmation",
        size_bytes=1,
        storage_path="test/wage-tax-difference.txt",
    )
    session.add(evidence)
    session.flush()
    service = FinanceService(session)
    preview = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "wage-tax-difference-preview",
                "batch_kind": "regular",
                "payroll_period": "2026-03",
                "posting_date": "2026-03-05",
                "payment_date": "2026-03-05",
                "evidence_references": [evidence.id],
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "tax_reported_salary_fen": 500_000,
                        "accounting_gross_salary_fen": 150_000,
                        "tax_reporting_difference_reason": (
                            "历史申报数不形成实际工资债务；账务工资按个人缴费扣款确认。"
                        ),
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                ],
            }
        )
    )
    assert preview.status == "calculated", preview.model_dump(mode="json")
    assert preview.data["summary"] == {
        "gross_salary_fen": 150_000,
        "net_salary_fen": 0,
        "employer_social_insurance_fen": 160_000,
        "employer_housing_fund_fen": 70_000,
        "individual_income_tax_fen": 0,
    }
    line_payload = preview.data["lines"][0]
    assert line_payload["tax_reported_salary_fen"] == 500_000
    assert line_payload["gross_salary_fen"] == 150_000
    reconciliation = next(
        entry
        for entry in line_payload["trace"]
        if entry["step"] == "wage_tax_reporting_reconciliation"
    )
    assert reconciliation["values"] == {
        "accounting_gross_salary_fen": 150_000,
        "tax_reported_salary_fen": 500_000,
        "difference_fen": -350_000,
        "difference_reason": "历史申报数不形成实际工资债务；账务工资按个人缴费扣款确认。",
    }
    tax_state = next(
        entry for entry in line_payload["trace"] if entry["step"] == "tax_state_after"
    )
    assert tax_state["values"]["cumulative_income_fen"] == 500_000

    confirmed = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="wage-tax-difference-confirm",
        )
    )
    assert confirmed.status == "posted", confirmed.errors
    assert_balanced(session, confirmed.voucher_id)


def test_wage_reporting_difference_requires_reason_and_evidence() -> None:
    base = {
        "org_id": uuid.uuid4(),
        "idempotency_key": "wage-tax-difference-validation",
        "batch_kind": "regular",
        "payroll_period": "2026-03",
        "posting_date": "2026-03-31",
        "payment_date": "2026-03-31",
        "employee_items": [
            {
                "employee_id": uuid.uuid4(),
                "tax_reported_salary_fen": 500_000,
                "accounting_gross_salary_fen": 150_000,
                "special_additional_deduction_fen": 0,
                "other_legal_deduction_fen": 0,
            }
        ],
    }
    with pytest.raises(ValueError, match="tax_reporting_difference_reason"):
        PreviewPayrollRequest.model_validate(base)

    base["employee_items"][0]["tax_reporting_difference_reason"] = "负责人确认差异"
    with pytest.raises(ValueError, match="evidence_references"):
        PreviewPayrollRequest.model_validate(base)

    base["evidence_references"] = [uuid.uuid4()]
    validated = PreviewPayrollRequest.model_validate(base)
    assert validated.employee_items[0].accounting_gross_salary_fen == 150_000


def test_unreported_wage_line_posts_only_company_borne_social_without_tax_slot(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    employee_result = service.register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="CONTRIBUTION-ONLY-001",
            name="仅社保人员",
            employment_start_date=date(2026, 7, 1),
            tax_withholding_start_date=None,
        )
    )
    employee_id = uuid.UUID(employee_result["employee_id"])
    assert (
        service.register_employee_payroll_profile_version(
            RegisterEmployeePayrollProfileVersionRequest(
                org_id=organization.id,
                employee_id=employee_id,
                effective_from=date(2026, 7, 1),
                expense_role="payroll_service_cost",
                social_insurance_base_fen=500_000,
                housing_fund_base_fen=0,
                social_insurance_participating=True,
                housing_fund_participating=False,
                resident_employee=None,
            )
        )["status"]
        == "registered"
    )
    parameters = payroll_parameters()
    parameters["employee_contribution_shortfall_treatment"] = "employer_borne"
    parameters["contribution_rules"] = [
        {
            "code": code,
            "base_kind": "social_insurance",
            "employee_rate": employee_rate,
            "employer_rate": employer_rate,
            "minimum_base_fen": 0,
            "maximum_base_fen": 10_000_000,
            "rounding_rule": "half_up",
        }
        for code, employee_rate, employer_rate in (
            ("pension", "0.08", "0.16"),
            ("medical", "0.02", "0.095"),
            ("unemployment", "0.005", "0.005"),
            ("work_injury", "0", "0.004"),
        )
    ]
    assert (
        service.register_payroll_policy_version(
            RegisterPayrollPolicyVersionRequest.model_validate(
                {
                    "org_id": organization.id,
                    "region": "杭州",
                    "effective_from": "2026-01-01",
                    "effective_to": "2026-12-31",
                    "version": "contribution-only-2026",
                    "source_url": "https://www.chinatax.gov.cn/",
                    "parameters": parameters,
                }
            )
        )["status"]
        == "registered"
    )

    preview = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "contribution-only-july-preview",
                "batch_kind": "regular",
                "payroll_period": "2026-07",
                "posting_date": "2026-07-31",
                "payment_date": "2026-08-15",
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "wage_tax_declaration_state": "not_declared",
                        "tax_reported_salary_fen": None,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                ],
            }
        )
    )
    assert preview.status == "calculated", preview.model_dump(mode="json")
    assert preview.data["summary"] == {
        "gross_salary_fen": 0,
        "net_salary_fen": 0,
        "employer_social_insurance_fen": 184_500,
        "employer_housing_fund_fen": 0,
        "individual_income_tax_fen": 0,
    }
    assert preview.data["lines"][0]["wage_tax_declaration_state"] == "not_declared"

    confirmed = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="contribution-only-july-confirm",
        )
    )
    assert confirmed.status == "posted", confirmed.errors
    line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == preview.batch_id)
    )
    assert line is not None
    assert line.wage_tax_declaration_state == "not_declared"
    assert line.tax_reported_salary_fen is None
    assert line.employee_social_insurance_fen == 0
    assert line.employer_social_insurance_fen == 184_500
    assert session.scalar(
        select(PayrollTaxStateSlot).where(
            PayrollTaxStateSlot.regular_batch_id == preview.batch_id
        )
    ) is None
    assert_balanced(session, confirmed.voucher_id)


def test_payroll_profile_records_company_contribution_participation(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    employee_result = service.register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="WAGE-NO-SOCIAL-001",
            name="本公司未参保工资人员",
            employment_start_date=date(2026, 3, 1),
            tax_withholding_start_date=date(2026, 3, 1),
        )
    )
    employee_id = uuid.UUID(employee_result["employee_id"])
    request = RegisterEmployeePayrollProfileVersionRequest(
        org_id=organization.id,
        employee_id=employee_id,
        effective_from=date(2026, 3, 1),
        expense_role="payroll_management_expense",
        social_insurance_base_fen=0,
        housing_fund_base_fen=0,
        social_insurance_participating=False,
        housing_fund_participating=False,
        resident_employee=True,
    )

    registered = service.register_employee_payroll_profile_version(request)
    replay = service.register_employee_payroll_profile_version(request)
    profile = session.get(
        EmployeePayrollProfileVersion,
        uuid.UUID(registered["profile_version_id"]),
    )

    assert registered["status"] == "registered"
    assert replay["idempotent_replay"] is True
    assert profile is not None
    assert profile.social_insurance_participating is False
    assert profile.housing_fund_participating is False


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


def test_payroll_accruals_can_be_reversed_repeatedly_from_latest_to_earliest(
    session: Session, organization: Organization
) -> None:
    employee_id = register_payroll_facts(session, organization)
    service = FinanceService(session)
    confirmed = []
    for month in (3, 4):
        preview = service.preview_payroll(
            PreviewPayrollRequest.model_validate(
                {
                    "org_id": organization.id,
                    "idempotency_key": f"payroll-chain-preview-{month}",
                    "batch_kind": "regular",
                    "payroll_period": f"2026-{month:02d}",
                    "posting_date": f"2026-{month:02d}-05",
                    "payment_date": f"2026-{month:02d}-05",
                    "employee_items": [
                        {
                            "employee_id": employee_id,
                            "tax_reported_salary_fen": 1_000_000,
                            "special_additional_deduction_fen": 0,
                            "other_legal_deduction_fen": 0,
                        }
                    ],
                }
            )
        )
        assert preview.status == "calculated"
        result = service.confirm_payroll(
            ConfirmPayrollRequest(
                org_id=organization.id,
                batch_id=preview.batch_id,
                calculation_hash=preview.calculation_hash,
                idempotency_key=f"payroll-chain-confirm-{month}",
            )
        )
        assert result.status == "posted"
        confirmed.append(result)

    for month, result in zip((4, 3), reversed(confirmed), strict=True):
        reversed_result = service.reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=result.event_id,
                idempotency_key=f"payroll-chain-reverse-{month}",
                reason="从最新月份向前重建累计工资链",
                posting_date=date(2026, month, 6),
            )
        )
        assert reversed_result.status == "posted", reversed_result.model_dump(mode="json")

    assert all(
        session.get(PayrollBatch, result.batch_id).status == "reversed"
        for result in confirmed
    )


def test_social_insurance_payment_can_separate_evidenced_late_fee(
    session: Session, organization: Organization
) -> None:
    service, confirmed = preview_and_confirm(session, organization)
    social_items = session.scalars(
        select(OpenItem).where(
            OpenItem.source_event_id == confirmed.event_id,
            OpenItem.payable_category == "employer_social",
        )
    ).all()
    assert sum(item.original_amount_fen for item in social_items) == 160_000
    evidence = Evidence(
        org_id=organization.id,
        sha256=uuid.uuid5(organization.id, "social-late-fee-evidence").hex * 2,
        original_name="social-late-fee.txt",
        media_type="text/plain",
        source="test",
        size_bytes=1,
        storage_path=f"tests/{organization.id}/social-late-fee.txt",
        metadata_json={},
    )
    session.add(evidence)
    session.flush()
    bank = add_bank_row(session, organization, -160_500, "social-with-late-fee")
    common = {
        "org_id": organization.id,
        "event_type": "social_insurance_payment",
        "business_dates": {
            "business_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "posting_date": "2026-03-05",
        },
        "amounts": {"amount_fen": 160_500},
        "bank_account_code": "1002",
        "bank_transaction_references": [{"id": bank.id}],
        "allocations": [
            {"open_item_id": item.id, "amount_fen": item.original_amount_fen}
            for item in social_items
        ],
        "details": {"social_insurance_late_fee_fen": 500},
        "description": "社保本金1600元及滞纳金5元合并扣款",
    }
    missing = service.record_event(
        RecordEventRequest.model_validate(
            common | {"idempotency_key": "social-late-fee-missing-evidence"}
        )
    )
    assert missing.status == "needs_information"
    assert missing.missing_information == ["evidence_references"]

    payment = service.record_event(
        RecordEventRequest.model_validate(
            common
            | {
                "idempotency_key": "social-late-fee-payment",
                "evidence_references": [evidence.id],
            }
        )
    )
    assert payment.status == "posted", payment.errors
    assert payment.data["derived"] == {
        "payable_categories": ["employer_social", "withheld_employee_social"],
        "allocated_fen": 160_000,
        "social_insurance_late_fee_fen": 500,
    }
    voucher = session.get(Voucher, payment.voucher_id)
    assert voucher is not None
    by_role = {
        line.account.system_role: (line.debit_fen, line.credit_fen)
        for line in voucher.lines
    }
    assert by_role["employer_social_payable"] == (160_000, 0)
    assert by_role["social_insurance_late_fee_expense"] == (500, 0)
    assert by_role["bank"] == (0, 160_500)
    assert all(item.status == "settled" for item in social_items)
    assert bank.matched_event_id == payment.event_id

    with pytest.raises(
        ValueError,
        match="social_insurance_late_fee_fen is only accepted for social insurance payment",
    ):
        RecordEventRequest.model_validate(
            common
            | {
                "idempotency_key": "housing-with-social-late-fee",
                "event_type": "housing_fund_payment",
            }
        )
