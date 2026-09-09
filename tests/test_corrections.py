from __future__ import annotations

import uuid

import pytest
from sqlalchemy import func, select
from test_payroll_contribution_actuals import (
    _actual_request,
    _confirm,
    _preview,
    _setup_four_insurance_employee,
)

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.corrections import CorrectionService
from ai_accounting.event_amendment_schemas import ConfirmCorrectionRequest, PreviewCorrectionRequest
from ai_accounting.models import (
    BusinessCorrection,
    BusinessEvent,
    OpenItem,
    PayrollBatch,
    PayrollLine,
    Voucher,
)
from ai_accounting.schemas import RegisterPayrollFirstWageTaxTreatmentRequest


def test_database_failure_excludes_sql_parameters_and_unsafe_messages(caplog):
    from types import SimpleNamespace

    from sqlalchemy.exc import DBAPIError

    from ai_accounting.event_amendments import database_failure

    original = Exception("private SQL and values")
    original.sqlstate = "P0001"
    original.diag = SimpleNamespace(
        constraint_name="private SQL and values", message_primary="ACCOUNTING_PERIOD_private SQL"
    )
    result = database_failure(
        DBAPIError("private statement", {"secret": "private value"}, original)
    )
    assert result["errors"] == ["CORRECTION_INTERNAL_DATABASE_ERROR"]
    assert result["data"]["diagnostic_id"]
    assert "private" not in str(result) + caplog.text
    assert result["data"]["next_action"] == "inspect_failure"


def test_intervening_source_version_requires_new_preview(session, organization):
    finance, employee_id, proof = _setup_four_insurance_employee(session, organization)
    payroll = _preview(
        finance,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=800000,
        key="competing-source-payroll",
        evidence=proof,
    )
    assert _confirm(finance, organization, payroll, "competing-post").status == "posted"
    service = CorrectionService(session)
    first = PreviewCorrectionRequest(
        org_id=organization.id,
        source_changes=[_actual_request(organization, employee_id, proof, key="first-source")],
    )
    first_preview = service.preview(first)
    assert first_preview["status"] == "calculated", first_preview
    other = PreviewCorrectionRequest(
        org_id=organization.id,
        source_changes=[
            _actual_request(
                organization,
                employee_id,
                proof,
                key="other-source",
                medical_employee_fen=11000,
                medical_employer_fen=49000,
            )
        ],
    )
    other_preview = service.preview(other)
    assert other_preview["status"] == "calculated", other_preview
    changed = service.confirm(
        ConfirmCorrectionRequest(
            **other.model_dump(),
            calculation_hash=other_preview["calculation_hash"],
            idempotency_key="other-confirm",
        )
    )
    assert changed["status"] == "posted", changed
    stale = service.confirm(
        ConfirmCorrectionRequest(
            **first.model_dump(),
            calculation_hash=first_preview["calculation_hash"],
            idempotency_key="first-confirm",
        )
    )
    assert stale["errors"] == ["CORRECTION_PLAN_STALE"], stale
    assert stale["data"]["failure_kind"] == "version_conflict"
    assert session.scalar(select(func.count()).select_from(BusinessCorrection)) == 1


@pytest.mark.parametrize("existing_actual", [False, True])
def test_source_correction_preserves_payroll_and_voucher(session, organization, existing_actual):
    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    ids = []
    if existing_actual:
        registered = service.register_payroll_contribution_actual(
            _actual_request(organization, employee_id, evidence, key="initial")
        )
        ids = [uuid.UUID(value) for value in registered["actual_item_ids"]]
    payroll = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=800_000,
        key="payroll",
        evidence=evidence,
    )
    posted = _confirm(service, organization, payroll, "post-payroll")
    assert posted.status == "posted", posted
    voucher = session.scalar(select(Voucher).where(Voucher.event_id == posted.event_id))
    voucher_id, voucher_number = voucher.id, voucher.voucher_number
    original_facts = session.get(BusinessEvent, posted.event_id).facts
    original_line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == payroll.batch_id)
    )
    old_net = original_line.net_salary_fen
    change = _actual_request(
        organization,
        employee_id,
        evidence,
        key="updated",
        medical_employee_fen=12000,
        medical_employer_fen=48000,
        supersedes=ids,
    )
    change = change.model_copy(
        update={"declaration_date": None, "reason_code": None, "reason_description": ""}
    )
    direct = service.register_payroll_contribution_actual(change)
    assert direct["errors"] == ["SOURCE_CHANGE_REQUIRES_CORRECTION"]
    request = PreviewCorrectionRequest(org_id=organization.id, source_changes=[change])
    correction = CorrectionService(session)
    preview = correction.preview(request)
    assert preview["status"] == "calculated", preview
    assert session.get(BusinessEvent, posted.event_id).facts == original_facts
    assert session.scalar(select(func.count()).select_from(BusinessCorrection)) == 0
    confirmation = ConfirmCorrectionRequest(
        **request.model_dump(),
        idempotency_key="correct",
        calculation_hash=preview["calculation_hash"],
    )
    result = correction.confirm(confirmation)
    assert result["status"] == "posted", result
    session.expire_all()
    assert session.get(Voucher, voucher_id).voucher_number == voucher_number
    assert session.scalar(select(func.count()).select_from(Voucher)) == 1
    assert session.get(PayrollBatch, payroll.batch_id).business_event_id == posted.event_id
    line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == payroll.batch_id)
    )
    assert line.net_salary_fen != old_net
    assert correction.confirm(confirmation)["idempotent_replay"] is True


def test_linked_salary_tax_social_payment_and_later_month(session, organization):
    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    first = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=800_000,
        key="july",
        evidence=evidence,
    )
    source = _confirm(service, organization, first, "july-post")
    assert source.status == "posted", source
    items = list(
        session.scalars(select(OpenItem).where(OpenItem.source_event_id == source.event_id))
    )
    salary = next(item for item in items if item.payable_category == "salary")
    social = next(
        item
        for item in items
        if item.payable_category == "employer_social" and item.insurance_kind == "medical"
    )
    payment = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "pay",
            "posting_date": "2026-08-01",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "salary",
                    "kind": "salary_settlement",
                    "business_date": "2026-08-01",
                    "amount_fen": 98900,
                    "allocations": [{"open_item_id": salary.id, "amount_fen": 100000}],
                    "withholding_allocations": [
                        {
                            "open_item_id": salary.id,
                            "employee_social_insurance_items": {"pension": 1000},
                            "individual_income_tax_fen": 100,
                        }
                    ],
                },
                {
                    "key": "social",
                    "kind": "payable_settlement",
                    "business_date": "2026-08-01",
                    "allocations": [{"open_item_id": social.id, "amount_fen": 1000}],
                },
            ],
            "funds": [
                {
                    "key": "bank",
                    "account_code": "1002",
                    "direction": "payment",
                    "payment_date": "2026-08-01",
                    "amount_fen": 99900,
                    "allocations": [
                        {"component_key": "salary", "amount_fen": 98900},
                        {"component_key": "social", "amount_fen": 1000},
                    ],
                }
            ],
        }
    )
    paid = service.record_event(payment)
    assert paid.status == "posted", paid
    tax = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == paid.event_id,
            OpenItem.payable_category == "individual_income_tax",
        )
    )
    tax_request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "tax",
            "posting_date": "2026-08-02",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "tax",
                    "kind": "payable_settlement",
                    "business_date": "2026-08-02",
                    "allocations": [{"open_item_id": tax.id, "amount_fen": 100}],
                }
            ],
            "funds": [
                {
                    "key": "bank",
                    "account_code": "1002",
                    "direction": "payment",
                    "payment_date": "2026-08-02",
                    "amount_fen": 100,
                    "allocations": [{"component_key": "tax", "amount_fen": 100}],
                }
            ],
        }
    )
    tax_paid = service.record_event(tax_request)
    assert tax_paid.status == "posted", tax_paid
    later = _preview(
        service,
        organization,
        employee_id,
        period="2026-08",
        salary_fen=800_000,
        key="aug",
        evidence=evidence,
    )
    later_posted = _confirm(service, organization, later, "aug-post")
    assert later_posted.status == "posted", later_posted
    from ai_accounting.models import BusinessEventComponent

    payroll_component = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == source.event_id,
            BusinessEventComponent.kind == "payroll_accrual",
        )
    )
    supplement = service.record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "linked-supplement",
                "posting_date": "2026-08-29",
                "evidence_references": [evidence.id],
                "components": [
                    {
                        "key": "supplement",
                        "kind": "payroll_contribution_supplement",
                        "business_date": "2026-08-29",
                        "employee_id": employee_id,
                        "contribution_period": "2026-07",
                        "source": {"component_id": payroll_component.id},
                        "items": [
                            {
                                "contribution_group": "social_insurance",
                                "insurance_kind": "medical",
                                "employee_amount_fen": 0,
                                "employer_amount_fen": 100,
                                "employee_amount_treatment": "employer_borne",
                            }
                        ],
                    }
                ],
            }
        )
    )
    assert supplement.status == "posted", supplement
    supplement_facts = session.get(BusinessEvent, supplement.event_id).facts
    original_numbers = {v.id: v.voucher_number for v in session.scalars(select(Voucher))}
    old_payment_facts = session.get(BusinessEvent, paid.event_id).facts
    change = _actual_request(
        organization,
        employee_id,
        evidence,
        key="change-medical",
        medical_employee_fen=11000,
        medical_employer_fen=49000,
    )
    request = PreviewCorrectionRequest(org_id=organization.id, source_changes=[change])
    preview = CorrectionService(session).preview(request)
    assert preview["status"] == "calculated", preview
    assert len(preview["data"]["changes"]) == 5
    result = CorrectionService(session).confirm(
        ConfirmCorrectionRequest(
            **request.model_dump(),
            idempotency_key="linked",
            calculation_hash=preview["calculation_hash"],
        )
    )
    assert result["status"] == "posted", result
    assert {v.id: v.voucher_number for v in session.scalars(select(Voucher))} == original_numbers
    assert session.get(BusinessEvent, paid.event_id).facts == old_payment_facts
    assert session.get(OpenItem, social.id).settled_amount_fen == 1000
    assert session.get(OpenItem, tax.id).settled_amount_fen == 100
    assert session.get(BusinessEvent, supplement.event_id).facts == supplement_facts


def test_first_wage_correction_and_management_hash(session, organization):
    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    payroll = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=800_000,
        key="wage",
        evidence=evidence,
    )
    assert _confirm(service, organization, payroll, "wage-post").status == "posted"
    change = RegisterPayrollFirstWageTaxTreatmentRequest(
        org_id=organization.id,
        employee_id=employee_id,
        idempotency_key="first-wage",
        tax_year=2026,
        first_wage_month=7,
        treatment_state="eligible",
        evidence_references=[evidence.id],
    )
    request = PreviewCorrectionRequest(org_id=organization.id, source_changes=[change])
    service = CorrectionService(session)
    preview = service.preview(request)
    assert preview["status"] == "calculated", preview
    annotated = request.model_copy(
        update={
            "reason": "管理备注",
            "source_changes": [
                change.model_copy(update={"confirmation_description": "外部流程备注"})
            ],
        }
    )
    assert service.preview(annotated)["calculation_hash"] == preview["calculation_hash"]
    result = service.confirm(
        ConfirmCorrectionRequest(
            **annotated.model_dump(),
            idempotency_key="first",
            calculation_hash=preview["calculation_hash"],
        )
    )
    assert result["status"] == "posted", result


def test_changed_obligation_requires_payment_disposition_and_rolls_back(session, organization):
    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    payroll = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=800_000,
        key="full-paid",
        evidence=evidence,
    )
    posted = _confirm(service, organization, payroll, "full-paid-post")
    item = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == posted.event_id,
            OpenItem.payable_category == "employer_social",
            OpenItem.insurance_kind == "medical",
        )
    )
    amount = item.original_amount_fen
    paid = service.record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "medical-paid",
                "posting_date": "2026-08-01",
                "evidence_references": [evidence.id],
                "components": [
                    {
                        "key": "medical",
                        "kind": "payable_settlement",
                        "business_date": "2026-08-01",
                        "allocations": [{"open_item_id": item.id, "amount_fen": amount}],
                    }
                ],
                "funds": [
                    {
                        "key": "bank",
                        "account_code": "1002",
                        "direction": "payment",
                        "payment_date": "2026-08-01",
                        "amount_fen": amount,
                        "allocations": [{"component_key": "medical", "amount_fen": amount}],
                    }
                ],
            }
        )
    )
    assert paid.status == "posted", paid
    source_facts = session.get(BusinessEvent, posted.event_id).facts
    change = _actual_request(
        organization,
        employee_id,
        evidence,
        key="lower-medical",
        medical_employee_fen=1000,
        medical_employer_fen=1000,
    )
    preview = CorrectionService(session).preview(
        PreviewCorrectionRequest(org_id=organization.id, source_changes=[change])
    )
    assert preview["status"] == "needs_information", preview
    assert preview["data"]["fact_issues"][0]["code"] == "CORRECTION_PAYMENT_DISPOSITION_REQUIRED"
    assert session.get(BusinessEvent, posted.event_id).facts == source_facts
    assert session.get(OpenItem, item.id).settled_amount_fen == amount
    assert session.scalar(select(func.count()).select_from(BusinessCorrection)) == 0


def test_confirm_failure_and_stale_plan_are_atomic(session, organization, monkeypatch):
    from ai_accounting.event_amendments import EventAmendmentService
    from ai_accounting.models import PayrollContributionActualSet

    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    payroll = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=800_000,
        key="atomic",
        evidence=evidence,
    )
    posted = _confirm(service, organization, payroll, "atomic-post")
    change = _actual_request(
        organization,
        employee_id,
        evidence,
        key="atomic-actual",
        medical_employee_fen=10000,
        medical_employer_fen=49000,
    )
    request = PreviewCorrectionRequest(org_id=organization.id, source_changes=[change])
    service = CorrectionService(session)
    preview = service.preview(request)
    assert preview["status"] == "calculated", preview
    confirmation = ConfirmCorrectionRequest(
        **request.model_dump(),
        calculation_hash=preview["calculation_hash"],
        idempotency_key="atomic-confirm",
    )
    stale = service.confirm(confirmation.model_copy(update={"calculation_hash": "0" * 64}))
    assert stale["errors"] == ["CORRECTION_PLAN_STALE"]
    from ai_accounting.service import FinanceService

    item = session.scalar(
        select(OpenItem).where(
            OpenItem.source_event_id == posted.event_id,
            OpenItem.payable_category == "employer_social",
            OpenItem.insurance_kind == "medical",
        )
    )
    paid = FinanceService(session).record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "posting_date": "2026-08-01",
                "idempotency_key": "new-consumer",
                "evidence_references": [evidence.id],
                "components": [
                    {
                        "key": "pay",
                        "kind": "payable_settlement",
                        "business_date": "2026-08-01",
                        "allocations": [{"open_item_id": item.id, "amount_fen": 100}],
                    }
                ],
                "funds": [
                    {
                        "key": "cash",
                        "account_code": "1001",
                        "direction": "payment",
                        "payment_date": "2026-08-01",
                        "amount_fen": 100,
                        "allocations": [{"component_key": "pay", "amount_fen": 100}],
                    }
                ],
            }
        )
    )
    assert paid.status == "posted", paid
    assert service.confirm(confirmation)["errors"] == ["CORRECTION_PLAN_STALE"]
    assert session.get(OpenItem, item.id).settled_amount_fen == 100
    preview = service.preview(request)
    assert preview["status"] == "calculated", preview
    confirmation = confirmation.model_copy(update={"calculation_hash": preview["calculation_hash"]})
    original = session.get(BusinessEvent, posted.event_id).facts
    complete = EventAmendmentService.complete

    def fail_after_post(self, state):
        complete(self, state)
        raise ValueError("INJECTED_COMPONENT_FAILURE")

    monkeypatch.setattr(EventAmendmentService, "complete", fail_after_post)
    result = service.confirm(confirmation)
    assert result["errors"] == ["INJECTED_COMPONENT_FAILURE"]
    assert session.get(BusinessEvent, posted.event_id).facts == original
    assert session.scalar(select(func.count()).select_from(PayrollContributionActualSet)) == 0
    assert session.scalar(select(func.count()).select_from(BusinessCorrection)) == 0


def test_cyclic_dependency_is_rejected_before_any_correction_write(session, organization):
    from test_business_components import expense, request
    from test_payroll_service import payroll_evidence

    from ai_accounting.accounting_periods import canonical_sha256
    from ai_accounting.event_amendment_schemas import CorrectionEventReplacement
    from ai_accounting.models import BusinessEventDependency
    from ai_accounting.service import FinanceService

    sample_evidence = payroll_evidence(session, organization, "cyclic-evidence")
    posted = [
        FinanceService(session).record_event(
            request(organization, sample_evidence, [expense(f"expense-{i}", 100)], key=f"cycle-{i}")
        )
        for i in range(2)
    ]
    assert all(result.status == "posted" for result in posted), posted
    # Simulate a corrupt imported graph at the SQLite storage boundary. The
    # correction planner must reject it rather than attempting either write.
    for parent, child in (posted, list(reversed(posted))):
        session.add(
            BusinessEventDependency(
                org_id=organization.id,
                parent_event_id=parent.event_id,
                child_event_id=child.event_id,
                dependency_kind="component_source",
                amount_fen=1,
            )
        )
    session.flush()
    source = session.get(BusinessEvent, posted[0].event_id)
    correction = PreviewCorrectionRequest(
        org_id=organization.id,
        event_replacements=[
            CorrectionEventReplacement(
                event_id=source.id,
                expected_facts_hash=canonical_sha256(source.facts),
                replacement=RecordEventRequest.model_validate(source.facts),
            )
        ],
    )
    result = CorrectionService(session).preview(correction)
    assert result["errors"] == ["CORRECTION_DEPENDENCY_CYCLE"], result
    assert session.scalar(select(func.count()).select_from(BusinessCorrection)) == 0
    assert session.scalar(select(func.count()).select_from(Voucher)) == 2


@pytest.mark.parametrize("replace_salary", [False, True])
def test_source_correction_rebuilds_combined_bonus_tax_dependency(
    session, organization, replace_salary
):
    from datetime import date

    from sqlalchemy import event

    database_errors = []
    event.listen(
        session.get_bind(),
        "handle_error",
        lambda ctx: database_errors.append((str(ctx.original_exception), ctx.statement)),
    )
    from ai_accounting.models import PayrollTaxStateSlot
    from ai_accounting.schemas import PreviewPayrollRequest

    service, employee_id, evidence = _setup_four_insurance_employee(session, organization)
    regular = _preview(
        service,
        organization,
        employee_id,
        period="2026-07",
        salary_fen=800_000,
        key="regular-bonus",
        evidence=evidence,
    )
    posted_regular = _confirm(service, organization, regular, "regular-bonus-post")
    assert posted_regular.status == "posted"
    bonus = service.preview_payroll(
        PreviewPayrollRequest(
            org_id=organization.id,
            idempotency_key="bonus",
            batch_kind="annual_bonus",
            payroll_period="2026-07",
            posting_date=date(2026, 7, 29),
            payment_date=date(2026, 7, 29),
            tax_method="combined",
            employee_items=[
                {
                    "employee_id": employee_id,
                    "annual_bonus_fen": 100000,
                    "regular_payroll_batch_id": regular.batch_id,
                }
            ],
            evidence_references=[evidence.id],
        )
    )
    assert bonus.status == "calculated", bonus
    assert _confirm(service, organization, bonus, "bonus-post").status == "posted"
    change = _actual_request(
        organization,
        employee_id,
        evidence,
        key="bonus-actual",
        medical_employee_fen=11000,
        medical_employer_fen=49000,
    )
    request = PreviewCorrectionRequest(org_id=organization.id, source_changes=[change])
    if replace_salary:
        from ai_accounting.accounting_periods import canonical_sha256
        from ai_accounting.event_amendment_schemas import CorrectionEventReplacement

        updated = PreviewPayrollRequest.model_validate(
            session.get(PayrollBatch, regular.batch_id).calculation_input["request"]
        )
        updated.employee_items[0].tax_reported_salary_fen += 10000
        request = PreviewCorrectionRequest(
            org_id=organization.id,
            event_replacements=[
                CorrectionEventReplacement(
                    event_id=posted_regular.event_id,
                    expected_facts_hash=canonical_sha256(
                        session.get(BusinessEvent, posted_regular.event_id).facts
                    ),
                    replacement=updated,
                )
            ],
        )
    preview = CorrectionService(session).preview(request)
    assert preview["status"] == "calculated", (preview, database_errors)
    result = CorrectionService(session).confirm(
        ConfirmCorrectionRequest(
            **request.model_dump(),
            calculation_hash=preview["calculation_hash"],
            idempotency_key="bonus-correction",
        )
    )
    assert result["status"] == "posted", result
    slot = session.scalar(select(PayrollTaxStateSlot))
    assert slot.regular_batch_id == regular.batch_id
    assert slot.final_batch_id == bonus.batch_id


def test_source_correction_preserves_unrelated_labor_in_mixed_event(session, organization):
    from test_confirmed_accrual_components import _accruals, _calculated_batches

    from ai_accounting.models import LaborRemunerationBatch, LaborRemunerationLine
    from ai_accounting.service import FinanceService

    payroll, proof, labor, labor_proof = _calculated_batches(session, organization)
    result = FinanceService(session).record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "mixed",
                "posting_date": "2026-03-05",
                "components": _accruals(payroll, proof, labor, labor_proof),
                "funds": [],
                "evidence_references": [proof.id, labor_proof.id],
            }
        )
    )
    assert result.status == "posted", result
    labor_line = session.scalar(select(LaborRemunerationLine))
    labor_values = {c.name: getattr(labor_line, c.name) for c in labor_line.__table__.columns}
    employee_id = session.scalar(select(PayrollLine.employee_id))
    request = PreviewCorrectionRequest(
        org_id=organization.id,
        source_changes=[
            RegisterPayrollFirstWageTaxTreatmentRequest(
                org_id=organization.id,
                employee_id=employee_id,
                idempotency_key="mixed-first",
                tax_year=2026,
                first_wage_month=3,
                treatment_state="eligible",
                evidence_references=[proof.id],
            )
        ],
    )
    preview = CorrectionService(session).preview(request)
    assert preview["status"] == "calculated", preview
    confirmed = CorrectionService(session).confirm(
        ConfirmCorrectionRequest(
            **request.model_dump(),
            calculation_hash=preview["calculation_hash"],
            idempotency_key="mixed-correct",
        )
    )
    assert confirmed["status"] == "posted", confirmed
    assert session.get(LaborRemunerationBatch, labor.batch_id).business_event_id == result.event_id
    line = session.get(LaborRemunerationLine, labor_line.id)
    assert {c.name: getattr(line, c.name) for c in line.__table__.columns} == labor_values
    assert session.scalar(select(func.count()).select_from(Voucher)) == 1
