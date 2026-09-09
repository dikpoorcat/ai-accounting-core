from contextlib import nullcontext

from sqlalchemy import select
from test_business_components import sample_evidence as _sample_evidence_fixture

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import AmendEventRequest, DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import BusinessEventComponent, BusinessEventDependency, OpenItem
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService

sample_evidence = _sample_evidence_fixture


def exercise_refund_receipt_dependencies(session, organization, evidence, authority=None):
    def attributed(tool):
        return authority.attributed_call(session, tool_name=tool) if authority else nullcontext()

    day = "2026-03-05"
    party = {"kind": "customer", "name": "Receipt lineage customer"}
    dates = {"business_date": day, "payment_date": day}
    tax = {
        "taxable": False,
        "invoice_type": "none",
        "waive_exemption": False,
        "tax_due_on_event": False,
    }

    def payload(key, component, direction=None, amount=None):
        return RecordEventRequest(
            org_id=organization.id,
            idempotency_key=key,
            posting_date=day,
            evidence_references=[evidence.id],
            components=[component],
            funds=[
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": direction,
                    "payment_date": day,
                    "amount_fen": amount,
                    "allocations": [{"component_key": component["key"], "amount_fen": amount}],
                }
            ]
            if direction
            else [],
        )

    def post(request):
        with attributed("finance_record_event"):
            result = ComponentService(session).record(request)
            assert result.status == "posted", result
        return result

    sale = post(
        payload(
            "credit-sale",
            {
                "key": "sale",
                "kind": "service_sale",
                "amount_fen": 100,
                "recognition_basis": "credit",
                "fulfillment_date": day,
                "tax_facts": tax,
                **dates,
                "metadata": {"counterparty": party},
            },
        )
    )
    source = session.scalar(
        select(BusinessEventComponent).where(BusinessEventComponent.event_id == sale.event_id)
    )
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == sale.event_id))
    receipts = []
    for index in [1, 2]:
        request = payload(
            f"receipt-{index}",
            {
                "key": "receipt",
                "kind": "receivable_settlement",
                "allocations": [{"open_item_id": item.id, "amount_fen": 50}],
                **dates,
                "metadata": {"counterparty": party},
            },
            "receipt",
            50,
        )
        receipts.append((post(request), request))
    refund = post(
        payload(
            "cash-refund",
            {
                "key": "refund",
                "kind": "customer_refund",
                "refund_kind": "sale_return",
                "amount_fen": 75,
                "source": {"component_id": source.id},
                "tax_facts": tax,
                **dates,
                "metadata": {"counterparty": party},
            },
            "payment",
            75,
        )
    )
    allocations = refund.data["components"][0]["derived"]["receipt_allocations"]
    assert sorted(a["amount_fen"] for a in allocations) == [25, 50]
    edges = list(
        session.scalars(
            select(BusinessEventDependency).where(
                BusinessEventDependency.child_event_id == refund.event_id,
                BusinessEventDependency.parent_event_id.in_([r.event_id for r, _ in receipts]),
            )
        )
    )
    assert sorted(edge.amount_fen for edge in edges) == [25, 50]
    receipt, original_request = receipts[0]
    reverse_request = ReverseEventRequest(
        org_id=organization.id,
        event_id=receipt.event_id,
        posting_date="2026-03-06",
        idempotency_key="reverse-receipt",
        reason="Receipt correction",
    )
    with attributed("finance_reverse_event"):
        assert FinanceService(session).reverse_event(reverse_request).status == "rejected"
    for action in ["amend", "delete"]:
        common = dict(
            org_id=organization.id,
            event_id=receipt.event_id,
            idempotency_key=f"{action}-receipt",
            reason="Receipt correction",
            expected_facts_hash=receipt.data["facts_hash"],
        )
        request = (
            AmendEventRequest(**common, replacement=original_request)
            if action == "amend"
            else DeleteEventRequest(**common)
        )
        with attributed(f"finance_{action}_event"):
            result = EventAmendmentService(session).amend(request)
            assert result["status"] == "rejected", result
    assert session.get(OpenItem, item.id).settled_amount_fen == 100
    from _correction_helpers import delete_open_event

    with attributed("finance_delete_event"):
        delete_open_event(session, organization.id, refund.event_id, "delete-refund")
    with attributed("finance_delete_event"):
        delete_open_event(session, organization.id, receipt.event_id, "delete-receipt")
    assert session.get(OpenItem, item.id).settled_amount_fen == 50


def test_refund_allocates_receipt_dependencies_for_whole_lifecycle(
    session, organization, sample_evidence
):
    exercise_refund_receipt_dependencies(session, organization, sample_evidence)
