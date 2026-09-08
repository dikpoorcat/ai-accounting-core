from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.models import Account, Evidence, OpenItem
from ai_accounting.service import FinanceService


@st.composite
def valid_settlement_sequences(draw: st.DrawFn) -> tuple[int, list[int]]:
    original = draw(st.integers(min_value=2, max_value=10_000_000))
    first = draw(st.integers(min_value=1, max_value=original))
    remaining = original - first
    second = draw(st.integers(min_value=0, max_value=remaining))
    return original, [value for value in (first, second) if value]


@given(sequence=valid_settlement_sequences())
@settings(max_examples=25, deadline=None)
def test_random_valid_receivable_sequences_preserve_open_item_conservation(
    sequence: tuple[int, list[int]],
) -> None:
    original, payments = sequence
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory.begin() as session:
            organization = seed_organization(
                session,
                taxpayer_identification_number="91330106MA1234567T",
                name="性质测试公司",
            )
            organization.accounting_period_control_enabled = False
            bank_account = session.scalar(
                select(Account).where(
                    Account.org_id == organization.id,
                    Account.code == "1002",
                )
            )
            assert bank_account is not None
            configured_at = datetime.now(UTC)
            bank_account.requires_bank_reconciliation = True
            bank_account.bank_reconciliation_start_date = date(2020, 1, 1)
            bank_account.bank_reconciliation_configured_at = configured_at
            session.flush()
            set_committed_value(
                organization,
                "bank_reconciliation_scope_current_action_id",
                uuid.uuid4(),
            )
            set_committed_value(
                organization,
                "bank_reconciliation_scope_confirmed_at",
                configured_at,
            )
            evidence = Evidence(
                org_id=organization.id,
                sha256="a" * 64,
                original_name="property-test.txt",
                media_type="text/plain",
                source="test",
                size_bytes=1,
                storage_path="test/property-test.txt",
            )
            session.add(evidence)
            session.flush()
            service = FinanceService(session)
            sale = service.record_event(
                RecordEventRequest(
                    org_id=organization.id,
                    idempotency_key=f"sale-{uuid.uuid4()}",
                    posting_date=date(2026, 8, 1),
                    evidence_references=[evidence.id],
                    components=[
                        {
                            "key": "sale",
                            "kind": "service_sale",
                            "business_date": "2026-08-01",
                            "fulfillment_date": "2026-08-01",
                            "amount_fen": original,
                            "counterparty": {
                                "kind": "customer",
                                "name": "性质测试客户",
                            },
                            "recognition_basis": "credit",
                            "tax_facts": {
                                "taxable": False,
                                "rate_percent": "0",
                                "invoice_type": "none",
                                "waive_exemption": False,
                                "tax_due_on_event": False,
                            },
                        }
                    ],
                )
            )
            assert sale.status == "posted", (sale.errors, sale.missing_information)
            item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == sale.event_id))
            assert item is not None
            for index, payment in enumerate(payments):
                result = service.record_event(
                    RecordEventRequest(
                        org_id=organization.id,
                        idempotency_key=f"receipt-{index}-{uuid.uuid4()}",
                        posting_date=date(2026, 8, 2),
                        evidence_references=[evidence.id],
                        components=[
                            {
                                "key": "settle",
                                "kind": "receivable_settlement",
                                "business_date": "2026-08-02",
                                "payment_date": "2026-08-02",
                                "counterparty": {
                                    "kind": "customer",
                                    "name": "性质测试客户",
                                },
                                "allocations": [{"open_item_id": item.id, "amount_fen": payment}],
                            }
                        ],
                        funds=[
                            {
                                "key": "receipt",
                                "account_code": "1002",
                                "direction": "receipt",
                                "payment_date": "2026-08-02",
                                "amount_fen": payment,
                                "allocations": [{"component_key": "settle", "amount_fen": payment}],
                            }
                        ],
                    )
                )
                assert result.status == "posted"
            assert item.original_amount_fen == original
            assert item.settled_amount_fen == sum(payments)
            assert item.original_amount_fen - item.settled_amount_fen >= 0
            assert item.status == ("settled" if sum(payments) == original else "partial")
    finally:
        engine.dispose()
