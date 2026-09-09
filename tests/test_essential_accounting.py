from datetime import date

from sqlalchemy import select
from test_business_components import sample_evidence as _evidence_fixture

from ai_accounting.business_metadata import UpdateBusinessMetadataRequest, update_business_metadata
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService, payload_hash
from ai_accounting.models import BusinessEvent, OpenItem, VoucherLine

sample_evidence = _evidence_fixture


def post(session, organization, evidence, key, components, *, amount=0, direction="receipt"):
    return ComponentService(session).record(
        RecordEventRequest(
            org_id=organization.id,
            idempotency_key=key,
            posting_date=date(2026, 3, 5),
            evidence_references=[evidence.id],
            components=components,
            funds=[
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": direction,
                    "payment_date": "2026-03-05",
                    "amount_fen": amount,
                    "allocations": [{"component_key": components[-1]["key"], "amount_fen": amount}],
                }
            ]
            if amount
            else [],
        )
    )


def test_anonymous_pass_through_and_stable_partial_payment(session, organization, sample_evidence):
    received = post(
        session,
        organization,
        sample_evidence,
        "receipt",
        [{"key": "collection", "kind": "pass_through", "amount_fen": 1000}],
        amount=1000,
    )
    assert received.status == "posted", received
    item = session.scalar(select(OpenItem))
    assert item.counterparty_id is None and item.original_amount_fen == 1000
    paid = post(
        session,
        organization,
        sample_evidence,
        "payment",
        [
            {
                "key": "payment",
                "kind": "payable_settlement",
                "allocations": [
                    {
                        "source_event_key": "receipt",
                        "source_component_key": "collection",
                        "amount_fen": 400,
                    }
                ],
            }
        ],
        amount=400,
        direction="payment",
    )
    assert paid.status == "posted", paid
    assert item.settled_amount_fen == 400
    too_much = post(
        session,
        organization,
        sample_evidence,
        "overspend",
        [
            {
                "key": "payment",
                "kind": "payable_settlement",
                "allocations": [
                    {
                        "source_event_key": "receipt",
                        "source_component_key": "collection",
                        "amount_fen": 700,
                    }
                ],
            }
        ],
        amount=700,
        direction="payment",
    )
    assert too_much.status == "rejected", too_much
    assert item.settled_amount_fen == 400
    assert len(session.scalars(select(BusinessEvent)).all()) == 2


def test_management_updates_do_not_rewrite_accounting(session, organization, sample_evidence):
    from ai_accounting.business_metadata import metadata_projection
    from ai_accounting.models import Counterparty

    posted = post(
        session,
        organization,
        sample_evidence,
        "managed",
        [
            {
                "key": "collection",
                "kind": "pass_through",
                "amount_fen": 1000,
                "metadata": {"purpose": "initial"},
            }
        ],
        amount=1000,
    )
    assert posted.status == "posted", posted
    event = session.get(BusinessEvent, posted.event_id)
    before = payload_hash(event.facts)
    lines = [
        (line.id, line.debit_fen, line.credit_fen, line.counterparty_id)
        for line in session.scalars(select(VoucherLine))
    ]
    request = UpdateBusinessMetadataRequest(
        org_id=organization.id,
        source={"event_key": "managed", "component_key": "collection"},
        expected_version=1,
        idempotency_key="metadata-2",
        metadata={"counterparty": {"kind": "supplier", "name": "Named later"}},
    )
    changed = update_business_metadata(session, request)
    assert changed["status"] == "updated", changed
    assert changed["version"] == 2 and changed["metadata"]["purpose"] == "initial"
    assert update_business_metadata(session, request)["idempotent_replay"]
    assert update_business_metadata(
        session, request.model_copy(update={"idempotency_key": "stale"})
    )["errors"] == ["METADATA_VERSION_CONFLICT"]
    party = Counterparty(org_id=organization.id, kind="supplier", name="Registered supplier")
    session.add(party)
    session.flush()
    linked = update_business_metadata(
        session,
        request.model_copy(
            update={
                "expected_version": 2,
                "idempotency_key": "metadata-3",
                "metadata": request.metadata.model_validate({"counterparty": {"id": party.id}}),
            }
        ),
    )
    assert linked["status"] == "updated", linked
    management = metadata_projection(session, organization.id, event.id, "collection")
    assert management["display_names"]["counterparty"] == "Registered supplier"
    assert "name" not in management["metadata"]["counterparty"]
    assert management["history"][1]["metadata"]["counterparty"]["name"] == "Named later"
    assert payload_hash(event.facts) == before
    assert [
        (line.id, line.debit_fen, line.credit_fen, line.counterparty_id)
        for line in session.scalars(select(VoucherLine))
    ] == lines


def test_anonymous_supplier_advance_can_apply_without_project_labels(
    session, organization, sample_evidence
):
    advance = post(
        session,
        organization,
        sample_evidence,
        "advance",
        [
            {
                "key": "advance",
                "kind": "supplier_advance",
                "amount_fen": 1000,
                "purchase_purpose": "intangible_asset",
            }
        ],
        amount=1000,
        direction="payment",
    )
    assert advance.status == "posted", advance
    applied = post(
        session,
        organization,
        sample_evidence,
        "apply",
        [
            {
                "key": "cost",
                "kind": "project_cost",
                "business_date": "2026-03-05",
                "amount_fen": 1000,
                "project_nature": "purchased_intangible",
                "cost_element": "purchase_price",
                "rights_controlled": True,
            },
            {
                "key": "apply",
                "kind": "supplier_advance_application",
                "advances": [
                    {
                        "source_event_key": "advance",
                        "source_component_key": "advance",
                        "amount_fen": 1000,
                    }
                ],
                "allocations": [{"source_component_key": "cost", "amount_fen": 1000}],
            },
        ],
    )
    assert applied.status == "posted", applied
    assert all(i.settled_amount_fen == 1000 for i in session.scalars(select(OpenItem)))


def test_metadata_omission_and_explicit_clear_have_distinct_idempotency(
    session, organization, sample_evidence
):
    posted = post(
        session,
        organization,
        sample_evidence,
        "clearable",
        [
            {
                "key": "collection",
                "kind": "pass_through",
                "amount_fen": 1000,
                "metadata": {"purpose": "retained"},
            }
        ],
        amount=1000,
    )
    assert posted.status == "posted"
    request = UpdateBusinessMetadataRequest(
        org_id=organization.id,
        source={"event_key": "clearable", "component_key": "collection"},
        expected_version=1,
        idempotency_key="patch",
        metadata={},
    )
    assert update_business_metadata(session, request)["metadata"]["purpose"] == "retained"
    clear = request.model_copy(
        update={"metadata": request.metadata.model_validate({"purpose": None})}
    )
    assert update_business_metadata(session, clear)["errors"] == [
        "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"
    ]
    clear = clear.model_copy(update={"expected_version": 2, "idempotency_key": "clear"})
    assert update_business_metadata(session, clear)["metadata"] == {}


def test_only_management_changes_keep_preview_accounting_hash(
    session, organization, sample_evidence
):
    request = RecordEventRequest(
        org_id=organization.id,
        posting_date="2026-03-05",
        idempotency_key="metadata-preview",
        evidence_references=[sample_evidence.id],
        components=[
            {
                "key": "expense",
                "kind": "expense",
                "business_date": "2026-03-05",
                "amount_fen": 1000,
                "expense_class": "general_expense",
                "payment_basis": "supplier_credit",
            }
        ],
    )
    before = ComponentService(session).preview(request)
    assert before.status == "calculated", before
    modified = request.model_copy(
        update={
            "components": [
                request.components[0].model_copy(
                    update={
                        "metadata": request.components[0].metadata.model_validate(
                            {"description": "管理说明", "due_date": "2020-01-01"}
                        )
                    }
                )
            ]
        }
    )
    after = ComponentService(session).preview(modified)
    assert after.status == "calculated", after
    assert before.data["facts_hash"] == after.data["facts_hash"]
    assert not session.scalars(select(BusinessEvent)).all()


def test_accounting_idempotency_reuses_dates_and_equivalent_sources(
    session, organization, sample_evidence
):
    from ai_accounting.business_metadata import metadata_projection

    request = RecordEventRequest(
        org_id=organization.id,
        posting_date="2026-03-05",
        idempotency_key="stable-receipt",
        evidence_references=[sample_evidence.id],
        components=[{"key": "collection", "kind": "pass_through", "amount_fen": 1000}],
        funds=[
            {
                "key": "cash",
                "account_code": "1001",
                "direction": "receipt",
                "payment_date": "2026-03-05",
                "amount_fen": 1000,
                "allocations": [{"component_key": "collection", "amount_fen": 1000}],
            }
        ],
    )
    service = ComponentService(session)
    posted = service.record(request)
    assert posted.status == "posted", posted
    event = session.get(BusinessEvent, posted.event_id)
    accounting_hash = event.request_payload_hash
    changed = request.model_dump(mode="json")
    changed["description"] = "optional summary"
    changed["components"][0].update(
        business_date="2026-03-05",
        payment_date="2026-03-05",
        metadata={"purpose": "optional information"},
    )
    replayed = service.record(RecordEventRequest.model_validate(changed))
    assert replayed.status == "posted" and replayed.data["idempotent_replay"], replayed
    assert event.request_payload_hash == accounting_hash == payload_hash(event.facts)
    assert metadata_projection(session, organization.id, event.id, "collection")["version"] == 0

    item = session.scalar(select(OpenItem))
    payment = changed.copy()
    payment["idempotency_key"] = "stable-payment"
    payment["components"] = [
        {
            "key": "pay",
            "kind": "payable_settlement",
            "allocations": [
                {
                    "source_event_key": "stable-receipt",
                    "source_component_key": "collection",
                    "amount_fen": 1000,
                }
            ],
        }
    ]
    payment["funds"] = [
        {
            **changed["funds"][0],
            "direction": "payment",
            "allocations": [{"component_key": "pay", "amount_fen": 1000}],
        }
    ]
    paid = service.record(RecordEventRequest.model_validate(payment))
    assert paid.status == "posted", paid
    payment["components"][0]["allocations"] = [{"open_item_id": str(item.id), "amount_fen": 1000}]
    retry = service.record(RecordEventRequest.model_validate(payment))
    assert retry.status == "posted" and retry.data["idempotent_replay"], retry
    assert item.settled_amount_fen == 1000
    assert len(session.scalars(select(BusinessEvent)).all()) == 2


def test_component_cannot_infer_date_from_different_fund_dates(
    session, organization, sample_evidence
):
    request = RecordEventRequest(
        org_id=organization.id,
        posting_date="2026-03-05",
        idempotency_key="ambiguous-date",
        evidence_references=[sample_evidence.id],
        components=[{"key": "collection", "kind": "pass_through", "amount_fen": 1000}],
        funds=[
            {
                "key": key,
                "account_code": "1001",
                "direction": "receipt",
                "payment_date": day,
                "amount_fen": 500,
                "allocations": [{"component_key": "collection", "amount_fen": 500}],
            }
            for key, day in [("first", "2026-03-04"), ("second", "2026-03-05")]
        ],
    )
    result = ComponentService(session).record(request)
    assert result.errors == ["COMPONENT_MULTIPLE_PAYMENT_DATES_REQUIRES_SPLIT"]
    assert not session.scalars(select(BusinessEvent)).all()
