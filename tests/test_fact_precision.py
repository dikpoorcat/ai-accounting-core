from copy import deepcopy

import pytest
from sqlalchemy import select
from test_business_components import sample_evidence as _evidence_fixture

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import BusinessEvent, Counterparty, OpenItem

sample_evidence = _evidence_fixture


def record(session, org, evidence, key, components, funds=(), day="2026-06-30"):
    return ComponentService(session).record(
        RecordEventRequest(
            org_id=org.id,
            idempotency_key=key,
            posting_date=day,
            evidence_references=[evidence.id],
            components=components,
            funds=list(funds),
        )
    )


def cash(key, day, amount, component, direction="payment"):
    return {
        "key": key,
        "account_code": "1001",
        "payment_date": day,
        "direction": direction,
        "amount_fen": amount,
        "allocations": [{"component_key": component, "amount_fen": amount}],
    }


@pytest.mark.parametrize("basis", ["supplier_credit", "person_advance"])
def test_monthly_expense_partial_settlement_and_metadata(
    session, organization, sample_evidence, basis
):
    component = {
        "key": "expense",
        "kind": "expense",
        "recognition_period": "2026-06",
        "amount_fen": 3230302,
        "expense_class": "labor_service_cost",
        "payment_basis": basis,
    }
    if basis == "person_advance":
        party = Counterparty(org_id=organization.id, kind="employee", name="Test payer")
        session.add(party)
        session.flush()
        component["payer"] = {"id": party.id}
    first = record(session, organization, sample_evidence, "june", [component])
    assert first.status == "posted", first
    event = session.get(BusinessEvent, first.event_id)
    facts = deepcopy(event.facts)
    assert facts["components"][0]["business_date"] is None
    assert facts["components"][0].get("payment_date") is None
    assert first.data["components"][0]["recognition"]["label"] == "按月确认"
    component["metadata"] = {"advance_payment_date": "2026-06-12", "description": "optional"}
    retried = record(session, organization, sample_evidence, "june", [component])
    assert retried.status == "posted" and retried.data["idempotent_replay"]
    assert event.facts == facts
    paid = record(
        session,
        organization,
        sample_evidence,
        "july",
        [
            {
                "key": "pay",
                "kind": "payable_settlement",
                "allocations": [
                    {
                        "source_event_key": "june",
                        "source_component_key": "expense",
                        "amount_fen": 3000302,
                    }
                ],
            }
        ],
        [cash("a", "2026-07-06", 1000000, "pay"), cash("b", "2026-07-07", 2000302, "pay")],
        day="2026-07-07",
    )
    assert paid.status == "posted", paid
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == first.event_id))
    assert item.settled_amount_fen == 3000302


def test_monthly_obligation_cannot_pay_before_cutoff(session, organization, sample_evidence):
    created = record(
        session,
        organization,
        sample_evidence,
        "source",
        [
            {
                "key": "expense",
                "kind": "expense",
                "recognition_period": "2026-06",
                "expense_class": "general_expense",
                "payment_basis": "supplier_credit",
                "amount_fen": 100,
            }
        ],
    )
    assert created.status == "posted", created
    refused = record(
        session,
        organization,
        sample_evidence,
        "early",
        [
            {
                "key": "pay",
                "kind": "payable_settlement",
                "allocations": [
                    {
                        "source_event_key": "source",
                        "source_component_key": "expense",
                        "amount_fen": 100,
                    }
                ],
            }
        ],
        [cash("cash", "2026-06-15", 100, "pay")],
    )
    assert refused.status == "needs_information", refused
    assert refused.missing_information == ["components.pay.source_recognized_by.2026-06-15"]
    assert len(session.scalars(select(BusinessEvent)).all()) == 1
    assert session.scalar(select(OpenItem)).settled_amount_fen == 0


def test_monthly_cutoff_not_before_period_end(session, organization, sample_evidence):
    refused = record(
        session,
        organization,
        sample_evidence,
        "future",
        [
            {
                "key": "expense",
                "kind": "expense",
                "recognition_period": "2026-06",
                "expense_class": "general_expense",
                "payment_basis": "supplier_credit",
                "amount_fen": 100,
            }
        ],
        day="2026-06-15",
    )
    assert refused.errors == ["RECOGNITION_PERIOD_IN_FUTURE"]
    assert not session.scalars(select(BusinessEvent)).all()


def test_monthly_personal_deposit_and_debt_transfer(session, organization, sample_evidence):
    party = Counterparty(org_id=organization.id, kind="employee", name="Test payer")
    session.add(party)
    session.flush()
    result = record(
        session,
        organization,
        sample_evidence,
        "combined",
        [
            {
                "key": "deposit",
                "kind": "refundable_deposit",
                "recognition_period": "2026-06",
                "advanced_by": {"id": party.id},
                "amount_fen": 200,
            },
            {
                "key": "cost",
                "kind": "project_cost",
                "recognition_period": "2026-06",
                "project_nature": "purchased_intangible",
                "cost_element": "purchase_price",
                "rights_controlled": True,
                "amount_fen": 100,
            },
            {
                "key": "transfer",
                "kind": "debt_transfer",
                "recognition_period": "2026-06",
                "payer": {"id": party.id},
                "allocations": [{"source_component_key": "cost", "amount_fen": 100}],
            },
        ],
    )
    assert result.status == "posted", result
    items = session.scalars(select(OpenItem)).all()
    assert (
        sum(
            i.original_amount_fen - i.settled_amount_fen
            for i in items
            if i.item_type == "payable" and i.counterparty_id == party.id
        )
        == 300
    )


def test_labor_total_without_breakdown_and_management_hash(session, organization, sample_evidence):
    from test_labor_remuneration_service import _register_person

    from ai_accounting.labor_remuneration_schemas import PreviewLaborRemunerationBatchRequest
    from ai_accounting.labor_remuneration_service import LaborRemunerationService
    from ai_accounting.models import LaborRemunerationBatch, LaborRemunerationLine

    person = _register_person(session, organization, sample_evidence, "TOTAL", "Total test")
    service = LaborRemunerationService(session)
    payload = {
        "org_id": organization.id,
        "idempotency_key": "labor-total",
        "remuneration_period": "2026-06",
        "business_date": "2026-06-30",
        "posting_date": "2026-06-30",
        "evidence_references": [sample_evidence.id],
        "items": [
            {
                "labor_person_id": person,
                "service_start_date": "2026-06-01",
                "service_end_date": "2026-06-30",
                "gross_remuneration_fen": 100000,
                "expense_role": "labor_service_cost",
                "tax_identity": "resident",
                "income_grouping": "continuous_monthly",
                "is_full_time_student": False,
            }
        ],
    }
    preview = service.preview_batch(PreviewLaborRemunerationBatchRequest.model_validate(payload))
    assert preview.status == "calculated", preview
    first_batch = session.get(LaborRemunerationBatch, preview.batch_id)
    first_line = session.scalar(
        select(LaborRemunerationLine).where(LaborRemunerationLine.batch_id == preview.batch_id)
    )
    assert first_line.fixed_fee_fen is None and first_line.commission_fen is None
    original = deepcopy(first_batch.calculation_input)
    payload["items"][0].update(
        external_declaration_status="confirmed",
        external_declaration_reference="optional",
        fixed_fee_fen=100000,
        commission_fen=0,
    )
    retried = service.preview_batch(PreviewLaborRemunerationBatchRequest.model_validate(payload))
    assert (
        retried.batch_id == preview.batch_id
        and retried.calculation_hash == preview.calculation_hash
    )
    _, new_input, _ = service._derive_batch(
        PreviewLaborRemunerationBatchRequest.model_validate(payload)
    )
    assert new_input == original


def test_payroll_registration_dates_are_optional_and_not_hashed(session, organization):
    from test_payroll_service import payroll_evidence, register_payroll_facts

    from ai_accounting.schemas import (
        RegisterPayrollContributionActualRequest,
        RegisterPayrollFirstWageTaxTreatmentRequest,
    )
    from ai_accounting.service import FinanceService

    employee = register_payroll_facts(session, organization)
    evidence = payroll_evidence(session, organization, "optional-registration-date")
    service = FinanceService(session)
    treatment = RegisterPayrollFirstWageTaxTreatmentRequest(
        org_id=organization.id,
        idempotency_key="first-wage-optional-date",
        employee_id=employee,
        tax_year=2026,
        first_wage_month=3,
        treatment_state="eligible",
        evidence_references=[evidence.id],
    )
    first = service.register_payroll_first_wage_tax_treatment(treatment)
    assert first["status"] == "registered", first
    dated = treatment.model_copy(
        update={"declaration_date": __import__("datetime").date(2026, 4, 1)}
    )
    assert service.register_payroll_first_wage_tax_treatment(dated)["status"] == "registered"
    actual = RegisterPayrollContributionActualRequest(
        org_id=organization.id,
        idempotency_key="actual-optional-date",
        employee_id=employee,
        contribution_period="2026-03",
        evidence_references=[evidence.id],
        items=[
            {
                "contribution_group": "social_insurance",
                "insurance_kind": "pension",
                "actual_state": "declared",
                "employee_amount_fen": 100,
                "employer_amount_fen": 200,
            }
        ],
    )
    result = service.register_payroll_contribution_actual(actual)
    assert result["status"] == "registered", result


def test_company_setup_and_backup_notes_are_optional():
    import uuid

    from ai_accounting.company_schemas import (
        ConfigureCloseBackupRequest,
        CreateCompanyRequest,
        PreviewCompanyStatusChangeRequest,
    )

    company = CreateCompanyRequest(
        name="资料精度测试公司",
        taxpayer_identification_number="91330106MA1234567T",
        effective_from="2026-01-01",
        filing_cycle="quarterly",
        urban_maintenance_rate="0.07",
        idempotency_key="company",
    )
    assert company.confirmation_note == ""
    assert (
        PreviewCompanyStatusChangeRequest(
            org_id=uuid.uuid4(), target_status="archived"
        ).confirmation_note
        == ""
    )
    assert (
        ConfigureCloseBackupRequest(
            org_id=uuid.uuid4(),
            backup_directory="D:/backups",
            idempotency_key="backup",
        ).confirmation_note
        == ""
    )
