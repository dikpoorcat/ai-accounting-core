from __future__ import annotations

from typing import Any

import pytest
from _postgres_helpers import authenticated_business_database
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.models import OpenItem
from ai_accounting.service import FinanceService

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


def _request(org_id: object, payload: dict[str, Any]) -> RecordEventRequest:
    return RecordEventRequest.model_validate({"org_id": org_id, **payload})


def test_postgres_person_debt_transfer_and_cash_settlement_are_final() -> None:
    with authenticated_business_database(
        "person_reimbursement", name="个人代垫 PostgreSQL 测试"
    ) as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
                service = FinanceService(session)
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    purchase = service.record_event(
                        _request(
                            org_id,
                            {
                                "idempotency_key": "pg-person-payable-source",
                                "posting_date": "2026-08-01",
                                "evidence_references": [evidence_id],
                                "components": [
                                    {
                                        "key": "expense",
                                        "kind": "expense",
                                        "business_date": "2026-08-01",
                                        "amount_fen": 50_000,
                                        "expense_class": "general_expense",
                                        "payment_basis": "supplier_credit",
                                        "counterparty": {
                                            "kind": "supplier",
                                            "name": "测试供应商",
                                        },
                                    }
                                ],
                            },
                        )
                    )
                assert purchase.status == "posted", purchase.errors
                session.commit()

                source_item = session.scalar(
                    select(OpenItem).where(OpenItem.source_event_id == purchase.event_id)
                )
                assert source_item is not None
                payer = {"kind": "employee", "name": "测试员工"}
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    on_behalf = service.record_event(
                        _request(
                            org_id,
                            {
                                "idempotency_key": "pg-person-paid-on-behalf",
                                "posting_date": "2026-08-02",
                                "evidence_references": [evidence_id],
                                "components": [
                                    {
                                        "key": "transfer",
                                        "kind": "debt_transfer",
                                        "business_date": "2026-08-02",
                                        "payment_date": "2026-08-02",
                                        "payer": payer,
                                        "allocations": [
                                            {
                                                "open_item_id": source_item.id,
                                                "amount_fen": 50_000,
                                            }
                                        ],
                                    }
                                ],
                            },
                        )
                    )
                assert on_behalf.status == "posted", on_behalf.errors
                session.commit()

                person_item = session.scalar(
                    select(OpenItem).where(OpenItem.source_event_id == on_behalf.event_id)
                )
                assert person_item is not None
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    cash_payment = service.record_event(
                        _request(
                            org_id,
                            {
                                "idempotency_key": "pg-person-cash-settlement",
                                "posting_date": "2026-08-02",
                                "evidence_references": [evidence_id],
                                "components": [
                                    {
                                        "key": "settlement",
                                        "kind": "payable_settlement",
                                        "business_date": "2026-08-02",
                                        "payment_date": "2026-08-02",
                                        "counterparty": payer,
                                        "allocations": [
                                            {
                                                "open_item_id": person_item.id,
                                                "amount_fen": 50_000,
                                            }
                                        ],
                                    }
                                ],
                                "funds": [
                                    {
                                        "key": "cash",
                                        "account_code": "1001",
                                        "direction": "payment",
                                        "payment_date": "2026-08-02",
                                        "amount_fen": 50_000,
                                        "allocations": [
                                            {
                                                "component_key": "settlement",
                                                "amount_fen": 50_000,
                                            }
                                        ],
                                    }
                                ],
                            },
                        )
                    )
                assert cash_payment.status == "posted", cash_payment.errors
                session.commit()
                assert source_item.status == "settled"
                assert person_item.status == "settled"
