import uuid

import pytest
from _postgres_helpers import catalog_owner_authority
from sqlalchemy import event as sqlalchemy_event
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from test_business_components import expense, request
from test_financial_statements_postgres import _isolated_business_engine

from ai_accounting.coa import seed_organization
from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import AmendEventRequest, DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    Evidence,
    OpenItem,
    Settlement,
    Voucher,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


def test_postgres_whole_component_amend_delete_and_reverse_are_atomic():
    with _isolated_business_engine() as engine, Session(engine) as session:
        database_errors = []
        sqlalchemy_event.listen(
            engine,
            "handle_error",
            lambda context: database_errors.append(str(context.original_exception)),
        )
        org = seed_organization(
            session,
            name="Component lifecycle test",
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
        )
        session.commit()
        with catalog_owner_authority(session, org) as authority:
            with authority.attributed_call(session, tool_name="finance_register_evidence"):
                proof = Evidence(
                    org_id=org.id,
                    original_name="lifecycle.txt",
                    storage_path="test/lifecycle.txt",
                    sha256=uuid.uuid4().hex * 2,
                    size_bytes=1,
                    media_type="text/plain",
                    source="test",
                )
                session.add(proof)
                session.flush()
            session.commit()
            service = ComponentService(session)
            original = request(org, proof, [expense("travel", 100), expense("fee", 20)])
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = service.record(original)
                assert posted.status == "posted", posted
            session.commit()
            identities = {c.key: c.id for c in session.scalars(select(BusinessEventComponent))}
            replacement = request(org, proof, [expense("fee", 30), expense("travel", 200)])
            with authority.attributed_call(session, tool_name="finance_amend_event"):
                amended = EventAmendmentService(session).amend(
                    AmendEventRequest(
                        org_id=org.id,
                        event_id=posted.event_id,
                        idempotency_key="amend-whole",
                        expected_facts_hash=posted.data["facts_hash"],
                        reason="核对原始依据",
                        replacement=replacement,
                    )
                )
                assert amended["status"] == "posted", (amended, database_errors)
            session.commit()
            voucher = session.scalar(select(Voucher))
            assert (voucher.id, voucher.voucher_number) == (
                posted.voucher_id,
                posted.voucher_number,
            )
            assert {
                c.key: c.id for c in session.scalars(select(BusinessEventComponent))
            } == identities
            event = session.get(BusinessEvent, posted.event_id)
            current = service.result(event)
            with authority.attributed_call(session, tool_name="finance_delete_event"):
                deleted = EventAmendmentService(session).amend(
                    DeleteEventRequest(
                        org_id=org.id,
                        event_id=event.id,
                        idempotency_key="delete-whole",
                        expected_facts_hash=current.data["facts_hash"],
                        reason="整笔误录",
                    )
                )
                assert deleted["status"] == "deleted", deleted
            session.commit()
            assert session.scalar(select(func.count()).select_from(Voucher)) == 0
            assert session.scalar(select(func.count()).select_from(BusinessEventComponent)) == 0

            party = {"kind": "supplier", "name": "Local source supplier"}
            components = [
                expense(
                    "credit", 100, payment_basis="supplier_credit", metadata={"counterparty": party}
                ),
                {
                    "key": "settle",
                    "kind": "payable_settlement",
                    "business_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "allocations": [{"source_component_key": "credit", "amount_fen": 100}],
                    "metadata": {"counterparty": party},
                },
            ]
            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = service.record(
                    request(org, proof, components, key="local-source", amounts=[("settle", 100)])
                )
                assert posted.status == "posted", posted
            session.commit()
            with authority.attributed_call(session, tool_name="finance_reverse_event"):
                reversed_result = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org.id,
                        event_id=posted.event_id,
                        idempotency_key="reverse-local-source",
                        posting_date="2026-03-06",
                        reason="整笔更正来源及核销",
                    )
                )
                assert reversed_result.status == "posted", reversed_result
            session.commit()
            item = session.scalar(select(OpenItem))
            assert item.status == "reversed" and item.settled_amount_fen == 0
            assert session.scalar(select(Settlement)).reversed is True


def test_postgres_refund_receipt_dependencies_prevent_partial_lifecycle_changes():
    from test_component_refund_lifecycle import exercise_refund_receipt_dependencies

    with _isolated_business_engine() as engine, Session(engine) as session:
        org = seed_organization(
            session,
            name="Refund dependency test",
            taxpayer_identification_number="91330106MA1234567T",
            accounting_period_control_enabled=False,
        )
        session.commit()
        with catalog_owner_authority(session, org) as authority:
            with authority.attributed_call(session, tool_name="finance_register_evidence"):
                proof = Evidence(
                    org_id=org.id,
                    original_name="refund.txt",
                    storage_path="test/refund.txt",
                    sha256=uuid.uuid4().hex * 2,
                    size_bytes=1,
                    media_type="text/plain",
                    source="test",
                )
                session.add(proof)
                session.flush()
            session.commit()
            exercise_refund_receipt_dependencies(session, org, proof, authority)
            session.commit()
