from contextlib import nullcontext
from datetime import date

import pytest
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from conftest import import_test_bank_transaction, prepare_authenticated_bank_account
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_payroll_service import preview_and_confirm

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.event_amendment_schemas import AmendEventRequest, DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import (
    BankTransactionMatch,
    BusinessEvent,
    BusinessEventComponent,
    Counterparty,
    OpenItem,
    Organization,
    PayrollBatch,
    PayrollContributionSupplement,
    PayrollEventLink,
    PayrollLine,
    Voucher,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService


def _scenario(session, organization, batch, evidence, authority=None):
    def call(name):
        return authority.attributed_call(session, tool_name=name) if authority else nullcontext()

    employee_id = session.scalar(
        select(PayrollLine.employee_id).where(PayrollLine.payroll_batch_id == batch.id)
    )
    source = session.scalar(
        select(BusinessEventComponent)
        .join(PayrollEventLink, PayrollEventLink.component_id == BusinessEventComponent.id)
        .where(
            PayrollEventLink.payroll_batch_id == batch.id,
            PayrollEventLink.link_kind == "payroll_accrual",
        )
    )
    agency = session.get(
        Counterparty,
        session.scalar(
            select(OpenItem.counterparty_id).where(
                OpenItem.source_component_id == source.id,
                OpenItem.payable_category == "employer_social",
            )
        ),
    )
    day = date(2026, 4, 30)
    bank = None
    if authority:
        prepare_authenticated_bank_account(
            session, organization, authority=authority, evidence_id=evidence.id, booking_date=day
        )
        bank = import_test_bank_transaction(
            session, organization, amount_fen=-107, booking_date=day, key="mixed-supplement-payment"
        )

    supplements = [
        {
            "key": key,
            "kind": "payroll_contribution_supplement",
            "business_date": day,
            "employee_id": employee_id,
            "contribution_period": "2026-03",
            "due_date": day,
            "assessment_reference": key,
            "reason_code": "agency_assessment",
            "reason_description": "已取得补缴核定单",
            "source": {"component_id": source.id},
            "items": [
                {
                    "contribution_group": "social_insurance",
                    "insurance_kind": "pension",
                    "employee_amount_fen": 10,
                    "employer_amount_fen": employer,
                    "employee_amount_treatment": "employee_receivable",
                }
            ],
        }
        for key, employer in [("assessment-a", 30), ("assessment-b", 50)]
    ]
    payment = {
        "key": "pay",
        "kind": "payable_settlement",
        "business_date": day,
        "payment_date": day,
        "counterparty": {"id": agency.id},
        "allocations": [
            {
                "source_component_key": c["key"],
                "source_open_item_key": f"{category}.pension",
                "amount_fen": amount,
            }
            for c in supplements
            for category, amount in [
                ("employer_social", c["items"][0]["employer_amount_fen"]),
                ("withheld_employee_social", 10),
            ]
        ],
    }
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "mixed-supplements",
            "posting_date": day,
            "evidence_references": [evidence.id],
            "components": [
                *supplements,
                payment,
                {
                    "key": "fee",
                    "kind": "expense",
                    "business_date": day,
                    "payment_date": day,
                    "amount_fen": 7,
                    "expense_class": "finance_expense",
                    "payment_basis": "immediate",
                },
            ],
            "funds": [
                {
                    "key": "cash",
                    "account_code": "1002" if authority else "1001",
                    "direction": "payment",
                    "payment_date": day,
                    "amount_fen": 107,
                    "allocations": [
                        {"component_key": "pay", "amount_fen": 100},
                        {"component_key": "fee", "amount_fen": 7},
                    ],
                    "bank_transaction_references": [{"id": bank.id}] if bank else [],
                }
            ],
        }
    )
    before = session.scalar(select(func.count()).select_from(BusinessEvent))
    bad = request.model_dump(mode="json")
    bad["components"][1]["assessment_reference"] = "assessment-a"
    with call("finance_record_event"):
        failed = FinanceService(session).record_event(RecordEventRequest.model_validate(bad))
    assert failed.errors == ["CONTRIBUTION_SUPPLEMENT_ASSESSMENT_ALREADY_RECORDED"]
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == before
    assert session.scalar(select(func.count()).select_from(PayrollContributionSupplement)) == 0

    with call("finance_record_event"):
        result = FinanceService(session).record_event(request)
    assert result.status == "posted", result.model_dump(mode="json")
    if authority:
        session.commit()
    with call("finance_record_event"):
        retry = FinanceService(session).record_event(request)
    assert retry.event_id == result.event_id
    replacement = request.model_copy(update={"description": "核对两份补缴核定单后整体更新说明"})
    with call("finance_amend_event"):
        amended = EventAmendmentService(session).amend(
            AmendEventRequest(
                org_id=organization.id,
                event_id=result.event_id,
                idempotency_key="amend-mixed-supplements",
                expected_facts_hash=result.data["facts_hash"],
                reason="更新整笔业务的核对说明",
                replacement=replacement,
            )
        )
    assert amended["status"] == "posted", amended
    assert session.get(Voucher, result.voucher_id).voucher_number == result.voucher_number
    if authority:
        session.commit()
    rows = list(
        session.scalars(
            select(PayrollContributionSupplement).where(
                PayrollContributionSupplement.event_id == result.event_id
            )
        )
    )
    assert len(rows) == len({row.component_id for row in rows}) == 2
    items = list(
        session.scalars(select(OpenItem).where(OpenItem.source_event_id == result.event_id))
    )
    assert len(items) == 6
    assert all(item.status == "settled" for item in items if item.item_type == "payable")
    assert sum(item.original_amount_fen for item in items if item.item_type == "receivable") == 20
    assert (
        session.scalar(
            select(func.count()).select_from(Voucher).where(Voucher.event_id == result.event_id)
        )
        == 1
    )
    if bank:
        assert (
            session.scalar(
                select(func.count())
                .select_from(BankTransactionMatch)
                .where(BankTransactionMatch.bank_transaction_id == bank.id)
            )
            == 1
        )
    with call("finance_reverse_event"):
        reverse = FinanceService(session).reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=result.event_id,
                idempotency_key="reverse-mixed-supplements",
                posting_date=day,
                reason="整笔补缴业务撤销",
            )
        )
    assert reverse.status == "posted", reverse.model_dump(mode="json")
    if authority:
        session.commit()
    assert all(item.status == "reversed" and item.settled_amount_fen == 0 for item in items)

    removable = request.model_dump(mode="json")
    removable["idempotency_key"] = "removable-supplements"
    removable["components"] = removable["components"][:2]
    removable["funds"] = []
    for component in removable["components"]:
        component["assessment_reference"] += "-removable"
    with call("finance_record_event"):
        posted = FinanceService(session).record_event(RecordEventRequest.model_validate(removable))
    assert posted.status == "posted", posted.model_dump(mode="json")
    with call("finance_delete_event"):
        deleted = EventAmendmentService(session).amend(
            DeleteEventRequest(
                org_id=organization.id,
                event_id=posted.event_id,
                idempotency_key="delete-supplements",
                expected_facts_hash=posted.data["facts_hash"],
                reason="撤去没有下游依赖的两份误录核定单",
            )
        )
    assert deleted["status"] == "deleted", deleted
    if authority:
        session.commit()
    assert session.get(Voucher, posted.voucher_id) is None
    assert (
        session.scalar(
            select(func.count())
            .select_from(PayrollContributionSupplement)
            .where(PayrollContributionSupplement.event_id == posted.event_id)
        )
        == 0
    )


def test_repeated_supplements_local_payment_and_reversal(session, organization):
    _, posted = preview_and_confirm(session, organization)
    batch = session.get(PayrollBatch, posted.batch_id)
    evidence = session.get(BusinessEvent, posted.event_id).evidence[0]
    _scenario(session, organization, batch, evidence)


@pytest.mark.postgres
@pytest.mark.postgres_current
def test_repeated_supplements_local_payment_and_reversal_postgres():
    with authenticated_business_database("supplement_components") as (engine, org_id, proof, owner):
        with Session(engine) as session:
            prepare_authenticated_bank_account(
                session,
                session.get(Organization, org_id),
                authority=owner,
                evidence_id=proof,
                booking_date=date(2026, 3, 5),
            )
            org, batch, _, evidence, _ = confirmed_payroll(
                session, org_id, proof, owner, key="supplement-source"
            )
            session.commit()
            _scenario(session, org, batch, evidence, owner)
