from dataclasses import replace
from datetime import date

import pytest
from _correction_helpers import delete_open_event
from _postgres_helpers import authenticated_business_database
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from test_labor_remuneration_service import _evidence, _register_person
from test_payroll_service import payroll_evidence, register_payroll_facts

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import AmendEventRequest, DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.labor_remuneration_schemas import (
    LaborRemunerationItemFacts,
    PreviewLaborRemunerationBatchRequest,
)
from ai_accounting.labor_remuneration_service import LaborRemunerationService
from ai_accounting.ledger import build_business_event, commit_posting_plan
from ai_accounting.models import (
    BusinessEvent,
    Evidence,
    LaborRemunerationBatch,
    LaborRemunerationBatchEvidence,
    LaborRemunerationLine,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollLine,
    PayrollTaxStateSlot,
    Voucher,
)
from ai_accounting.schemas import (
    PreviewPayrollRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService


def _calculated_batches(session, organization, evidence=None):
    employee_id = register_payroll_facts(session, organization)
    payroll_proof = evidence or payroll_evidence(session, organization, "component-accrual-payroll")
    payroll = FinanceService(session).preview_payroll(
        PreviewPayrollRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "component-payroll-preview",
                "batch_kind": "regular",
                "payroll_period": "2026-03",
                "posting_date": "2026-03-05",
                "evidence_references": [payroll_proof.id],
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
    assert payroll.status == "calculated", payroll
    labor_proof = evidence or _evidence(session, organization, "c")
    person_id = _register_person(
        session, organization, labor_proof, "component-labor-person", "组件劳务人员"
    )
    labor = LaborRemunerationService(session).preview_batch(
        PreviewLaborRemunerationBatchRequest(
            org_id=organization.id,
            idempotency_key="component-labor-preview",
            remuneration_period="2026-03",
            business_date=date(2026, 3, 5),
            posting_date=date(2026, 3, 5),
            planned_payment_date=date(2026, 3, 5),
            items=[
                LaborRemunerationItemFacts(
                    labor_person_id=person_id,
                    service_start_date=date(2026, 3, 1),
                    service_end_date=date(2026, 3, 5),
                    fixed_fee_fen=300_000,
                    commission_fen=200_000,
                    expense_role="labor_sales_expense",
                    tax_identity="resident",
                    income_grouping="single_occurrence",
                    is_full_time_student=False,
                    external_declaration_status="not_due",
                )
            ],
            evidence_references=[labor_proof.id],
        )
    )
    assert labor.status == "calculated", labor
    return payroll, payroll_proof, labor, labor_proof


def _accruals(payroll, payroll_proof, labor, labor_proof):
    return [
        {
            "key": "payroll",
            "kind": "payroll_accrual",
            "business_date": "2026-03-05",
            "batch_id": payroll.batch_id,
            "calculation_hash": payroll.calculation_hash,
            "evidence_references": [payroll_proof.id],
            "metadata": {"confirmation_note": "组合确认工资"},
        },
        {
            "key": "labor-accrual",
            "kind": "labor_remuneration_accrual",
            "business_date": "2026-03-05",
            "batch_id": labor.batch_id,
            "calculation_hash": labor.calculation_hash,
            "evidence_references": [labor_proof.id],
            "metadata": {"confirmation_note": "组合确认劳务"},
        },
    ]


def test_payroll_labor_and_expense_open_event_deletes_as_one_event(session, organization):
    payroll, payroll_proof, labor, labor_proof = _calculated_batches(session, organization)
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "mixed-accrual-confirm",
            "posting_date": "2026-03-05",
            "components": [
                *_accruals(payroll, payroll_proof, labor, labor_proof),
                {
                    "key": "expense",
                    "kind": "expense",
                    "business_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "amount_fen": 100,
                    "expense_class": "general_expense",
                    "payment_basis": "immediate",
                    "evidence_references": [payroll_proof.id],
                },
            ],
            "funds": [
                {
                    "key": "expense-cash",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": "2026-03-05",
                    "amount_fen": 100,
                    "allocations": [{"component_key": "expense", "amount_fen": 100}],
                }
            ],
        }
    )
    result = ComponentService(session).record(request)
    replay = ComponentService(session).record(request)
    assert result.status == "posted", result
    assert replay.event_id == result.event_id
    assert replay.data["idempotent_replay"] is True

    reversed_result = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=result.event_id,
            idempotency_key="mixed-accrual-reverse",
            posting_date=date(2026, 3, 6),
            reason="组合计提整体冲正",
        )
    )
    assert reversed_result.errors == ["OPEN_PERIOD_REQUIRES_AMENDMENT"]
    delete_open_event(session, organization.id, result.event_id, "delete-mixed")
    assert session.get(PayrollBatch, payroll.batch_id) is None
    assert session.get(LaborRemunerationBatch, labor.batch_id) is None


def test_same_event_accruals_are_explicit_local_settlement_sources(session, organization):
    payroll, payroll_proof, labor, labor_proof = _calculated_batches(session, organization)
    payroll_line = session.scalar(
        select(PayrollLine).where(PayrollLine.payroll_batch_id == payroll.batch_id)
    )
    labor_line = session.scalar(
        select(LaborRemunerationLine).where(LaborRemunerationLine.batch_id == labor.batch_id)
    )
    salary_key = f"salary:{payroll_line.id}"
    source = {
        "source_component_key": "payroll",
        "source_open_item_key": salary_key,
    }
    components = [
        *_accruals(payroll, payroll_proof, labor, labor_proof),
        {
            "key": "salary",
            "kind": "salary_settlement",
            "business_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "amount_fen": 839_500,
            "allocations": [{**source, "amount_fen": 1_000_000}],
            "withholding_allocations": [
                {
                    **source,
                    "employee_social_insurance_items": {"pension": 80_000},
                    "employee_housing_fund_items": {"housing_fund": 70_000},
                    "individual_income_tax_fen": 10_500,
                }
            ],
        },
        {
            "key": "labor",
            "kind": "labor_settlement",
            "business_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "source_component_key": "labor-accrual",
            "source_open_item_key": str(labor_line.id),
            "amount_fen": 500000,
            "settlement_mode": "net_after_withholding",
            "metadata": {
                "withholding_agency_code": "TAX-LABOR-01",
                "withholding_agency_name": "测试税务局",
            },
        },
    ]
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "same-event-accrual-settlement",
            "posting_date": "2026-03-05",
            "components": components,
            "funds": [
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": "2026-03-05",
                    "amount_fen": 1_259_500,
                    "allocations": [
                        {"component_key": "salary", "amount_fen": 839_500},
                        {"component_key": "labor", "amount_fen": 420_000},
                    ],
                }
            ],
        }
    )
    result = ComponentService(session).record(request)
    assert result.status == "posted", result
    amended = EventAmendmentService(session).amend(
        AmendEventRequest(
            org_id=organization.id,
            event_id=result.event_id,
            idempotency_key="same-event-accrual-settlement-amend",
            expected_facts_hash=result.data["facts_hash"],
            reason="复核组合计提与结算",
            replacement=request.model_copy(update={"idempotency_key": "same-event-replacement"}),
        )
    )
    assert amended["status"] == "posted", amended
    assert session.get(PayrollLine, payroll_line.id) is not None
    assert session.get(LaborRemunerationLine, labor_line.id) is not None


def test_mixed_accrual_event_amends_and_deletes_with_stable_preview_state(session, organization):
    payroll, payroll_proof, labor, labor_proof = _calculated_batches(session, organization)

    def event_request(amount, key):
        return RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": key,
                "posting_date": "2026-03-05",
                "components": [
                    *_accruals(payroll, payroll_proof, labor, labor_proof),
                    {
                        "key": "expense",
                        "kind": "expense",
                        "business_date": "2026-03-05",
                        "payment_date": "2026-03-05",
                        "amount_fen": amount,
                        "expense_class": "general_expense",
                        "payment_basis": "immediate",
                        "evidence_references": [payroll_proof.id],
                    },
                ],
                "funds": [
                    {
                        "key": "expense-cash",
                        "account_code": "1001",
                        "direction": "payment",
                        "payment_date": "2026-03-05",
                        "amount_fen": amount,
                        "allocations": [{"component_key": "expense", "amount_fen": amount}],
                    }
                ],
            }
        )

    original = ComponentService(session).record(event_request(100, "mixed-amend-source"))
    slot_ids = set(session.scalars(select(PayrollTaxStateSlot.id)))
    amended = EventAmendmentService(session).amend(
        AmendEventRequest(
            org_id=organization.id,
            event_id=original.event_id,
            idempotency_key="mixed-accrual-amend",
            expected_facts_hash=original.data["facts_hash"],
            reason="修正同事件费用",
            replacement=event_request(120, "replacement-key-ignored"),
        )
    )
    assert amended["status"] == "posted", amended
    assert set(session.scalars(select(PayrollTaxStateSlot.id))) == slot_ids
    assert session.get(PayrollBatch, payroll.batch_id).status == "posted"
    assert session.get(LaborRemunerationBatch, labor.batch_id).status == "posted"

    deleted = EventAmendmentService(session).amend(
        DeleteEventRequest(
            org_id=organization.id,
            event_id=original.event_id,
            idempotency_key="mixed-accrual-delete",
            expected_facts_hash=amended["facts_hash"],
            reason="整笔重复",
        )
    )
    assert deleted["status"] == "deleted", deleted
    assert session.scalar(select(func.count()).select_from(PayrollBatch)) == 0
    assert session.scalar(select(func.count()).select_from(LaborRemunerationBatch)) == 0


def test_stale_hash_and_missing_batch_proof_leave_no_formal_graph(session, organization):
    payroll, payroll_proof, labor, labor_proof = _calculated_batches(session, organization)
    baseline = tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (BusinessEvent, Voucher, OpenItem)
    )
    stale_components = _accruals(payroll, payroll_proof, labor, labor_proof)
    stale_components[0]["calculation_hash"] = "0" * 64
    stale = ComponentService(session).record(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "stale-mixed-accrual",
                "posting_date": "2026-03-05",
                "components": stale_components,
            }
        )
    )
    assert stale.status == "rejected"
    assert stale.errors == ["STALE_PAYROLL_CALCULATION"]
    assert baseline == tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (BusinessEvent, Voucher, OpenItem)
    )

    proof_link = session.scalar(
        select(LaborRemunerationBatchEvidence).where(
            LaborRemunerationBatchEvidence.batch_id == labor.batch_id
        )
    )
    session.delete(proof_link)
    session.flush()
    missing_proof = ComponentService(session).record(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "missing-proof-mixed-accrual",
                "posting_date": "2026-03-05",
                "components": _accruals(payroll, payroll_proof, labor, labor_proof),
            }
        )
    )
    assert missing_proof.status == "rejected"
    assert missing_proof.errors == ["LABOR_CALCULATION_SNAPSHOT_TAMPERED"]
    assert baseline == tuple(
        session.scalar(select(func.count()).select_from(model))
        for model in (BusinessEvent, Voucher, OpenItem)
    )


@pytest.mark.parametrize("component_index", [0, 1])
def test_amendment_cannot_take_accrual_batch_from_another_event(
    session, organization, component_index
):
    payroll, payroll_proof, labor, labor_proof = _calculated_batches(session, organization)
    accruals = _accruals(payroll, payroll_proof, labor, labor_proof)
    original_owner = ComponentService(session).record(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "owned-accrual-source",
                "posting_date": "2026-03-05",
                "components": accruals,
            }
        )
    )
    assert original_owner.status == "posted", original_owner
    expense = ComponentService(session).record(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "unrelated-expense",
                "posting_date": "2026-03-05",
                "components": [
                    {
                        "key": "expense",
                        "kind": "expense",
                        "business_date": "2026-03-05",
                        "amount_fen": 100,
                        "expense_class": "general_expense",
                        "payment_basis": "supplier_credit",
                        "evidence_references": [payroll_proof.id],
                        "metadata": {"counterparty": {"kind": "supplier", "name": "独立供应商"}},
                    }
                ],
            }
        )
    )
    assert expense.status == "posted", expense
    rejected = EventAmendmentService(session).amend(
        AmendEventRequest(
            org_id=organization.id,
            event_id=expense.event_id,
            idempotency_key="reject-foreign-accrual-amendment",
            expected_facts_hash=expense.data["facts_hash"],
            reason="验证批次只属于原业务",
            replacement=RecordEventRequest.model_validate(
                {
                    "org_id": organization.id,
                    "idempotency_key": "foreign-accrual-replacement",
                    "posting_date": "2026-03-05",
                    "components": [accruals[component_index]],
                }
            ),
        )
    )
    assert rejected["status"] == "rejected", rejected
    for model, batch_id in (
        (PayrollBatch, payroll.batch_id),
        (LaborRemunerationBatch, labor.batch_id),
    ):
        batch = session.get(model, batch_id)
        assert batch.status == "posted"
        assert batch.business_event_id == original_owner.event_id
    assert session.get(BusinessEvent, expense.event_id).status == "posted"
    assert session.get(Voucher, expense.voucher_id) is not None


def test_repeated_labor_accrual_components_delete_every_owned_batch(session, organization):
    _, _, first, proof = _calculated_batches(session, organization)
    second_person = _register_person(
        session, organization, proof, "component-labor-person-two", "第二位组件劳务人员"
    )
    second = LaborRemunerationService(session).preview_batch(
        PreviewLaborRemunerationBatchRequest(
            org_id=organization.id,
            idempotency_key="component-labor-preview-two",
            remuneration_period="2026-03",
            business_date=date(2026, 3, 5),
            posting_date=date(2026, 3, 5),
            planned_payment_date=date(2026, 3, 5),
            items=[
                LaborRemunerationItemFacts(
                    labor_person_id=second_person,
                    service_start_date=date(2026, 3, 1),
                    service_end_date=date(2026, 3, 5),
                    fixed_fee_fen=100_000,
                    commission_fen=0,
                    expense_role="labor_management_expense",
                    tax_identity="resident",
                    income_grouping="single_occurrence",
                    is_full_time_student=False,
                    external_declaration_status="not_due",
                )
            ],
            evidence_references=[proof.id],
        )
    )
    components = [
        {
            "key": key,
            "kind": "labor_remuneration_accrual",
            "business_date": "2026-03-05",
            "batch_id": item.batch_id,
            "calculation_hash": item.calculation_hash,
            "evidence_references": [proof.id],
            "metadata": {"confirmation_note": "确认重复劳务批次"},
        }
        for key, item in (("labor-one", first), ("labor-two", second))
    ]
    posted = ComponentService(session).record(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "repeat-labor-accruals",
                "posting_date": "2026-03-05",
                "components": components,
            }
        )
    )
    assert posted.status == "posted", posted
    reversed_result = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=posted.event_id,
            idempotency_key="repeat-labor-accruals-reverse",
            posting_date=date(2026, 3, 6),
            reason="整体撤销重复劳务计提",
        )
    )
    assert reversed_result.errors == ["OPEN_PERIOD_REQUIRES_AMENDMENT"]
    delete_open_event(session, organization.id, posted.event_id, "delete-repeated-labor")
    assert session.get(LaborRemunerationBatch, first.batch_id) is None
    assert session.get(LaborRemunerationBatch, second.batch_id) is None


@pytest.mark.postgres
def test_mixed_accrual_components_commit_in_real_postgres():
    with authenticated_business_database("confirmed_accrual_components") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            evidence = session.get(Evidence, evidence_id)
            with authority.attributed_call(session, tool_name="finance_record_event"):
                payroll, payroll_proof, labor, labor_proof = _calculated_batches(
                    session, organization, evidence
                )
                payroll_line = session.scalar(
                    select(PayrollLine).where(PayrollLine.payroll_batch_id == payroll.batch_id)
                )
                labor_line = session.scalar(
                    select(LaborRemunerationLine).where(
                        LaborRemunerationLine.batch_id == labor.batch_id
                    )
                )
                salary_source = {
                    "source_component_key": "payroll",
                    "source_open_item_key": f"salary:{payroll_line.id}",
                }
                result = ComponentService(session).record(
                    RecordEventRequest.model_validate(
                        {
                            "org_id": org_id,
                            "idempotency_key": "postgres-mixed-accrual-confirm",
                            "posting_date": "2026-03-05",
                            "components": [
                                *_accruals(payroll, payroll_proof, labor, labor_proof),
                                {
                                    "key": "salary",
                                    "kind": "salary_settlement",
                                    "business_date": "2026-03-05",
                                    "payment_date": "2026-03-05",
                                    "amount_fen": 839500,
                                    "allocations": [{**salary_source, "amount_fen": 1000000}],
                                    "withholding_allocations": [
                                        {
                                            **salary_source,
                                            "employee_social_insurance_items": {"pension": 80000},
                                            "employee_housing_fund_items": {"housing_fund": 70000},
                                            "individual_income_tax_fen": 10500,
                                        }
                                    ],
                                },
                                {
                                    "key": "labor",
                                    "kind": "labor_settlement",
                                    "business_date": "2026-03-05",
                                    "payment_date": "2026-03-05",
                                    "source_component_key": "labor-accrual",
                                    "source_open_item_key": str(labor_line.id),
                                    "amount_fen": 500000,
                                    "settlement_mode": "net_after_withholding",
                                    "metadata": {
                                        "withholding_agency_code": "TAX-LABOR-01",
                                        "withholding_agency_name": "测试税务局",
                                    },
                                },
                            ],
                            "funds": [
                                {
                                    "key": "cash",
                                    "account_code": "1001",
                                    "direction": "payment",
                                    "payment_date": "2026-03-05",
                                    "amount_fen": 1259500,
                                    "allocations": [
                                        {"component_key": "salary", "amount_fen": 839500},
                                        {"component_key": "labor", "amount_fen": 420000},
                                    ],
                                }
                            ],
                        }
                    )
                )
                assert result.status == "posted", result
                session.commit()
            assert session.get(PayrollBatch, payroll.batch_id).status == "posted"
            assert session.get(LaborRemunerationBatch, labor.batch_id).status == "posted"
            with authority.attributed_call(session, tool_name="finance_reverse_event"):
                reversed_result = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=result.event_id,
                        idempotency_key="postgres-mixed-accrual-and-payment-reverse",
                        posting_date=date(2026, 3, 6),
                        reason="整笔撤销工资劳务计提及同笔发放",
                    )
                )
                assert reversed_result.errors == ["OPEN_PERIOD_REQUIRES_AMENDMENT"]
            with authority.attributed_call(session, tool_name="finance_delete_event"):
                delete_open_event(session, org_id, result.event_id, "delete-mixed-accrual")
                session.commit()
            assert session.get(PayrollBatch, payroll.batch_id) is None
            assert session.get(LaborRemunerationBatch, labor.batch_id) is None
            assert (
                session.scalar(select(OpenItem).where(OpenItem.source_event_id == result.event_id))
                is None
            )


@pytest.mark.postgres
def test_postgres_rejects_balanced_payroll_component_and_open_item_forgeries():
    with authenticated_business_database("payroll_component_forgery") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            organization = session.get(Organization, org_id)
            evidence = session.get(Evidence, evidence_id)
            with authority.attributed_call(session, tool_name="finance_preview_payroll"):
                payroll, proof, _, _ = _calculated_batches(session, organization, evidence)
                session.commit()

            def forged_plan(key, mutate):
                request = RecordEventRequest.model_validate(
                    {
                        "org_id": org_id,
                        "idempotency_key": key,
                        "posting_date": "2026-03-05",
                        "components": [
                            {
                                "key": "payroll",
                                "kind": "payroll_accrual",
                                "business_date": "2026-03-05",
                                "batch_id": payroll.batch_id,
                                "calculation_hash": payroll.calculation_hash,
                                "evidence_references": [proof.id],
                            }
                        ],
                    }
                )
                event = build_business_event(
                    session,
                    org_id=org_id,
                    idempotency_key=key,
                    request_payload_hash="f" * 64,
                    event_type="composite",
                    status="draft",
                    description="forged payroll component",
                    facts=request.model_dump(mode="json"),
                    business_date=date(2026, 3, 5),
                    posting_date=date(2026, 3, 5),
                    rule_trace=[],
                )
                session.add(event)
                session.flush()
                compiler = ComponentService(session)
                compiler.request = request
                compiler.event = event
                compiler.plans = {}
                compiler.evidence_ids = {proof.id}
                plan = compiler.compile_payroll_accrual(request.components[0])
                commit_posting_plan(
                    session,
                    event=event,
                    components=[mutate(plan)],
                    posting_date=date(2026, 3, 5),
                    description=event.description,
                )

            def wrong_balanced_entries(plan):
                entries = list(plan.entries)
                debit = next(i for i, entry in enumerate(entries) if entry.debit_fen)
                salary = next(
                    i
                    for i, entry in enumerate(entries)
                    if entry.account_role == "employee_salary_payable"
                )
                entries[debit] = replace(entries[debit], debit_fen=entries[debit].debit_fen + 100)
                entries[salary] = replace(
                    entries[salary], credit_fen=entries[salary].credit_fen + 100
                )
                return replace(plan, entries=entries)

            with authority.attributed_call(session, tool_name="finance_forged_direct_write"):
                forged_plan("forged-payroll-balanced", wrong_balanced_entries)
                with pytest.raises(DBAPIError, match="PAYROLL_ACCRUAL_VOUCHER_AMOUNT_MISMATCH"):
                    session.commit()
                session.rollback()

            def wrong_salary_item(plan):
                items = [
                    replace(item, original_amount_fen=item.original_amount_fen + 100)
                    if item.payable_category == "salary"
                    else item
                    for item in plan.open_items
                ]
                return replace(plan, open_items=items)

            with authority.attributed_call(session, tool_name="finance_forged_direct_write"):
                forged_plan("forged-payroll-open-item", wrong_salary_item)
                with pytest.raises(DBAPIError, match="PAYROLL_ACCRUAL_OPEN_ITEM"):
                    session.commit()
                session.rollback()
