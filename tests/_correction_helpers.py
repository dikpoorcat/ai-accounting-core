"""Typed cleanup of explicitly mistaken open-period events in lifecycle tests."""

from ai_accounting.accounting_periods import canonical_sha256
from ai_accounting.event_amendment_schemas import DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import BusinessEvent


def delete_open_event(session, org_id, event_id, key):
    source = session.get(BusinessEvent, event_id)
    result = EventAmendmentService(session).amend(
        DeleteEventRequest(
            org_id=org_id,
            event_id=event_id,
            idempotency_key=key,
            expected_facts_hash=canonical_sha256(source.facts),
        )
    )
    assert result["status"] == "deleted", result
    return result
