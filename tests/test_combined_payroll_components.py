from __future__ import annotations

import uuid
from contextlib import nullcontext

import pytest
from _postgres_helpers import authenticated_business_database
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_payroll_service import payroll_evidence, register_payroll_facts

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    Evidence,
    Organization,
    PayrollBatch,
    PayrollEventLink,
    PayrollLine,
    PayrollTaxStateSlot,
)
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
)
from ai_accounting.service import FinanceService


def _call(authority, session: Session, tool_name: str):
    return (
        authority.attributed_call(session, tool_name=tool_name)
        if authority is not None
        else nullcontext()
    )


def _register_employee(session, organization, authority=None):
    with _call(authority, session, "finance_register_payroll_facts"):
        return register_payroll_facts(session, organization)


def _register_additional_employee(session, organization, authority=None):
    with _call(authority, session, "finance_register_payroll_facts"):
        service = FinanceService(session)
        result = service.register_employee(
            RegisterEmployeeRequest(
                org_id=organization.id,
                employee_code="E-002",
                name="李四",
                employment_start_date="2026-03-01",
                tax_withholding_start_date="2026-03-01",
                status="active",
            )
        )
        employee_id = uuid.UUID(result["employee_id"])
        profile = service.register_employee_payroll_profile_version(
            RegisterEmployeePayrollProfileVersionRequest(
                org_id=organization.id,
                employee_id=employee_id,
                effective_from="2026-03-01",
                expense_role="payroll_management_expense",
                social_insurance_base_fen=1_000_000,
                housing_fund_base_fen=1_000_000,
                resident_employee=True,
            )
        )
        assert profile["status"] == "registered", profile
    return employee_id


def _preview_regular(
    session: Session,
    organization: Organization,
    evidence: Evidence,
    employee_id,
    *,
    key: str,
    authority=None,
):
    request = PreviewPayrollRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "batch_kind": "regular",
            "payroll_period": "2026-03",
            "posting_date": "2026-03-05",
            "evidence_references": [evidence.id],
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
    with _call(authority, session, "finance_preview_payroll"):
        result = FinanceService(session).preview_payroll(request)
    assert result.status == "calculated", result
    return result


def _preview_combined_bonus(
    session: Session,
    organization: Organization,
    evidence: Evidence,
    employee_id,
    regular_batch_id,
    *,
    key: str,
    payroll_period: str = "2026-03",
    authority=None,
):
    request = PreviewPayrollRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "batch_kind": "annual_bonus",
            "payroll_period": payroll_period,
            "posting_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "tax_method": "combined",
            "evidence_references": [evidence.id],
            "employee_items": [
                {
                    "employee_id": employee_id,
                    "annual_bonus_fen": 500_000,
                    "regular_payroll_batch_id": regular_batch_id,
                }
            ],
        }
    )
    with _call(authority, session, "finance_preview_payroll"):
        result = FinanceService(session).preview_payroll(request)
    assert result.status == "calculated", result
    return result


def _combined_event_request(organization, evidence, regular, *bonuses):
    components = [
        {
            "key": "regular",
            "kind": "payroll_accrual",
            "business_date": "2026-03-05",
            "batch_id": regular.batch_id,
            "calculation_hash": regular.calculation_hash,
            "metadata": {"confirmation_note": "同事件确认常规工资"},
        }
    ]
    components.extend(
        (
            {
                "key": f"bonus-{index}",
                "kind": "payroll_accrual",
                "business_date": "2026-03-05",
                "batch_id": bonus.batch_id,
                "calculation_hash": bonus.calculation_hash,
                "regular_payroll_component_keys": ["regular"],
                "metadata": {"confirmation_note": "同事件确认并入工资计税的年终奖"},
            }
            for index, bonus in enumerate(bonuses, 1)
        )
    )
    # Put the dependent first to prove the typed local key, rather than array order,
    # controls compilation.
    components = [*components[1:], components[0]]
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "combined-payroll-one-event",
            "posting_date": "2026-03-05",
            "evidence_references": [evidence.id],
            "components": components,
        }
    )


def _assert_combined_result(session, organization, regular, bonus, event_id):
    regular_batch = session.get(PayrollBatch, regular.batch_id)
    bonus_batch = session.get(PayrollBatch, bonus.batch_id)
    assert regular_batch.status == bonus_batch.status == "posted"
    assert regular_batch.business_event_id == bonus_batch.business_event_id == event_id
    regular_line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == regular.batch_id)
    )
    bonus_line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == bonus.batch_id)
    )
    assert bonus_line.regular_payroll_batch_id == regular.batch_id
    snapshot = bonus_batch.calculation_input["employee_snapshots"][0]
    assert snapshot["regular_payroll_batch_id"] == str(regular.batch_id)
    assert snapshot["regular_payroll_line_id"] == str(regular_line.id)
    assert snapshot["regular_payroll_calculation_hash"] == regular.calculation_hash
    slot = session.scalar(
        select(PayrollTaxStateSlot).where(
            PayrollTaxStateSlot.org_id == organization.id,
            PayrollTaxStateSlot.employee_id == regular_line.employee_id,
            PayrollTaxStateSlot.tax_year == 2026,
            PayrollTaxStateSlot.tax_month == 3,
        )
    )
    assert slot.regular_batch_id == regular.batch_id
    assert slot.final_batch_id == bonus.batch_id
    components = {
        component.key: component
        for component in session.scalars(
            select(BusinessEventComponent).where(BusinessEventComponent.event_id == event_id)
        )
    }
    assert components["bonus-1"].derived["local_regular_payroll_proofs"] == [
        {
            "component_key": "regular",
            "batch_id": str(regular.batch_id),
            "calculation_hash": regular.calculation_hash,
            "employee_line_ids": [str(regular_line.id)],
        }
    ]
    links = list(
        session.scalars(select(PayrollEventLink).where(PayrollEventLink.event_id == event_id))
    )
    assert {(link.component_id, link.payroll_batch_id) for link in links} == {
        (components["regular"].id, regular.batch_id),
        (components["bonus-1"].id, bonus.batch_id),
    }


def test_regular_and_combined_bonus_post_as_one_component_event(session, organization):
    evidence = payroll_evidence(session, organization, "combined-payroll-components")
    employee_id = _register_employee(session, organization)
    regular = _preview_regular(
        session,
        organization,
        evidence,
        employee_id,
        key="combined-regular-preview",
    )
    bonus = _preview_combined_bonus(
        session,
        organization,
        evidence,
        employee_id,
        regular.batch_id,
        key="combined-bonus-preview",
    )
    before_events = session.scalar(select(func.count()).select_from(BusinessEvent))
    standalone = FinanceService(session).confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=bonus.batch_id,
            calculation_hash=bonus.calculation_hash,
            idempotency_key="standalone-combined-bonus",
        )
    )
    assert standalone.status == "rejected", standalone
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == before_events
    assert session.scalar(select(func.count()).select_from(PayrollTaxStateSlot)) == 0

    request = _combined_event_request(organization, evidence, regular, bonus)
    preview = ComponentService(session).preview(request)
    assert preview.status == "calculated", preview
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == before_events
    assert session.scalar(select(func.count()).select_from(PayrollTaxStateSlot)) == 0
    posted = ComponentService(session).record(
        RecordEventRequest.model_validate(preview.data["reviewed_request"])
    )
    assert posted.status == "posted", posted
    _assert_combined_result(session, organization, regular, bonus, posted.event_id)


def test_stale_parent_and_competing_combined_bonus_roll_back_whole_event(session, organization):
    evidence = payroll_evidence(session, organization, "combined-payroll-rollback")
    employee_id = _register_employee(session, organization)
    first_regular = _preview_regular(
        session,
        organization,
        evidence,
        employee_id,
        key="stale-regular-preview",
    )
    stale_bonus = _preview_combined_bonus(
        session,
        organization,
        evidence,
        employee_id,
        first_regular.batch_id,
        key="stale-bonus-preview",
    )
    current_regular = _preview_regular(
        session,
        organization,
        evidence,
        employee_id,
        key="current-regular-preview",
    )
    stale_request = _combined_event_request(
        organization, evidence, current_regular, stale_bonus
    ).model_copy(update={"idempotency_key": "stale-combined-parent"})
    rejected = ComponentService(session).record(stale_request)
    assert rejected.status == "rejected", rejected
    assert "PAYROLL_LOCAL_REGULAR_PARENT_UNUSED" in rejected.errors
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0
    assert session.scalar(select(func.count()).select_from(PayrollTaxStateSlot)) == 0

    bonus_one = _preview_combined_bonus(
        session,
        organization,
        evidence,
        employee_id,
        current_regular.batch_id,
        key="competing-bonus-one",
    )
    bonus_two = _preview_combined_bonus(
        session,
        organization,
        evidence,
        employee_id,
        current_regular.batch_id,
        key="competing-bonus-two",
        payroll_period="2026-04",
    )
    competing_request = _combined_event_request(
        organization, evidence, current_regular, bonus_one, bonus_two
    ).model_copy(update={"idempotency_key": "competing-combined-bonuses"})
    rejected = ComponentService(session).record(competing_request)
    assert rejected.status == "rejected", rejected
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0
    assert session.scalar(select(func.count()).select_from(PayrollTaxStateSlot)) == 0
    assert session.get(PayrollBatch, current_regular.batch_id).status == "calculated"
    assert session.get(PayrollBatch, bonus_one.batch_id).status == "calculated"
    assert session.get(PayrollBatch, bonus_two.batch_id).status == "calculated"


def _exercise_two_local_regular_parents(session, organization, evidence, *, authority=None):
    first_employee_id = _register_employee(session, organization, authority)
    second_employee_id = _register_additional_employee(session, organization, authority)
    first_regular = _preview_regular(
        session,
        organization,
        evidence,
        first_employee_id,
        key="first-regular-parent",
        authority=authority,
    )
    second_regular = _preview_regular(
        session,
        organization,
        evidence,
        second_employee_id,
        key="second-regular-parent",
        authority=authority,
    )
    assert session.get(PayrollBatch, first_regular.batch_id).status == "calculated"
    with _call(authority, session, "finance_preview_payroll"):
        bonus = FinanceService(session).preview_payroll(
            PreviewPayrollRequest.model_validate(
                {
                    "org_id": organization.id,
                    "idempotency_key": "two-parent-bonus-preview",
                    "batch_kind": "annual_bonus",
                    "payroll_period": "2026-03",
                    "posting_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "tax_method": "combined",
                    "evidence_references": [evidence.id],
                    "employee_items": [
                        {
                            "employee_id": first_employee_id,
                            "annual_bonus_fen": 500_000,
                            "regular_payroll_batch_id": first_regular.batch_id,
                        },
                        {
                            "employee_id": second_employee_id,
                            "annual_bonus_fen": 600_000,
                            "regular_payroll_batch_id": second_regular.batch_id,
                        },
                    ],
                }
            )
        )
    assert bonus.status == "calculated", bonus
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "two-parent-combined-event",
            "posting_date": "2026-03-05",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "bonus",
                    "kind": "payroll_accrual",
                    "business_date": "2026-03-05",
                    "batch_id": bonus.batch_id,
                    "calculation_hash": bonus.calculation_hash,
                    "regular_payroll_component_keys": ["regular-a", "regular-b"],
                },
                {
                    "key": "regular-b",
                    "kind": "payroll_accrual",
                    "business_date": "2026-03-05",
                    "batch_id": second_regular.batch_id,
                    "calculation_hash": second_regular.calculation_hash,
                },
                {
                    "key": "regular-a",
                    "kind": "payroll_accrual",
                    "business_date": "2026-03-05",
                    "batch_id": first_regular.batch_id,
                    "calculation_hash": first_regular.calculation_hash,
                },
            ],
        }
    )
    with _call(authority, session, "finance_record_event"):
        posted = ComponentService(session).record(request)
    assert posted.status == "posted", posted
    slots = list(
        session.scalars(select(PayrollTaxStateSlot).order_by(PayrollTaxStateSlot.employee_id))
    )
    assert len(slots) == 2
    assert {slot.regular_batch_id for slot in slots} == {
        first_regular.batch_id,
        second_regular.batch_id,
    }
    assert {slot.final_batch_id for slot in slots} == {bonus.batch_id}
    bonus_component = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == posted.event_id,
            BusinessEventComponent.key == "bonus",
        )
    )
    proofs = bonus_component.derived["local_regular_payroll_proofs"]
    assert [proof["component_key"] for proof in proofs] == ["regular-a", "regular-b"]
    assert {proof["batch_id"] for proof in proofs} == {
        str(first_regular.batch_id),
        str(second_regular.batch_id),
    }


def test_combined_bonus_can_use_two_disjoint_local_regular_batches(session, organization):
    evidence = payroll_evidence(session, organization, "combined-payroll-two-parents")
    _exercise_two_local_regular_parents(session, organization, evidence)


def _exercise_posted_and_local_regular_parents(session, organization, evidence, *, authority=None):
    posted_employee_id = _register_employee(session, organization, authority)
    local_employee_id = _register_additional_employee(session, organization, authority)
    posted_regular = _preview_regular(
        session,
        organization,
        evidence,
        posted_employee_id,
        key="posted-regular-parent",
        authority=authority,
    )
    with _call(authority, session, "finance_confirm_payroll"):
        confirmed = FinanceService(session).confirm_payroll(
            ConfirmPayrollRequest(
                org_id=organization.id,
                batch_id=posted_regular.batch_id,
                calculation_hash=posted_regular.calculation_hash,
                idempotency_key="posted-regular-parent-confirmation",
            )
        )
    assert confirmed.status == "posted", confirmed
    local_regular = _preview_regular(
        session,
        organization,
        evidence,
        local_employee_id,
        key="local-regular-parent",
        authority=authority,
    )
    with _call(authority, session, "finance_preview_payroll"):
        bonus = FinanceService(session).preview_payroll(
            PreviewPayrollRequest.model_validate(
                {
                    "org_id": organization.id,
                    "idempotency_key": "posted-and-local-bonus-preview",
                    "batch_kind": "annual_bonus",
                    "payroll_period": "2026-03",
                    "posting_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "tax_method": "combined",
                    "evidence_references": [evidence.id],
                    "employee_items": [
                        {
                            "employee_id": posted_employee_id,
                            "annual_bonus_fen": 500_000,
                            "regular_payroll_batch_id": posted_regular.batch_id,
                        },
                        {
                            "employee_id": local_employee_id,
                            "annual_bonus_fen": 600_000,
                            "regular_payroll_batch_id": local_regular.batch_id,
                        },
                    ],
                }
            )
        )
    assert bonus.status == "calculated", bonus
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "posted-and-local-combined-event",
            "posting_date": "2026-03-05",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "bonus",
                    "kind": "payroll_accrual",
                    "business_date": "2026-03-05",
                    "batch_id": bonus.batch_id,
                    "calculation_hash": bonus.calculation_hash,
                    "regular_payroll_component_keys": ["local-regular"],
                },
                {
                    "key": "local-regular",
                    "kind": "payroll_accrual",
                    "business_date": "2026-03-05",
                    "batch_id": local_regular.batch_id,
                    "calculation_hash": local_regular.calculation_hash,
                },
            ],
        }
    )
    with _call(authority, session, "finance_record_event"):
        posted = ComponentService(session).record(request)
    assert posted.status == "posted", posted
    posted_batch = session.get(PayrollBatch, posted_regular.batch_id)
    local_batch = session.get(PayrollBatch, local_regular.batch_id)
    assert posted_batch.business_event_id == confirmed.event_id
    assert local_batch.business_event_id == posted.event_id
    slots = list(
        session.scalars(select(PayrollTaxStateSlot).order_by(PayrollTaxStateSlot.employee_id))
    )
    assert {slot.regular_batch_id for slot in slots} == {
        posted_regular.batch_id,
        local_regular.batch_id,
    }
    assert {slot.final_batch_id for slot in slots} == {bonus.batch_id}
    bonus_component = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == posted.event_id,
            BusinessEventComponent.key == "bonus",
        )
    )
    assert bonus_component.derived["local_regular_payroll_proofs"] == [
        {
            "component_key": "local-regular",
            "batch_id": str(local_regular.batch_id),
            "calculation_hash": local_regular.calculation_hash,
            "employee_line_ids": [
                str(
                    session.scalar(
                        select(PayrollLine.id).where(
                            PayrollLine.payroll_batch_id == local_regular.batch_id
                        )
                    )
                )
            ],
        }
    ]


def test_combined_bonus_can_mix_posted_and_local_regular_parents(session, organization):
    evidence = payroll_evidence(session, organization, "combined-payroll-mixed-parents")
    _exercise_posted_and_local_regular_parents(session, organization, evidence)


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_postgres_regular_and_combined_bonus_post_as_one_component_event():
    with authenticated_business_database(
        "combined_payroll_components", name="常规工资与并入计税年终奖"
    ) as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            evidence = session.get(Evidence, evidence_id)
            employee_id = _register_employee(session, organization, authority)
            regular = _preview_regular(
                session,
                organization,
                evidence,
                employee_id,
                key="pg-combined-regular-preview",
                authority=authority,
            )
            bonus = _preview_combined_bonus(
                session,
                organization,
                evidence,
                employee_id,
                regular.batch_id,
                key="pg-combined-bonus-preview",
                authority=authority,
            )
            session.commit()

        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            evidence = session.get(Evidence, evidence_id)
            request = _combined_event_request(organization, evidence, regular, bonus)
            with authority.attributed_call(session, tool_name="finance_preview_event"):
                preview = ComponentService(session).preview(request)
            assert preview.status == "calculated", preview
            assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0
            assert session.scalar(select(func.count()).select_from(PayrollTaxStateSlot)) == 0
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = ComponentService(session).record(
                    RecordEventRequest.model_validate(preview.data["reviewed_request"])
                )
            assert posted.status == "posted", posted
            session.commit()
            _assert_combined_result(session, organization, regular, bonus, posted.event_id)


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_postgres_combined_bonus_uses_two_local_regular_parents():
    with authenticated_business_database(
        "combined_payroll_two_parents", name="两个常规工资批次与同一并入计税年终奖"
    ) as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            evidence = session.get(Evidence, evidence_id)
            _exercise_two_local_regular_parents(
                session, organization, evidence, authority=authority
            )
            session.commit()


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_postgres_combined_bonus_mixes_posted_and_local_regular_parents():
    with authenticated_business_database(
        "combined_payroll_mixed_parents", name="已确认与同事件常规工资共同支撑年终奖"
    ) as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            evidence = session.get(Evidence, evidence_id)
            _exercise_posted_and_local_regular_parents(
                session, organization, evidence, authority=authority
            )
            session.commit()
