from sqlalchemy import func, select
from test_business_components import expense, request
from test_business_components import sample_evidence as _sample_evidence_fixture

from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import AmendEventRequest, DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    OpenItem,
    Settlement,
    Voucher,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService

sample_evidence = _sample_evidence_fixture


def test_whole_component_amend_keeps_voucher_and_identity(session, organization, sample_evidence):
    service = ComponentService(session)
    original = request(organization, sample_evidence, [expense("travel", 100), expense("fee", 20)])
    result = service.record(original)
    assert result.status == "posted", result
    ids = {c.key: c.id for c in session.scalars(select(BusinessEventComponent))}
    replacement = request(
        organization, sample_evidence, [expense("fee", 30), expense("travel", 200)]
    )
    amendment = AmendEventRequest(
        org_id=organization.id,
        event_id=result.event_id,
        idempotency_key="amend-components",
        expected_facts_hash=result.data["facts_hash"],
        reason="核对原始票据",
        replacement=replacement,
    )
    amended = EventAmendmentService(session).amend(amendment)
    assert amended["status"] == "posted", amended
    voucher = session.scalar(select(Voucher))
    assert voucher.voucher_number == result.voucher_number
    assert voucher.id == result.voucher_id
    assert {c.key: c.id for c in session.scalars(select(BusinessEventComponent))} == ids
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 1
    different_metadata = replacement.model_copy(deep=True)
    different_metadata.components[0].metadata.purpose = "management only"
    retry = EventAmendmentService(session).amend(
        amendment.model_copy(update={"replacement": different_metadata})
    )
    assert retry["status"] == "posted" and retry["idempotent_replay"], retry


def test_reverse_entire_event_including_local_settlement(session, organization, sample_evidence):
    party = {"kind": "supplier", "name": "Supplier"}
    components = [
        expense("purchase", 100, payment_basis="supplier_credit", metadata={"counterparty": party}),
        {
            "key": "settle",
            "kind": "payable_settlement",
            "business_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "allocations": [{"source_component_key": "purchase", "amount_fen": 100}],
            "metadata": {"counterparty": party},
        },
    ]
    result = ComponentService(session).record(
        request(organization, sample_evidence, components, amounts=[("settle", 100)])
    )
    assert result.status == "posted", result
    reversed_result = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=result.event_id,
            posting_date="2026-03-06",
            idempotency_key="reverse-components",
        )
    )
    assert reversed_result.status == "posted", reversed_result
    reversal = session.get(BusinessEvent, reversed_result.event_id)
    assert "reason" not in reversal.facts
    assert (
        FinanceService(session)
        .reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=result.event_id,
                posting_date="2026-03-06",
                idempotency_key="reverse-components",
                reason="optional audit explanation",
            )
        )
        .event_id
        == reversed_result.event_id
    )
    assert session.scalar(select(OpenItem)).status == "reversed"
    assert session.scalar(select(OpenItem)).settled_amount_fen == 0
    assert session.scalar(select(Settlement)).reversed is True
    assert session.scalar(select(func.count()).select_from(BusinessEventComponent)) == 6
    from datetime import date
    from types import SimpleNamespace

    from ai_accounting.dashboard_brief import _load_vouchers

    views = _load_vouchers(
        session,
        org_id=organization.id,
        period=SimpleNamespace(start_date=date(2026, 3, 1), end_date=date(2026, 3, 31)),
        counterparties={},
    )
    original, view = next(
        (voucher, view) for voucher, view in views if voucher.id == result.voucher_id
    )
    assert (
        original.status == "posted"
    )  # The original voucher, including a closed one, stays immutable.
    assert view["status"] == "reversed" and view["state"] == "已在后续期间冲正"


def test_delete_whole_component_event(session, organization, sample_evidence):
    result = ComponentService(session).record(
        request(organization, sample_evidence, [expense("purchase", 100), expense("fee", 20)])
    )
    deleted = EventAmendmentService(session).amend(
        DeleteEventRequest(
            org_id=organization.id,
            event_id=result.event_id,
            idempotency_key="delete-components",
            expected_facts_hash=result.data["facts_hash"],
        )
    )
    assert deleted["status"] == "deleted", deleted
    assert session.scalar(select(func.count()).select_from(Voucher)) == 0
    assert session.scalar(select(func.count()).select_from(BusinessEventComponent)) == 0
