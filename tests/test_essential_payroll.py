from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select
from test_labor_remuneration_service import _evidence, _register_person
from test_payroll_service import payroll_evidence, payroll_parameters, register_payroll_facts

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.labor_remuneration_schemas import (
    ConfirmLaborRemunerationBatchRequest,
    EndLaborServicePersonRequest,
    LaborRemunerationItemFacts,
    PreviewLaborRemunerationBatchRequest,
)
from ai_accounting.labor_remuneration_service import LaborRemunerationService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    OpenItem,
    PayrollBatch,
    PayrollContributionSupplement,
    PayrollEventLink,
    PayrollLine,
    PayrollWithholdingEntitlement,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
)
from ai_accounting.service import FinanceService


def test_payroll_policy_registration_does_not_require_payment_agency_metadata(
    session, organization
):
    parameters = payroll_parameters()
    parameters.pop("payment_targets")
    result = FinanceService(session).register_payroll_policy_version(
        RegisterPayrollPolicyVersionRequest(
            org_id=organization.id,
            region="必要事实测试地区",
            effective_from=date(2026, 1, 1),
            effective_to=date(2026, 12, 31),
            version="essential-2026",
            source_url="https://www.chinatax.gov.cn/",
            parameters=parameters,
        )
    )
    assert result["status"] == "registered", result


def _post_payroll(session, organization, employee_id, period: str, sequence: int):
    evidence = payroll_evidence(session, organization, f"essential-payroll-{sequence}")
    service = FinanceService(session)
    preview = service.preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": f"essential-preview-{sequence}",
                "batch_kind": "regular",
                "payroll_period": period,
                "posting_date": f"2026-{sequence + 2:02d}-28",
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "tax_reported_salary_fen": 1_000_000,
                        "special_additional_deduction_fen": 0,
                        "other_legal_deduction_fen": 0,
                        "tax_relief_fen": 0,
                    }
                ],
                "evidence_references": [evidence.id],
            }
        )
    )
    assert preview.status == "calculated", preview
    posted = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key=f"essential-confirm-{sequence}",
        )
    )
    assert posted.status == "posted", posted
    return posted, evidence


@pytest.mark.parametrize("split_dates", [False, True])
def test_salary_payment_combines_multiple_payroll_batches_without_agency_metadata(
    session, organization, split_dates
):
    employee_id = register_payroll_facts(session, organization)
    march, march_evidence = _post_payroll(session, organization, employee_id, "2026-03", 1)
    april, april_evidence = _post_payroll(session, organization, employee_id, "2026-04", 2)
    batches = [session.get(PayrollBatch, march.batch_id), session.get(PayrollBatch, april.batch_id)]
    salary_items = list(
        session.scalars(
            select(OpenItem)
            .where(
                OpenItem.source_event_id.in_([march.event_id, april.event_id]),
                OpenItem.payable_category == "salary",
            )
            .order_by(OpenItem.id)
        )
    )
    lines = {
        line.payroll_batch_id: line
        for line in session.scalars(
            select(PayrollLine).where(
                PayrollLine.payroll_batch_id.in_([batch.id for batch in batches])
            )
        )
    }
    withholdings = []
    stable_withholdings = []
    cash_sources = []
    stable_cash_sources = []
    stable_allocations = []
    cash_fen = 0
    for item in salary_items:
        batch = next(batch for batch in batches if batch.business_event_id == item.source_event_id)
        source_event = session.get(BusinessEvent, item.source_event_id)
        source_component = session.get(BusinessEventComponent, item.source_component_id)
        stable_source = {
            "source_event_key": source_event.idempotency_key,
            "source_component_key": source_component.key,
            "source_open_item_key": item.component_key,
        }
        line = lines[batch.id]
        entitlements = list(
            session.scalars(
                select(PayrollWithholdingEntitlement).where(
                    PayrollWithholdingEntitlement.payroll_line_id == line.id
                )
            )
        )
        social = {
            row.insurance_kind: row.amount_fen
            for row in entitlements
            if row.contribution_group == "employee_social_insurance"
        }
        housing = {
            row.insurance_kind: row.amount_fen
            for row in entitlements
            if row.contribution_group == "employee_housing_fund"
        }
        income_tax = sum(
            row.amount_fen
            for row in entitlements
            if row.contribution_group == "individual_income_tax"
        )
        withheld = sum(social.values()) + sum(housing.values()) + income_tax
        net_cash = item.original_amount_fen - withheld
        cash_fen += net_cash
        withholding = {
            "employee_social_insurance_items": social,
            "employee_housing_fund_items": housing,
            "individual_income_tax_fen": income_tax,
        }
        withholdings.append({"open_item_id": item.id, **withholding})
        stable_withholdings.append({**stable_source, **withholding})
        cash_sources.append({"open_item_id": item.id, "amount_fen": net_cash})
        stable_cash_sources.append({**stable_source, "amount_fen": net_cash})
        stable_allocations.append({**stable_source, "amount_fen": item.original_amount_fen})

    request_payload = {
        "org_id": organization.id,
        "idempotency_key": "essential-two-batch-salary-payment",
        "posting_date": date(2026, 5, 5),
        "evidence_references": [march_evidence.id, april_evidence.id],
        "components": [
            {
                "key": "salary",
                "kind": "salary_settlement",
                "business_date": date(2026, 5, 5),
                "amount_fen": cash_fen,
                "allocations": [
                    {
                        "open_item_id": item.id,
                        "amount_fen": item.original_amount_fen,
                    }
                    for item in salary_items
                ],
                "withholding_allocations": withholdings,
            }
        ],
        "funds": [
            {
                "key": "cash",
                "account_code": "1001",
                "direction": "payment",
                "payment_date": date(2026, 5, 5),
                "amount_fen": cash_fen,
                "allocations": [
                    {
                        "component_key": "salary",
                        "amount_fen": cash_fen,
                        "source_allocations": cash_sources,
                    }
                ],
            }
        ],
    }
    if split_dates:
        request_payload["funds"] = [
            {
                "key": f"cash-{index}",
                "account_code": "1001",
                "direction": "payment",
                "payment_date": date(2026, 5, 4 + index),
                "amount_fen": source["amount_fen"],
                "allocations": [
                    {
                        "component_key": "salary",
                        "amount_fen": source["amount_fen"],
                        "source_allocations": [source],
                    }
                ],
            }
            for index, source in enumerate(cash_sources)
        ]
    service = FinanceService(session)
    result = service.record_event(RecordEventRequest.model_validate(request_payload))
    assert result.status == "posted", result
    component = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == result.event_id,
            BusinessEventComponent.key == "salary",
        )
    )
    assert set(component.derived["payroll_batch_ids"]) == {str(batch.id) for batch in batches}
    links = list(
        session.scalars(
            select(PayrollEventLink).where(
                PayrollEventLink.event_id == result.event_id,
                PayrollEventLink.link_kind == "salary_payment",
            )
        )
    )
    assert {link.payroll_batch_id for link in links} == {batch.id for batch in batches}
    statutory = list(
        session.scalars(
            select(OpenItem).where(
                OpenItem.source_event_id == result.event_id,
                OpenItem.payable_category.in_(
                    [
                        "withheld_employee_social",
                        "withheld_employee_housing",
                        "individual_income_tax",
                    ]
                ),
            )
        )
    )
    assert statutory
    assert all(item.counterparty_id is None and item.due_date is None for item in statutory)
    stable_request_payload = {
        **request_payload,
        "components": [
            {
                **request_payload["components"][0],
                "allocations": stable_allocations,
                "withholding_allocations": stable_withholdings,
            }
        ],
        "funds": [
            {
                **fund,
                "allocations": [
                    {
                        **allocation,
                        "source_allocations": [
                            stable_cash_sources[cash_sources.index(source)]
                            for source in allocation["source_allocations"]
                        ],
                    }
                    for allocation in fund["allocations"]
                ],
            }
            for fund in request_payload["funds"]
        ],
    }
    replay = service.record_event(RecordEventRequest.model_validate(stable_request_payload))
    assert replay.status == "posted", replay
    assert replay.event_id == result.event_id
    assert replay.data["idempotent_replay"] is True
    assert replay.data["created_open_items"] == result.data["created_open_items"]


def test_contribution_supplement_needs_no_batch_reference_or_management_text(session, organization):
    employee_id = register_payroll_facts(session, organization)
    evidence = payroll_evidence(session, organization, "essential-supplement")
    result = FinanceService(session).record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "essential-supplement",
                "posting_date": date(2026, 4, 30),
                "evidence_references": [evidence.id],
                "components": [
                    {
                        "key": "supplement",
                        "kind": "payroll_contribution_supplement",
                        "business_date": date(2026, 4, 30),
                        "employee_id": employee_id,
                        "contribution_period": "2026-03",
                        "items": [
                            {
                                "contribution_group": "social_insurance",
                                "insurance_kind": "pension",
                                "employee_amount_fen": 10,
                                "employer_amount_fen": 30,
                                "employee_amount_treatment": "employee_receivable",
                            }
                        ],
                    }
                ],
            }
        )
    )
    assert result.status == "posted", result
    supplement = session.scalar(
        select(PayrollContributionSupplement).where(
            PayrollContributionSupplement.event_id == result.event_id
        )
    )
    assert supplement.source_payroll_batch_id is None
    assert supplement.assessment_reference is None
    assert supplement.reason_description is None
    assert session.get(BusinessEvent, result.event_id).status == "posted"


def test_prior_labor_service_can_post_after_employee_record_exists_without_management_fields(
    session, organization
):
    evidence = _evidence(session, organization, "z")
    person_id = _register_person(
        session,
        organization,
        evidence,
        "LABOR-PRIOR",
        "历史劳务转员工",
    )
    labor = LaborRemunerationService(session)
    ended = labor.end_person(
        EndLaborServicePersonRequest(
            org_id=organization.id,
            labor_person_id=person_id,
            relationship_end_date=date(2026, 8, 31),
            idempotency_key="essential-prior-labor-end",
            evidence_references=[evidence.id],
        )
    )
    assert ended.status == "registered"
    employee = FinanceService(session).register_employee(
        RegisterEmployeeRequest(
            org_id=organization.id,
            employee_code="ESSENTIAL-LABOR-E001",
            name="历史劳务转员工",
            employment_start_date=date(2026, 9, 1),
            prior_labor_person_id=person_id,
        )
    )
    assert employee["status"] == "registered"
    preview = labor.preview_batch(
        PreviewLaborRemunerationBatchRequest(
            org_id=organization.id,
            idempotency_key="essential-prior-labor-preview",
            remuneration_period="2026-08",
            business_date=date(2026, 8, 31),
            posting_date=date(2026, 8, 31),
            items=[
                LaborRemunerationItemFacts(
                    labor_person_id=person_id,
                    service_start_date=date(2026, 8, 1),
                    service_end_date=date(2026, 8, 31),
                    fixed_fee_fen=100_000,
                    commission_fen=0,
                    expense_role="labor_service_cost",
                    tax_identity="resident",
                    income_grouping="single_occurrence",
                    is_full_time_student=False,
                )
            ],
            evidence_references=[evidence.id],
        )
    )
    assert preview.status == "calculated", preview
    posted = labor.confirm_batch(
        ConfirmLaborRemunerationBatchRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="essential-prior-labor-confirm",
        )
    )
    assert posted.status == "posted", posted
    obligation = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == posted.event_id,
            OpenItem.payable_category == "labor_remuneration",
        )
    )
    assert obligation is not None and obligation.due_date is None
