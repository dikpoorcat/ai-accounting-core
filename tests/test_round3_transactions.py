from __future__ import annotations

import shutil
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from threading import Barrier

import pytest
from _postgres_helpers import authenticated_business_database

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.database import make_session_factory
from ai_accounting.models import BusinessEvent
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _expense_request(org_id: object, *, key: str) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "posting_date": "2026-03-05",
            "components": [
                {
                    "key": "expense",
                    "kind": "expense",
                    "business_date": "2026-03-05",
                    "payment_date": "2026-03-05",
                    "amount_fen": 100,
                    "expense_class": "general_expense",
                    "payment_basis": "supplier_credit",
                    "counterparty": {"kind": "supplier", "name": "R3 测试供应商"},
                }
            ],
        }
    )


def _reverse_request(org_id: object, event_id: object, *, key: str) -> ReverseEventRequest:
    return ReverseEventRequest(
        org_id=org_id,
        event_id=event_id,
        idempotency_key=key,
        reason="R3 并发冲正",
        posting_date=date(2026, 3, 6),
    )


def test_r3_008_reversal_replays_after_source_lock_and_r3_009_rebooks_source() -> None:
    """Real authenticated sessions race the same reversal and independent corrections."""
    with authenticated_business_database("reversal_race") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        factory = make_session_factory(engine)

        def post(key):
            with factory.begin() as session:
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    result = FinanceService(session).record_event(
                        _expense_request(org_id, key=key).model_copy(
                            update={"evidence_references": [evidence_id]}
                        )
                    )
                assert result.status == "posted", result
                return result

        original = post("r3-original-expense")
        same_request = _reverse_request(org_id, original.event_id, key="r3-concurrent-reversal")
        barrier = Barrier(2)

        def reverse(request):
            barrier.wait(timeout=10)
            with factory.begin() as session:
                with authority.attributed_call(session, tool_name="finance_reverse_event"):
                    return FinanceService(session).reverse_event(request)

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(reverse, [same_request, same_request]))
        assert {result.status for result in results} == {"posted"}
        assert len({result.event_id for result in results}) == 1
        rebooked = post("r3-corrected-expense")
        with factory() as session:
            assert session.get(BusinessEvent, original.event_id).status == "reversed"
            assert session.get(BusinessEvent, rebooked.event_id).status == "posted"

        barrier = Barrier(2)
        requests = [
            _reverse_request(org_id, rebooked.event_id, key=f"r3-reversal-race-{i}")
            for i in range(2)
        ]
        with ThreadPoolExecutor(max_workers=2) as executor:
            contended = list(executor.map(reverse, requests))
        assert {result.status for result in contended} == {"posted", "rejected"}
        rejected = next(result for result in contended if result.status == "rejected")
        assert rejected.errors == ["EVENT_IS_NOT_REVERSIBLE"]
