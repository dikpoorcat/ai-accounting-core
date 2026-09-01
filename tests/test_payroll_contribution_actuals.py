from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session
from test_payroll_service import add_bank_row, payment_request, payroll_parameters

from ai_accounting.accounting_period_schemas import (
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.models import (
    BusinessEvent,
    Evidence,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollContributionActualItem,
    PayrollContributionActualSet,
    PayrollContributionActualUse,
    PayrollContributionSupplement,
    PayrollFirstWageTaxTreatmentUse,
    PayrollLine,
    PayrollPolicyVersion,
    Voucher,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RecordEventRequest,
    RecordPayrollContributionSupplementRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollContributionActualRequest,
    RegisterPayrollFirstWageTaxTreatmentRequest,
    RegisterPayrollPolicyVersionRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService


def _setup_four_insurance_employee(
    session: Session, organization: Organization
) -> tuple[FinanceService, uuid.UUID, Evidence]:
    service = FinanceService(session)
    employee_result = service.register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="FOUR-INSURANCE-001",
            name="王小娜",
            employment_start_date=date(2026, 7, 1),
            tax_withholding_start_date=date(2026, 7, 1),
        )
    )
    employee_id = uuid.UUID(employee_result["employee_id"])
    profile = service.register_employee_payroll_profile_version(
        RegisterEmployeePayrollProfileVersionRequest(
            org_id=organization.id,
            employee_id=employee_id,
            effective_from=date(2026, 7, 1),
            expense_role="payroll_management_expense",
            social_insurance_base_fen=500_000,
            housing_fund_base_fen=0,
            social_insurance_participating=True,
            housing_fund_participating=False,
            resident_employee=True,
        )
    )
    assert profile["status"] == "registered"
    parameters = payroll_parameters()
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
    policy = service.register_payroll_policy_version(
        RegisterPayrollPolicyVersionRequest.model_validate(
            {
                "org_id": organization.id,
                "region": "杭州",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "version": "hangzhou-four-insurance-2026",
                "source_url": "https://www.chinatax.gov.cn/",
                "parameters": parameters,
            }
        )
    )
    assert policy["status"] == "registered"
    evidence = Evidence(
        org_id=organization.id,
        sha256="7" * 64,
        original_name="王小娜2026年7月社保申报表.pdf",
        source="test",
        size_bytes=1,
        storage_path="test/wang-xiaona-july-social.pdf",
    )
    session.add(evidence)
    session.flush()
    return service, employee_id, evidence


def _actual_request(
    organization: Organization,
    employee_id: uuid.UUID,
    evidence: Evidence,
    *,
    key: str,
    medical_employee_fen: int = 0,
    medical_employer_fen: int = 0,
    supersedes: list[uuid.UUID] | None = None,
) -> RegisterPayrollContributionActualRequest:
    return RegisterPayrollContributionActualRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "employee_id": employee_id,
            "contribution_period": "2026-07",
            "declaration_date": "2026-08-10",
            "reason_code": "partial_declaration",
            "reason_description": "7月医保和工伤未补缴，养老和失业正常申报",
            "items": [
                {
                    "contribution_group": "social_insurance",
                    "insurance_kind": "medical",
                    "actual_state": "not_declared" if not medical_employee_fen else "declared",
                    "employee_amount_fen": medical_employee_fen,
                    "employer_amount_fen": medical_employer_fen,
                },
                {
                    "contribution_group": "social_insurance",
                    "insurance_kind": "work_injury",
                    "actual_state": "not_declared",
                    "employee_amount_fen": 0,
                    "employer_amount_fen": 0,
                },
            ],
            "evidence_references": [evidence.id],
            "supersedes_actual_ids": supersedes or [],
        }
    )


def _preview(
    service: FinanceService,
    organization: Organization,
    employee_id: uuid.UUID,
    *,
    period: str,
    salary_fen: int,
    key: str,
    evidence: Evidence,
) -> object:
    month = int(period[-2:])
    return service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": key,
                "batch_kind": "regular",
                "payroll_period": period,
                "posting_date": date(2026, month, 28),
                "payment_date": date(2026, month + 1, 15),
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "tax_reported_salary_fen": salary_fen,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                    }
                ],
                "evidence_references": [evidence.id],
            }
        )
    )


def _confirm(
    service: FinanceService, organization: Organization, preview: object, key: str
) -> object:
    return service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key=key,
        )
    )


def test_sparse_july_actual_then_august_returns_to_uniform_four_insurance(
    session: Session, organization: Organization
) -> None:
    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    registered = service.register_payroll_contribution_actual(
        _actual_request(organization, employee_id, evidence, key="wang-july-actual")
    )
    replay = service.register_payroll_contribution_actual(
        _actual_request(organization, employee_id, evidence, key="wang-july-actual")
    )
    assert registered["status"] == "registered"
    assert replay["idempotent_replay"] is True

    july = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=42_500,
        key="wang-july-preview",
        evidence=evidence,
    )
    assert july.status == "calculated", july.model_dump(mode="json")
    july_line = july.data["lines"][0]
    assert july_line["employee_social_insurance_items"] == {
        "pension": 40_000,
        "medical": 0,
        "unemployment": 2_500,
        "work_injury": 0,
    }
    assert july_line["employer_social_insurance_items"] == {
        "pension": 80_000,
        "medical": 0,
        "unemployment": 2_500,
        "work_injury": 0,
    }
    assert july.data["summary"]["employer_social_insurance_fen"] == 82_500
    assert july.data["summary"]["net_salary_fen"] == 0
    assert _confirm(service, organization, july, "wang-july-confirm").status == "posted"

    august = _preview(
        service,
        organization,
        employee_id,
        period="2026-08",
        salary_fen=1_000_000,
        key="wang-august-preview",
        evidence=evidence,
    )
    assert august.status == "calculated", august.model_dump(mode="json")
    august_line = august.data["lines"][0]
    assert august_line["employee_social_insurance_items"] == {
        "pension": 40_000,
        "medical": 10_000,
        "unemployment": 2_500,
        "work_injury": 0,
    }
    assert august_line["employer_social_insurance_items"] == {
        "pension": 80_000,
        "medical": 47_500,
        "unemployment": 2_500,
        "work_injury": 2_000,
    }
    policy = session.scalar(
        select(PayrollPolicyVersion).where(PayrollPolicyVersion.org_id == organization.id)
    )
    assert policy is not None
    assert [rule["enabled"] for rule in policy.parameters["contribution_rules"]] == [True] * 4


def test_actual_requires_evidence_policy_kind_and_reversal_before_posted_correction(
    session: Session, organization: Organization
) -> None:
    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    missing_evidence = _actual_request(
        organization, employee_id, evidence, key="actual-missing-evidence"
    ).model_copy(update={"evidence_references": [uuid.uuid4()]})
    assert service.register_payroll_contribution_actual(missing_evidence)["errors"] == [
        "PAYROLL_EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH"
    ]
    unknown_payload = _actual_request(
        organization, employee_id, evidence, key="actual-unknown-kind"
    ).model_dump()
    unknown_payload["items"] = [
        {
            "contribution_group": "social_insurance",
            "insurance_kind": "invented_insurance",
            "actual_state": "declared",
            "employee_amount_fen": 1,
            "employer_amount_fen": 1,
        }
    ]
    unknown_kind = RegisterPayrollContributionActualRequest.model_validate(unknown_payload)
    assert service.register_payroll_contribution_actual(unknown_kind)["errors"] == [
        "CONTRIBUTION_ACTUAL_KIND_NOT_IN_POLICY"
    ]

    registered = service.register_payroll_contribution_actual(
        _actual_request(organization, employee_id, evidence, key="actual-before-draft")
    )
    original_ids = [uuid.UUID(value) for value in registered["actual_item_ids"]]
    draft = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=100_000,
        key="actual-draft-preview",
        evidence=evidence,
    )
    assert draft.status == "calculated"
    corrected = service.register_payroll_contribution_actual(
        _actual_request(
            organization,
            employee_id,
            evidence,
            key="actual-correct-draft",
            medical_employee_fen=1,
            medical_employer_fen=1,
            supersedes=original_ids,
        )
    )
    assert corrected["status"] == "registered"
    assert session.get(PayrollBatch, draft.batch_id).status == "superseded"
    current_ids = [uuid.UUID(value) for value in corrected["actual_item_ids"]]

    final_preview = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=100_000,
        key="actual-final-preview",
        evidence=evidence,
    )
    assert _confirm(service, organization, final_preview, "actual-final-confirm").status == "posted"
    blocked = service.register_payroll_contribution_actual(
        _actual_request(
            organization,
            employee_id,
            evidence,
            key="actual-correct-posted",
            supersedes=current_ids,
        )
    )
    assert blocked["errors"] == ["CONTRIBUTION_ACTUAL_POSTED_PAYROLL_MUST_BE_REVERSED_FIRST"]
    assert blocked["blocking_payroll_batch_ids"] == [str(final_preview.batch_id)]
    assert (
        session.scalar(
            select(PayrollContributionActualUse).where(
                PayrollContributionActualUse.payroll_batch_id == final_preview.batch_id
            )
        )
        is not None
    )


def test_historical_supplement_posts_now_without_rewriting_original_payroll(
    session: Session, organization: Organization
) -> None:
    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    july = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=1_000_000,
        key="supplement-july-preview",
        evidence=evidence,
    )
    confirmed = _confirm(service, organization, july, "supplement-july-confirm")
    assert confirmed.status == "posted"
    original_line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == july.batch_id)
    )
    assert original_line is not None
    original_snapshot = (
        original_line.employee_social_insurance_fen,
        original_line.employer_social_insurance_fen,
        original_line.calculation_trace,
    )

    supplement = service.record_payroll_contribution_supplement(
        RecordPayrollContributionSupplementRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "historical-medical-injury-supplement",
                "employee_id": employee_id,
                "contribution_period": "2026-07",
                "posting_date": "2026-08-31",
                "due_date": "2026-09-15",
                "assessment_reference": "HZ-SOCIAL-2026-08-0001",
                "reason_code": "missing_declaration",
                "reason_description": "8月确认补缴7月医保和工伤",
                "items": [
                    {
                        "contribution_group": "social_insurance",
                        "insurance_kind": "medical",
                        "employee_amount_fen": 10_000,
                        "employer_amount_fen": 47_500,
                        "employee_amount_treatment": "employee_receivable",
                    },
                    {
                        "contribution_group": "social_insurance",
                        "insurance_kind": "work_injury",
                        "employee_amount_fen": 0,
                        "employer_amount_fen": 2_000,
                        "employee_amount_treatment": "employer_borne",
                    },
                ],
                "evidence_references": [evidence.id],
            }
        )
    )
    assert supplement.status == "posted", supplement.model_dump(mode="json")
    replay = service.record_payroll_contribution_supplement(
        RecordPayrollContributionSupplementRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "historical-medical-injury-supplement",
                "employee_id": employee_id,
                "contribution_period": "2026-07",
                "posting_date": "2026-08-31",
                "due_date": "2026-09-15",
                "assessment_reference": "HZ-SOCIAL-2026-08-0001",
                "reason_code": "missing_declaration",
                "reason_description": "8月确认补缴7月医保和工伤",
                "items": [
                    {
                        "contribution_group": "social_insurance",
                        "insurance_kind": "medical",
                        "employee_amount_fen": 10_000,
                        "employer_amount_fen": 47_500,
                        "employee_amount_treatment": "employee_receivable",
                    },
                    {
                        "contribution_group": "social_insurance",
                        "insurance_kind": "work_injury",
                        "employee_amount_fen": 0,
                        "employer_amount_fen": 2_000,
                        "employee_amount_treatment": "employer_borne",
                    },
                ],
                "evidence_references": [evidence.id],
            }
        )
    )
    assert replay.event_id == supplement.event_id
    voucher = session.get(Voucher, supplement.voucher_id)
    assert voucher is not None
    by_role = {}
    for line in voucher.lines:
        debit, credit = by_role.get(line.account.system_role, (0, 0))
        by_role[line.account.system_role] = (debit + line.debit_fen, credit + line.credit_fen)
    assert by_role["payroll_management_expense"] == (49_500, 0)
    assert by_role["employee_receivable"] == (10_000, 0)
    assert by_role["employer_social_payable"] == (0, 49_500)
    assert by_role["withheld_employee_social_payable"] == (0, 10_000)
    items = session.scalars(
        select(OpenItem).where(OpenItem.source_event_id == supplement.event_id)
    ).all()
    assert sorted(
        (item.item_type, item.payable_category, item.original_amount_fen) for item in items
    ) == [
        ("payable", "employer_social", 2_000),
        ("payable", "employer_social", 47_500),
        ("payable", "withheld_employee_social", 10_000),
        ("receivable", None, 10_000),
    ]
    assert session.get(PayrollBatch, july.batch_id).status == "posted"
    assert (
        original_line.employee_social_insurance_fen,
        original_line.employer_social_insurance_fen,
        original_line.calculation_trace,
    ) == original_snapshot
    normalized = session.scalar(
        select(PayrollContributionSupplement).where(
            PayrollContributionSupplement.event_id == supplement.event_id
        )
    )
    assert normalized is not None
    assert normalized.contribution_period == "2026-07"

    payable_items = [item for item in items if item.item_type == "payable"]
    bank = add_bank_row(
        session,
        organization,
        -59_500,
        "supplement-social-payment",
        booking_date=date(2026, 9, 15),
    )
    payment_payload = payment_request(
        organization,
        event_type="social_insurance_payment",
        amount_fen=59_500,
        allocations=[
            {"open_item_id": item.id, "amount_fen": item.original_amount_fen}
            for item in payable_items
        ],
        bank=bank,
        key="supplement-social-payment",
    ).model_dump()
    payment_payload["business_dates"] = {
        "business_date": "2026-09-15",
        "payment_date": "2026-09-15",
        "posting_date": "2026-09-15",
    }
    payment = service.record_event(RecordEventRequest.model_validate(payment_payload))
    assert payment.status == "posted", payment.model_dump(mode="json")
    reversed_payment = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=payment.event_id,
            idempotency_key="reverse-supplement-social-payment",
            reason="测试先撤销现金缴款",
            posting_date=date(2026, 9, 16),
        )
    )
    assert reversed_payment.status == "posted"

    reversed_result = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=supplement.event_id,
            idempotency_key="reverse-historical-medical-injury-supplement",
            reason="撤销错误补缴认定",
            posting_date=date(2026, 9, 17),
        )
    )
    assert reversed_result.status == "posted"
    assert session.get(BusinessEvent, supplement.event_id).status == "reversed"
    assert session.get(PayrollBatch, july.batch_id).status == "posted"


def test_actual_rows_are_normalized_and_policy_is_not_polluted(
    session: Session, organization: Organization
) -> None:
    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    result = service.register_payroll_contribution_actual(
        _actual_request(organization, employee_id, evidence, key="normalized-actual")
    )
    actual_set = session.get(PayrollContributionActualSet, uuid.UUID(result["actual_set_id"]))
    assert actual_set is not None
    rows = session.scalars(
        select(PayrollContributionActualItem).where(
            PayrollContributionActualItem.actual_set_id == actual_set.id
        )
    ).all()
    assert {(row.insurance_kind, row.actual_state) for row in rows} == {
        ("medical", "not_declared"),
        ("work_injury", "not_declared"),
    }
    policy = session.scalar(
        select(PayrollPolicyVersion).where(PayrollPolicyVersion.org_id == organization.id)
    )
    assert policy is not None
    assert {rule["code"] for rule in policy.parameters["contribution_rules"]} == {
        "pension",
        "medical",
        "unemployment",
        "work_injury",
    }


def test_month_end_lists_specific_unapplied_insurance_kinds(
    session: Session, organization: Organization
) -> None:
    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    service.register_payroll_contribution_actual(
        _actual_request(organization, employee_id, evidence, key="checklist-actual")
    )
    period_service = AccountingPeriodService(session, current_date=date(2026, 8, 31))
    generated = period_service.generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=organization.id,
            period_month="2026-07",
            idempotency_key="checklist-period-july",
            confirmation_note="生成7月检查清单",
            evidence_references=[evidence.id],
        )
    )

    def payroll_item() -> dict[str, object]:
        close = period_service.preview_accounting_period_close(
            PreviewAccountingPeriodCloseRequest(
                org_id=organization.id,
                period_id=generated.period_id,
                closing_date=date(2026, 7, 31),
            )
        )
        return next(
            item
            for item in close.data["assistant_review_checklist"]["items"]
            if item["code"] == "MONTH_END_PEOPLE_PAYROLL_STATUTORY"
        )

    before = payroll_item()
    assert before["system_facts"]["contribution_actual_difference_count"] == 2
    assert before["system_facts"]["unapplied_contribution_actual_difference_count"] == 2
    contribution_question = next(
        question for question in before["owner_questions"] if "尚未进入本月工资批次" in question
    )
    assert "medical" in contribution_question
    assert "work_injury" in contribution_question

    preview = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=42_500,
        key="checklist-payroll-preview",
        evidence=evidence,
    )
    assert _confirm(service, organization, preview, "checklist-payroll-confirm").status == "posted"
    after = payroll_item()
    assert after["system_facts"]["contribution_actual_difference_count"] == 2
    assert after["system_facts"]["unapplied_contribution_actual_difference_count"] == 0
    assert all(
        fact["applied_to_current_payroll"]
        for fact in after["system_facts"]["contribution_actual_differences"]
    )
    assert after["state"] == "owner_confirmation_required"
    assert after["completed"] is False
    assert any("新入职、离职、停薪" in question for question in after["owner_questions"])
    assert after["system_facts"]["owner_workflow_question_codes"] == [
        "WORKFORCE_AND_PAY_CHANGES",
        "SOCIAL_INSURANCE_AND_HOUSING_FUND",
        "INDIVIDUAL_INCOME_TAX_WITHHOLDING",
    ]
    social_index = next(
        index
        for index, question in enumerate(after["owner_questions"])
        if "社保及公积金" in question
    )
    individual_income_tax_index = next(
        index
        for index, question in enumerate(after["owner_questions"])
        if "个税全员全额扣缴申报" in question
    )
    assert social_index < individual_income_tax_index


def test_first_wage_tax_treatment_is_evidenced_and_used_by_payroll(
    session: Session, organization: Organization
) -> None:
    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    registered = service.register_payroll_first_wage_tax_treatment(
        RegisterPayrollFirstWageTaxTreatmentRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "first-wage-treatment-2026",
                "employee_id": employee_id,
                "tax_year": 2026,
                "first_wage_month": 7,
                "treatment_state": "eligible",
                "declaration_date": "2026-08-25",
                "confirmation_description": (
                    "本年度此前未取得工资薪金，也未按累计预扣法预扣连续性劳务报酬个税"
                ),
                "evidence_references": [evidence.id],
            }
        )
    )
    assert registered["status"] == "registered"

    preview = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=1_000_000,
        key="first-wage-treatment-preview",
        evidence=evidence,
    )

    assert preview.status == "calculated", preview.model_dump(mode="json")
    assert preview.data["lines"][0]["individual_income_tax_fen"] == 0
    batch = session.get(PayrollBatch, preview.batch_id)
    assert batch is not None
    snapshot = batch.calculation_input["employee_snapshots"][0]["first_wage_tax_treatment"]
    assert snapshot["standard_deduction_start_month"] == 1
    use = session.scalar(
        select(PayrollFirstWageTaxTreatmentUse).where(
            PayrollFirstWageTaxTreatmentUse.payroll_batch_id == preview.batch_id
        )
    )
    assert use is not None
