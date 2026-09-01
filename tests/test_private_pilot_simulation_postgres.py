"""A fully fictional, repeatable private-pilot rehearsal on PostgreSQL 17.

This is deliberately an acceptance simulation, not an integration with a real
bank statement, customer, employee, or production Compose database.  DEC-013
late-bank evidence is intentionally out of scope until its remaining product
decisions are resolved.
"""

from __future__ import annotations

import shutil
import uuid
from calendar import monthrange
from datetime import UTC, date, datetime, timedelta
from hashlib import sha256
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import select
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.accounting_period_schemas import (
    AccountingPeriodReviewFacts,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationRequest,
    ConfirmBankStatementFileImportRequest,
    PreviewBankReconciliationRequest,
    PreviewBankStatementFileImportRequest,
)
from ai_accounting.bank_statement_service import BankStatementService
from ai_accounting.borrowing_schemas import (
    ConfirmBorrowingInterestRequest,
    DrawBorrowingRequest,
    PreviewBorrowingInterestRequest,
)
from ai_accounting.borrowing_service import BorrowingService
from ai_accounting.coa import seed_organization
from ai_accounting.config import Settings
from ai_accounting.fixed_asset_service import FixedAssetService
from ai_accounting.intangible_asset_schemas import (
    AcquireIntangibleAssetRequest,
    ConfirmIntangibleAssetAmortizationRequest,
    PreviewIntangibleAssetAmortizationRequest,
)
from ai_accounting.intangible_asset_service import IntangibleAssetService
from ai_accounting.models import (
    EXECUTION_ATTRIBUTION_SESSION_KEY,
    AccountingPeriodClose,
    AccountingPeriodCloseApproval,
    BankTransaction,
    Evidence,
    ExecutionAttribution,
    Organization,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    ConfirmFixedAssetDepreciationRequest,
    ConfirmPayrollRequest,
    PreviewFixedAssetDepreciationRequest,
    PreviewPayrollRequest,
    RecordEventRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _evidence(session: Session, organization: Organization, seed: str) -> Evidence:
    evidence = Evidence(
        org_id=organization.id,
        sha256=sha256(seed.encode("utf-8")).hexdigest(),
        original_name=f"fictional-{seed}.txt",
        media_type="text/plain",
        source="private-pilot-simulation",
        size_bytes=1,
        storage_path=f"private-pilot/{seed}.txt",
    )
    session.add(evidence)
    session.flush()
    return evidence


def _import_bank_row(
    session: Session,
    organization: Organization,
    *,
    amount_fen: int,
    booking_date: date,
    seed: str,
    import_dir: Path,
) -> tuple[BankTransaction, uuid.UUID]:
    """Create the fictional draw through the same CSV preview-confirm path."""

    file_name = f"{seed}.csv"
    amount_text = f"{amount_fen // 100}.{amount_fen % 100:02d}"
    (import_dir / file_name).write_text(
        "date,amount,reference,memo\n"
        f"{booking_date.isoformat()},{amount_text},{seed},fictional pilot {seed}\n",
        encoding="utf-8",
    )
    service = BankStatementService(
        session,
        settings=Settings(finance_bank_import_dir=import_dir),
        current_date=booking_date.replace(day=min(20, booking_date.day)),
    )
    request = PreviewBankStatementFileImportRequest(
        org_id=organization.id,
        bank_account_code="1002",
        source_file_name=file_name,
        file_format="csv",
        column_mapping={
            "booking_date": "date",
            "amount": "amount",
            "external_id": "reference",
            "memo": "memo",
        },
    )
    preview = service.preview_bank_statement_import(request)
    assert preview.status == "calculated", preview.errors
    assert preview.calculation_hash is not None
    result = service.confirm_bank_statement_import(
        ConfirmBankStatementFileImportRequest.model_validate(
            request.model_dump()
            | {
                "calculation_hash": preview.calculation_hash,
                "idempotency_key": f"pilot-import-{seed}",
            }
        )
    )
    assert result.status == "posted", result.errors
    assert result.action_id is not None
    row = session.scalar(
        select(BankTransaction).where(
            BankTransaction.org_id == organization.id,
            BankTransaction.external_id == seed,
        )
    )
    assert row is not None
    return row, result.action_id


def _reconcile_bank_month(
    session: Session,
    organization: Organization,
    *,
    period_id: uuid.UUID,
    month: int,
    opening_balance_fen: int,
    closing_balance_fen: int,
    evidence_id: uuid.UUID,
    import_action_ids: list[uuid.UUID],
) -> None:
    service = BankStatementService(session)
    request = PreviewBankReconciliationRequest(
        org_id=organization.id,
        period_id=period_id,
        bank_account_code="1002",
        coverage_start_date=date(2026, month, 1),
        coverage_end_date=_month_end(month),
        statement_opening_balance_fen=opening_balance_fen,
        statement_closing_balance_fen=closing_balance_fen,
        statement_import_action_ids=import_action_ids,
        statement_evidence_references=[evidence_id],
    )
    preview = service.preview_bank_reconciliation(request)
    assert preview.status == "calculated", preview.errors
    assert preview.calculation_hash is not None
    result = service.confirm_bank_reconciliation(
        ConfirmBankReconciliationRequest.model_validate(
            request.model_dump()
            | {
                "calculation_hash": preview.calculation_hash,
                "idempotency_key": f"pilot-reconcile-2026-{month:02d}",
            }
        )
    )
    assert result.status == "posted", result.errors


def _assert_balanced(session: Session, voucher_id: uuid.UUID) -> None:
    lines = session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)).all()
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines)


def _payroll_parameters() -> dict[str, object]:
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
            "version": "fictional-pilot-income-tax-2026",
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
            "version": "fictional-pilot-bonus-2026",
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
            "social_insurance": {"agency_code": "FICTIONAL-SOCIAL", "agency_name": "虚构社保机构"},
            "housing_fund": {"agency_code": "FICTIONAL-HOUSING", "agency_name": "虚构公积金机构"},
            "individual_income_tax": {
                "agency_code": "FICTIONAL-TAX",
                "agency_name": "虚构税务机构",
            },
        },
    }


def _review_facts() -> AccountingPeriodReviewFacts:
    return AccountingPeriodReviewFacts(
        voucher_completeness_reviewed=True,
        bank_reconciliation_reviewed=True,
        open_items_reviewed=True,
        payroll_and_statutory_items_reviewed=True,
        payroll_settlements_reviewed=True,
        tax_items_reviewed=True,
        asset_and_borrowing_schedules_reviewed=True,
    )


def _month_end(month: int) -> date:
    return date(2026, month, monthrange(2026, month)[1])


def _generate(
    service: AccountingPeriodService, org_id: uuid.UUID, evidence_id: uuid.UUID, month: int
) -> uuid.UUID:
    result = service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=org_id,
            period_month=f"2026-{month:02d}",
            idempotency_key=f"pilot-generate-2026-{month:02d}",
            confirmation_note=f"虚构试用连续生成 2026-{month:02d}",
            evidence_references=[evidence_id],
        )
    )
    assert result.status == "posted", result.errors
    assert result.period_id is not None
    return result.period_id


def _close(
    service: AccountingPeriodService,
    org_id: uuid.UUID,
    evidence_id: uuid.UUID,
    period_id: uuid.UUID,
    month: int,
) -> uuid.UUID:
    preview_request = PreviewAccountingPeriodCloseRequest(
        org_id=org_id,
        period_id=period_id,
        closing_date=_month_end(month),
    )
    preview = service.preview_accounting_period_close(preview_request)
    assert preview.status == "calculated", preview.errors
    attribution_id = service.session.info.get(EXECUTION_ATTRIBUTION_SESSION_KEY)
    assert attribution_id is not None
    attribution = service.session.get(ExecutionAttribution, attribution_id)
    assert attribution is not None
    now = datetime.now(UTC)
    approval = AccountingPeriodCloseApproval(
        org_id=org_id,
        period_id=period_id,
        owner_account_id=attribution.owner_account_id,
        owner_session_id=attribution.owner_session_id,
        owner_credential_version=attribution.owner_credential_version,
        calculation_hash=preview.calculation_hash,
        confirmation_method="local_password_reauthentication",
        confirmed_at=now,
        expires_at=now + timedelta(minutes=30),
    )
    service.session.add(approval)
    service.session.flush()
    result = service.confirm_accounting_period_close(
        ConfirmAccountingPeriodCloseRequest(
            **preview_request.model_dump(),
            calculation_hash=preview.calculation_hash,
            management_commentary_context_hash=preview.data[
                "assistant_review_checklist"
            ]["management_commentary"]["context_hash"],
            management_commentary=f"2026 年 {month} 月经营情况已基于关账上下文完成分析。",
            owner_approval_id=approval.id,
            idempotency_key=f"pilot-close-2026-{month:02d}",
            review_facts=_review_facts(),
            confirmation_note=f"虚构试用逐月关闭 2026-{month:02d}",
            evidence_references=[evidence_id],
        )
    )
    assert result.status == "posted", result.errors
    assert result.close_id is not None
    return result.close_id


def _confirm_borrowing_interest(
    service: BorrowingService,
    org_id: uuid.UUID,
    borrowing_id: uuid.UUID,
    *,
    start: date,
    end: date,
    key: str,
) -> None:
    preview_request = PreviewBorrowingInterestRequest(
        org_id=org_id, borrowing_id=borrowing_id, period_start=start, period_end=end
    )
    preview = service.preview_borrowing_interest(preview_request)
    assert preview.status == "calculated", preview.errors
    result = service.confirm_borrowing_interest(
        ConfirmBorrowingInterestRequest(
            **preview_request.model_dump(),
            calculation_hash=preview.calculation_hash,
            idempotency_key=key,
        )
    )
    assert result.status == "posted", result.errors


def _confirm_fixed_asset_depreciation(
    service: FixedAssetService, org_id: uuid.UUID, asset_id: uuid.UUID, month: int
) -> None:
    request = PreviewFixedAssetDepreciationRequest(
        org_id=org_id,
        asset_id=asset_id,
        depreciation_period=f"2026-{month:02d}",
        posting_date=_month_end(month),
    )
    preview = service.preview_fixed_asset_depreciation(request)
    assert preview.status == "calculated", preview.errors
    result = service.confirm_fixed_asset_depreciation(
        ConfirmFixedAssetDepreciationRequest(
            **request.model_dump(),
            calculation_hash=preview.calculation_hash,
            idempotency_key=f"pilot-fixed-depreciation-2026-{month:02d}",
        )
    )
    assert result.status == "posted", result.errors


def _confirm_intangible_amortization(
    service: IntangibleAssetService, org_id: uuid.UUID, asset_id: uuid.UUID, month: int
) -> None:
    request = PreviewIntangibleAssetAmortizationRequest(
        org_id=org_id,
        asset_id=asset_id,
        amortization_period=f"2026-{month:02d}",
        posting_date=_month_end(month),
    )
    preview = service.preview_intangible_asset_amortization(request)
    assert preview.status == "calculated", preview.errors
    result = service.confirm_intangible_asset_amortization(
        ConfirmIntangibleAssetAmortizationRequest(
            **request.model_dump(),
            calculation_hash=preview.calculation_hash,
            idempotency_key=f"pilot-intangible-amortization-2026-{month:02d}",
        )
    )
    assert result.status == "posted", result.errors


def test_private_pilot_fictional_five_month_rehearsal_on_ephemeral_postgresql17(
    authenticated_bank_scope: object,
    tmp_path: Path,
) -> None:
    """Exercise the private-pilot path without real data or a Compose database."""

    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="虚构五个月私有试用组织",
                    filing_cycle="monthly",
                )
                evidence = _evidence(session, organization, "pilot-evidence")
                session.commit()
                org_id, evidence_id = organization.id, evidence.id

            with Session(engine) as session:
                organization = session.get(Organization, org_id)
                assert organization is not None
                authority = authenticated_bank_scope(
                    session,
                    organization,
                    evidence_id=evidence_id,
                    accounts=[
                        {
                            "bank_account_code": "1002",
                            "account_name": "银行存款",
                            "start_date": date(2026, 1, 1),
                        }
                    ],
                    executor_name="private-pilot-simulation",
                )
                write_call = authority.attributed_call(
                    session, tool_name="finance_private_pilot_rehearsal"
                )
                write_call.__enter__()
                period_service = AccountingPeriodService(session, current_date=date(2026, 8, 11))
                finance = FinanceService(session)
                fixed_assets = FixedAssetService(session)
                intangibles = IntangibleAssetService(session)
                borrowings = BorrowingService(session)
                march_period_id = _generate(period_service, org_id, evidence_id, 3)

                sale = finance.record_event(
                    RecordEventRequest.model_validate(
                        {
                            "org_id": org_id,
                            "idempotency_key": "pilot-fictional-march-sale",
                            "event_type": "service_credit_sale",
                            "business_dates": {
                                "business_date": "2026-03-05",
                                "posting_date": "2026-03-05",
                                "fulfillment_date": "2026-03-05",
                                "payment_date": "2026-03-05",
                                "tax_obligation_date": "2026-03-05",
                            },
                            "amounts": {"gross_amount_fen": 101_000},
                            "counterparty": {"kind": "customer", "name": "虚构试用客户"},
                            "tax_facts": {
                                "taxable": True,
                                "rate_percent": "1",
                                "invoice_type": "ordinary",
                                "waive_exemption": False,
                                "tax_due_on_event": True,
                            },
                        }
                    )
                )
                assert sale.status == "posted", sale.errors
                _assert_balanced(session, sale.voucher_id)

                employee = finance.register_employee(
                    RegisterEmployeeRequest(
                        org_id=org_id,
                        employee_code="FICTIONAL-E-001",
                        name="虚构试用员工",
                        employment_start_date=date(2026, 3, 1),
                        tax_withholding_start_date=date(2026, 3, 1),
                        status="active",
                    )
                )
                employee_id = uuid.UUID(employee["employee_id"])
                assert (
                    finance.register_employee_payroll_profile_version(
                        RegisterEmployeePayrollProfileVersionRequest(
                            org_id=org_id,
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
                    finance.register_payroll_policy_version(
                        RegisterPayrollPolicyVersionRequest(
                            org_id=org_id,
                            region="虚构试用地区",
                            effective_from=date(2026, 1, 1),
                            effective_to=date(2026, 12, 31),
                            version="fictional-pilot-2026",
                            source_url="https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
                            parameters=_payroll_parameters(),
                        )
                    )["status"]
                    == "registered"
                )
                payroll_preview = finance.preview_payroll(
                    PreviewPayrollRequest.model_validate(
                        {
                            "org_id": org_id,
                            "idempotency_key": "pilot-fictional-payroll-preview",
                            "batch_kind": "regular",
                            "payroll_period": "2026-03",
                            "posting_date": "2026-03-20",
                            "payment_date": "2026-03-20",
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
                assert payroll_preview.status == "calculated", payroll_preview.errors
                payroll = finance.confirm_payroll(
                    ConfirmPayrollRequest(
                        org_id=org_id,
                        batch_id=payroll_preview.batch_id,
                        calculation_hash=payroll_preview.calculation_hash,
                        idempotency_key="pilot-fictional-payroll-confirm",
                    )
                )
                assert payroll.status == "posted", payroll.errors
                _assert_balanced(session, payroll.voucher_id)

                fixed = fixed_assets.acquire_fixed_asset(
                    AcquireFixedAssetRequest.model_validate(
                        {
                            "org_id": org_id,
                            "idempotency_key": "pilot-fictional-fixed-acquire",
                            "asset_code": "FICTIONAL-FA-001",
                            "asset_name": "虚构试用设备",
                            "category": "production_equipment",
                            "expected_use_over_one_year": True,
                            "purchase_date": "2026-03-10",
                            "posting_date": "2026-03-10",
                            "cost_components": {
                                "purchase_price_fen": 1_000_000,
                                "noncreditable_tax_fen": 0,
                                "transport_and_handling_fen": 0,
                                "installation_and_direct_cost_fen": 0,
                            },
                            "supplier": {"kind": "supplier", "name": "虚构设备供应商"},
                            "settlement_method": "payable",
                            "due_date": "2026-04-10",
                            "evidence_references": [evidence_id],
                            "claims_creditable_input_vat": False,
                        }
                    )
                )
                assert fixed.status == "posted", fixed.errors
                activated = fixed_assets.activate_fixed_asset(
                    ActivateFixedAssetRequest.model_validate(
                        {
                            "org_id": org_id,
                            "asset_id": fixed.asset_id,
                            "idempotency_key": "pilot-fictional-fixed-activate",
                            "activation_date": "2026-03-12",
                            "posting_date": "2026-03-12",
                            "useful_life_months": 24,
                            "residual_value_fen": 0,
                            "benefit_area": "management",
                            "evidence_references": [evidence_id],
                        }
                    )
                )
                assert activated.status == "posted", activated.errors

                intangible = intangibles.acquire_intangible_asset(
                    AcquireIntangibleAssetRequest.model_validate(
                        {
                            "org_id": org_id,
                            "idempotency_key": "pilot-fictional-intangible-acquire",
                            "asset_code": "FICTIONAL-IA-001",
                            "asset_name": "虚构试用软件许可",
                            "category": "software",
                            "rights_description": "虚构的十二个月软件使用许可",
                            "supplier": {"kind": "supplier", "name": "虚构软件供应商"},
                            "acquisition_date": "2026-03-10",
                            "available_for_use_date": "2026-03-10",
                            "posting_date": "2026-03-10",
                            "cost_components": {
                                "purchase_price_fen": 120_000,
                                "noncreditable_tax_fen": 0,
                                "directly_attributable_cost_fen": 0,
                            },
                            "settlement_method": "payable",
                            "due_date": "2026-04-10",
                            "benefit_area": "management",
                            "life_basis": "legal_or_contractual",
                            "useful_life_months": 12,
                            "life_basis_explanation": "虚构合同约定十二个月许可期",
                            "is_available_for_use": True,
                            "claims_creditable_input_vat": False,
                            "evidence_references": [evidence_id],
                        }
                    )
                )
                assert intangible.status == "posted", intangible.errors

                draw_bank, march_import_action_id = _import_bank_row(
                    session,
                    session.get(Organization, org_id),
                    amount_fen=1_000_000,
                    booking_date=date(2026, 3, 3),
                    seed="fictional-loan-draw",
                    import_dir=tmp_path,
                )
                borrowing = borrowings.draw_borrowing(
                    DrawBorrowingRequest.model_validate(
                        {
                            "org_id": org_id,
                            "idempotency_key": "pilot-fictional-borrowing-draw",
                            "borrowing_code": "FICTIONAL-LOAN-001",
                            "contract_name": "虚构试用周转借款",
                            "lender": {"name": "虚构持牌银行"},
                            "lender_is_licensed_financial_institution": True,
                            "currency": "CNY",
                            "principal_fen": 1_000_000,
                            "bank_account_code": "1002",
                            "drawdown_date": "2026-03-03",
                            "due_date": "2027-03-03",
                            "posting_date": "2026-03-03",
                            "annual_rate_percent": "3.65",
                            "day_count_basis": "actual_365",
                            "interest_due_dates": [
                                "2026-03-31",
                                "2026-04-30",
                                "2026-05-31",
                                "2026-06-30",
                                "2026-07-31",
                                "2027-03-03",
                            ],
                            "capitalization_applicable": False,
                            "purpose_description": "仅用于虚构试用日常经营周转",
                            "term_facts": {
                                "single_drawdown": True,
                                "fixed_rate": True,
                                "simple_interest": True,
                                "bullet_principal_at_maturity": True,
                                "allows_prepayment": False,
                                "allows_extension": False,
                                "has_penalty_interest": False,
                                "has_financing_fees": False,
                            },
                            "bank_transaction_references": [{"id": draw_bank.id}],
                            "evidence_references": [evidence_id],
                        }
                    )
                )
                assert borrowing.status == "posted", borrowing.errors
                _confirm_intangible_amortization(intangibles, org_id, intangible.asset_id, 3)
                _confirm_borrowing_interest(
                    borrowings,
                    org_id,
                    borrowing.borrowing_id,
                    start=date(2026, 3, 3),
                    end=date(2026, 3, 31),
                    key="pilot-borrowing-interest-2026-03",
                )

                tax_preview = finance.preview_tax_period(
                    TaxPeriodPreviewRequest(
                        org_id=org_id,
                        start_date=date(2026, 3, 1),
                        end_date=date(2026, 3, 31),
                        adjustment_posting_date=date(2026, 3, 31),
                    )
                )
                assert tax_preview["status"] == "calculated", tax_preview
                tax = finance.confirm_tax_period(
                    TaxPeriodConfirmRequest(
                        org_id=org_id,
                        start_date=date(2026, 3, 1),
                        end_date=date(2026, 3, 31),
                        adjustment_posting_date=date(2026, 3, 31),
                        calculation_hash=tax_preview["calculation_hash"],
                        idempotency_key="pilot-fictional-march-tax-period",
                    )
                )
                assert tax.status == "posted", tax.errors
                _reconcile_bank_month(
                    session,
                    session.get(Organization, org_id),
                    period_id=march_period_id,
                    month=3,
                    opening_balance_fen=0,
                    closing_balance_fen=1_000_000,
                    evidence_id=evidence_id,
                    import_action_ids=[march_import_action_id],
                )
                march_close_id = _close(period_service, org_id, evidence_id, march_period_id, 3)
                march_close = session.get(AccountingPeriodClose, march_close_id)
                march_snapshot = (march_close.calculation_hash, march_close.calculation_payload)

                previous_interest_end = date(2026, 3, 31)
                for month in range(4, 8):
                    period_id = _generate(period_service, org_id, evidence_id, month)
                    _confirm_fixed_asset_depreciation(fixed_assets, org_id, fixed.asset_id, month)
                    _confirm_intangible_amortization(
                        intangibles, org_id, intangible.asset_id, month
                    )
                    month_end = _month_end(month)
                    _confirm_borrowing_interest(
                        borrowings,
                        org_id,
                        borrowing.borrowing_id,
                        start=previous_interest_end,
                        end=month_end,
                        key=f"pilot-borrowing-interest-2026-{month:02d}",
                    )
                    _reconcile_bank_month(
                        session,
                        session.get(Organization, org_id),
                        period_id=period_id,
                        month=month,
                        opening_balance_fen=1_000_000,
                        closing_balance_fen=1_000_000,
                        evidence_id=evidence_id,
                        import_action_ids=[],
                    )
                    _close(period_service, org_id, evidence_id, period_id, month)
                    previous_interest_end = month_end

                _generate(period_service, org_id, evidence_id, 8)
                locked_sale_reversal = finance.reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=sale.event_id,
                        idempotency_key="pilot-fictional-august-locked-march-sale",
                        reason="虚构试用先验证税期调整锁定来源",
                        posting_date=date(2026, 8, 1),
                    )
                )
                assert locked_sale_reversal.status == "rejected"
                assert locked_sale_reversal.errors == ["TAX_PERIOD_SOURCE_LOCKED"]
                tax_reversal = finance.reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=tax.event_id,
                        idempotency_key="pilot-fictional-august-reversal-of-march-tax",
                        reason="虚构试用先冲正三月税期调整",
                        posting_date=date(2026, 8, 1),
                    )
                )
                assert tax_reversal.status == "posted", tax_reversal.errors
                reversal = finance.reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=sale.event_id,
                        idempotency_key="pilot-fictional-august-reversal-of-march-sale",
                        reason="虚构试用在后续开放月更正三月服务销售",
                        posting_date=date(2026, 8, 2),
                    )
                )
                assert reversal.status == "posted", reversal.errors
                _assert_balanced(session, reversal.voucher_id)
                session.refresh(march_close)
                assert (
                    march_close.calculation_hash,
                    march_close.calculation_payload,
                ) == march_snapshot
                assert (
                    session.scalar(
                        select(Voucher).where(Voucher.org_id == org_id, Voucher.status == "posted")
                    )
                    is not None
                )
                session.commit()
                write_call.__exit__(None, None, None)
        finally:
            engine.dispose()
