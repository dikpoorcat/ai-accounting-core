from dataclasses import replace
from datetime import date

import pytest
from _postgres_helpers import authenticated_business_database
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_payroll_service import register_payroll_facts

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import AmendEventRequest, DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollEventLink,
    PayrollLine,
    PayrollTaxStateSlot,
    PayrollWithholdingEntitlement,
    Voucher,
)
from ai_accounting.schemas import PreviewPayrollRequest, ReverseEventRequest
from ai_accounting.service import FinanceService


def _preview_regular(session, org_id, employee_id, evidence_id, *, key, payroll_period="2026-03"):
    posting_date = f"{payroll_period}-05"
    result = FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": f"{key}-regular-preview",
                "batch_kind": "regular",
                "payroll_period": payroll_period,
                "posting_date": posting_date,
                "evidence_references": [evidence_id],
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
    assert result.status == "calculated", result
    return result


def _preview_combined_bonus(session, org_id, employee_id, evidence_id, regular, *, key):
    result = FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": f"{key}-bonus-preview",
                "batch_kind": "annual_bonus",
                "payroll_period": "2026-03",
                "posting_date": "2026-03-05",
                "payment_date": "2026-03-05",
                "tax_method": "combined",
                "evidence_references": [evidence_id],
                "employee_items": [
                    {
                        "employee_id": employee_id,
                        "annual_bonus_fen": 100_000,
                        "regular_payroll_batch_id": regular.batch_id,
                    }
                ],
            }
        )
    )
    assert result.status == "calculated", result
    return result


def _combined_request(org_id, evidence_id, regular, bonus, *, key):
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "posting_date": "2026-03-05",
            "description": "同月工资与并入综合所得年终奖组合确认",
            "components": [
                {
                    "key": "regular",
                    "kind": "payroll_accrual",
                    "business_date": "2026-03-05",
                    "batch_id": regular.batch_id,
                    "calculation_hash": regular.calculation_hash,
                    "evidence_references": [evidence_id],
                    "metadata": {"confirmation_note": "确认月度工资"},
                },
                {
                    "key": "bonus",
                    "kind": "payroll_accrual",
                    "business_date": "2026-03-05",
                    "batch_id": bonus.batch_id,
                    "calculation_hash": bonus.calculation_hash,
                    "regular_payroll_component_keys": ["regular"],
                    "evidence_references": [evidence_id],
                    "metadata": {"confirmation_note": "确认并入综合所得年终奖"},
                },
            ],
        }
    )


def _calculated_pair(session, org_id, employee_id, evidence_id, *, key):
    regular = _preview_regular(session, org_id, employee_id, evidence_id, key=key)
    bonus = _preview_combined_bonus(session, org_id, employee_id, evidence_id, regular, key=key)
    return regular, bonus


def _component_ids(session, event_id):
    return {
        component.key: component.id
        for component in session.scalars(
            select(BusinessEventComponent).where(BusinessEventComponent.event_id == event_id)
        )
    }


@pytest.mark.postgres
def test_combined_payroll_whole_amend_and_delete_preserve_then_remove_graph():
    with authenticated_business_database("combined_payroll_amend_delete") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            database_errors = []
            sqlalchemy_event.listen(
                engine,
                "handle_error",
                lambda context: database_errors.append(str(context.original_exception)),
            )
            organization = session.get(Organization, org_id)
            with authority.attributed_call(session, tool_name="finance_record_event"):
                employee_id = register_payroll_facts(session, organization)
                regular, bonus = _calculated_pair(
                    session, org_id, employee_id, evidence_id, key="lifecycle"
                )
                request = _combined_request(
                    org_id,
                    evidence_id,
                    regular,
                    bonus,
                    key="combined-payroll-lifecycle",
                )
                posted = ComponentService(session).record(request)
                assert posted.status == "posted", posted
            session.commit()

            batch_ids = {regular.batch_id, bonus.batch_id}
            component_ids = _component_ids(session, posted.event_id)
            payroll_line_ids = set(
                session.scalars(
                    select(PayrollLine.id).where(PayrollLine.payroll_batch_id.in_(batch_ids))
                )
            )
            regular_line_ids = sorted(
                str(line_id)
                for line_id in session.scalars(
                    select(PayrollLine.id).where(PayrollLine.payroll_batch_id == regular.batch_id)
                )
            )
            bonus_component = session.get(BusinessEventComponent, component_ids["bonus"])
            assert bonus_component.derived["local_regular_payroll_proofs"] == [
                {
                    "component_key": "regular",
                    "batch_id": str(regular.batch_id),
                    "calculation_hash": regular.calculation_hash,
                    "employee_line_ids": regular_line_ids,
                }
            ]
            voucher = session.scalar(select(Voucher).where(Voucher.event_id == posted.event_id))
            slot = session.scalar(
                select(PayrollTaxStateSlot).where(
                    PayrollTaxStateSlot.regular_batch_id == regular.batch_id
                )
            )
            assert slot.final_batch_id == bonus.batch_id
            original_ids = (voucher.id, slot.id, component_ids)

            replacement = request.model_copy(
                update={
                    "idempotency_key": "combined-payroll-lifecycle-replacement",
                    "description": "复核同月工资与年终奖组合确认",
                }
            )
            with authority.attributed_call(session, tool_name="finance_amend_event"):
                amended = EventAmendmentService(session).amend(
                    AmendEventRequest(
                        org_id=org_id,
                        event_id=posted.event_id,
                        idempotency_key="combined-payroll-whole-amend",
                        expected_facts_hash=posted.data["facts_hash"],
                        reason="复核组合计提",
                        replacement=replacement,
                    )
                )
                assert amended["status"] == "posted", (amended, database_errors)
            session.commit()

            current_slot = session.scalar(
                select(PayrollTaxStateSlot).where(
                    PayrollTaxStateSlot.regular_batch_id == regular.batch_id
                )
            )
            assert (
                amended["voucher_id"],
                current_slot.id,
                _component_ids(session, posted.event_id),
            ) == (str(original_ids[0]), original_ids[1], original_ids[2])
            assert current_slot.final_batch_id == bonus.batch_id
            assert {
                batch.id: (batch.status, batch.business_event_id)
                for batch in session.scalars(
                    select(PayrollBatch).where(PayrollBatch.id.in_(batch_ids))
                )
            } == {batch_id: ("posted", posted.event_id) for batch_id in batch_ids}

            with authority.attributed_call(session, tool_name="finance_delete_event"):
                deleted = EventAmendmentService(session).amend(
                    DeleteEventRequest(
                        org_id=org_id,
                        event_id=posted.event_id,
                        idempotency_key="combined-payroll-whole-delete",
                        expected_facts_hash=amended["facts_hash"],
                        reason="整笔组合计提误录",
                    )
                )
                assert deleted["status"] == "deleted", deleted
            session.commit()

            assert session.get(BusinessEvent, posted.event_id).status == "deleted"
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PayrollBatch)
                    .where(PayrollBatch.id.in_(batch_ids))
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PayrollTaxStateSlot)
                    .where(PayrollTaxStateSlot.regular_batch_id == regular.batch_id)
                )
                == 0
            )
            for model, predicate in (
                (BusinessEventComponent, BusinessEventComponent.event_id == posted.event_id),
                (PayrollEventLink, PayrollEventLink.event_id == posted.event_id),
                (OpenItem, OpenItem.source_event_id == posted.event_id),
                (Voucher, Voucher.event_id == posted.event_id),
            ):
                assert session.scalar(select(func.count()).select_from(model).where(predicate)) == 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PayrollWithholdingEntitlement)
                    .where(PayrollWithholdingEntitlement.payroll_line_id.in_(payroll_line_ids))
                )
                == 0
            )
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PayrollLine)
                    .where(PayrollLine.id.in_(payroll_line_ids))
                )
                == 0
            )


@pytest.mark.postgres
def test_combined_payroll_and_later_month_correct_in_one_group():
    with authenticated_business_database("combined_payroll_reverse") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            with authority.attributed_call(session, tool_name="finance_record_event"):
                employee_id = register_payroll_facts(session, organization)
                regular, bonus = _calculated_pair(
                    session, org_id, employee_id, evidence_id, key="reverse"
                )
                combined = ComponentService(session).record(
                    _combined_request(
                        org_id,
                        evidence_id,
                        regular,
                        bonus,
                        key="combined-payroll-to-reverse",
                    )
                )
                assert combined.status == "posted", combined
                april = _preview_regular(
                    session,
                    org_id,
                    employee_id,
                    evidence_id,
                    key="later",
                    payroll_period="2026-04",
                )
                later = ComponentService(session).record(
                    RecordEventRequest.model_validate(
                        {
                            "org_id": org_id,
                            "idempotency_key": "later-payroll-component",
                            "posting_date": "2026-04-05",
                            "components": [
                                {
                                    "key": "later-regular",
                                    "kind": "payroll_accrual",
                                    "business_date": "2026-04-05",
                                    "batch_id": april.batch_id,
                                    "calculation_hash": april.calculation_hash,
                                    "evidence_references": [evidence_id],
                                }
                            ],
                        }
                    )
                )
                assert later.status == "posted", later
            session.commit()

            with authority.attributed_call(session, tool_name="finance_reverse_event"):
                blocked = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=combined.event_id,
                        idempotency_key="combined-before-later-reverse",
                        posting_date=date(2026, 4, 6),
                        reason="先冲正仍有后续月份的组合计提",
                    )
                )
                assert blocked.status == "rejected", blocked
                assert blocked.errors == ["OPEN_PERIOD_REQUIRES_AMENDMENT"]
            from ai_accounting.corrections import CorrectionService
            from ai_accounting.event_amendment_schemas import (
                ConfirmCorrectionRequest,
                PreviewCorrectionRequest,
            )
            from ai_accounting.schemas import RegisterPayrollFirstWageTaxTreatmentRequest

            original_numbers = {v.id: v.voucher_number for v in session.scalars(select(Voucher))}
            correction = PreviewCorrectionRequest(
                org_id=org_id,
                source_changes=[
                    RegisterPayrollFirstWageTaxTreatmentRequest(
                        org_id=org_id,
                        employee_id=employee_id,
                        tax_year=2026,
                        first_wage_month=3,
                        treatment_state="eligible",
                        evidence_references=[evidence_id],
                        idempotency_key="combined-first-wage",
                    )
                ],
            )
            with authority.attributed_call(session, tool_name="finance_preview_correction"):
                preview = CorrectionService(session).preview(correction)
            assert preview["status"] == "calculated", preview
            assert len(preview["data"]["changes"]) == 2
            with authority.attributed_call(session, tool_name="finance_confirm_correction"):
                corrected = CorrectionService(session).confirm(
                    ConfirmCorrectionRequest(
                        **correction.model_dump(),
                        calculation_hash=preview["calculation_hash"],
                        idempotency_key="combined-correction",
                    )
                )
            assert corrected["status"] == "posted", corrected
            session.commit()
            assert {
                v.id: v.voucher_number for v in session.scalars(select(Voucher))
            } == original_numbers
            assert {
                session.get(PayrollBatch, batch_id).business_event_id
                for batch_id in (regular.batch_id, bonus.batch_id)
            } == {combined.event_id}
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PayrollTaxStateSlot)
                    .where(PayrollTaxStateSlot.employee_id == employee_id)
                )
                == 2
            )


@pytest.mark.postgres
def test_postgres_rejects_forged_combined_payroll_parent_proof_atomically(monkeypatch):
    with authenticated_business_database("combined_payroll_forged_proof") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            with authority.attributed_call(session, tool_name="finance_preview_payroll"):
                employee_id = register_payroll_facts(session, organization)
                regular, bonus = _calculated_pair(
                    session, org_id, employee_id, evidence_id, key="forged-proof"
                )
            session.commit()
            batch_ids = {regular.batch_id, bonus.batch_id}
            payroll_line_ids = set(
                session.scalars(
                    select(PayrollLine.id).where(PayrollLine.payroll_batch_id.in_(batch_ids))
                )
            )

            compile_original = FinanceService.compile_payroll_accrual_component

            def forged_compile(service, component, **kwargs):
                plan, evidence_ids = compile_original(service, component, **kwargs)
                if component.key != "bonus":
                    return plan, evidence_ids
                proofs = [
                    dict(proof) for proof in plan.derived.get("local_regular_payroll_proofs", [])
                ]
                assert len(proofs) == 1
                proofs[0]["calculation_hash"] = "0" * 64
                return (
                    replace(
                        plan,
                        derived=plan.derived | {"local_regular_payroll_proofs": proofs},
                    ),
                    evidence_ids,
                )

            monkeypatch.setattr(
                FinanceService,
                "compile_payroll_accrual_component",
                forged_compile,
            )
            request = _combined_request(
                org_id,
                evidence_id,
                regular,
                bonus,
                key="forged-combined-payroll-proof",
            )
            with authority.attributed_call(session, tool_name="finance_record_event"):
                forged = ComponentService(session).record(request)
                assert forged.status == "posted", forged
                with pytest.raises(
                    DBAPIError,
                    match="PAYROLL_LOCAL_REGULAR_PROOF_MISMATCH",
                ):
                    session.commit()
                session.rollback()

            assert (
                session.scalar(
                    select(BusinessEvent.id).where(
                        BusinessEvent.org_id == org_id,
                        BusinessEvent.idempotency_key == "forged-combined-payroll-proof",
                    )
                )
                is None
            )
            assert {
                batch.id: (batch.status, batch.business_event_id)
                for batch in session.scalars(
                    select(PayrollBatch).where(PayrollBatch.id.in_(batch_ids))
                )
            } == {batch_id: ("calculated", None) for batch_id in batch_ids}
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PayrollWithholdingEntitlement)
                    .where(PayrollWithholdingEntitlement.payroll_line_id.in_(payroll_line_ids))
                )
                == 0
            )
            for model, predicate in (
                (BusinessEventComponent, BusinessEventComponent.event_id == forged.event_id),
                (PayrollEventLink, PayrollEventLink.event_id == forged.event_id),
                (OpenItem, OpenItem.source_event_id == forged.event_id),
                (Voucher, Voucher.event_id == forged.event_id),
            ):
                assert session.scalar(select(func.count()).select_from(model).where(predicate)) == 0
            assert (
                session.scalar(
                    select(func.count())
                    .select_from(PayrollTaxStateSlot)
                    .where(PayrollTaxStateSlot.employee_id == employee_id)
                )
                == 0
            )
