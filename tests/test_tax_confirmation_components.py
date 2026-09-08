from __future__ import annotations

from datetime import date

from sqlalchemy import func, select
from test_financial_statements import _evidence

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    TaxPeriod,
    TaxPeriodSource,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService


def _sale(key: str, day: date, amount_fen: int, *, invoice_type: str = "special") -> dict:
    return {
        "key": key,
        "kind": "service_sale",
        "business_date": day,
        "amount_fen": amount_fen,
        "counterparty": {"kind": "customer", "name": "组合税务客户"},
        "recognition_basis": "immediate",
        "fulfillment_date": day,
        "tax_obligation_date": day,
        "tax_facts": {
            "taxable": True,
            "rate_percent": "1",
            "invoice_type": invoice_type,
            "waive_exemption": invoice_type == "special",
            "tax_due_on_event": True,
        },
    }


def _relief(key: str, start: date, end: date) -> dict:
    return {
        "key": key,
        "kind": "tax_relief",
        "business_date": end,
        "start_date": start,
        "end_date": end,
        "calculation_hash": None,
    }


def test_preview_projects_pending_sale_into_empty_relief_and_local_payment(session, organization):
    evidence = _evidence(session, organization, "projected-tax-period.txt")
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "projected-tax-period-payment",
            "posting_date": date(2026, 3, 31),
            "description": "同一事件确认销售、税期并缴税",
            "evidence_references": [evidence.id],
            # Relief deliberately precedes the taxable source in the envelope.
            "components": [
                _relief("period", date(2026, 1, 1), date(2026, 3, 31)),
                _sale("sale", date(2026, 3, 5), 101),
                {
                    "key": "vat-payment",
                    "kind": "tax_settlement",
                    "business_date": date(2026, 3, 31),
                    "payment_date": date(2026, 3, 31),
                    "amount_fen": 1,
                    "tax_type": "vat",
                    "settlement_kind": "payment",
                    "period_start": date(2026, 1, 1),
                    "period_end": date(2026, 3, 31),
                    "assessment_component_key": "period",
                },
            ],
            "funds": [
                {
                    "key": "sale-cash",
                    "account_code": "1001",
                    "direction": "receipt",
                    "payment_date": date(2026, 3, 31),
                    "amount_fen": 101,
                    "allocations": [{"component_key": "sale", "amount_fen": 101}],
                    "bank_transaction_references": [],
                },
                {
                    "key": "tax-cash",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": date(2026, 3, 31),
                    "amount_fen": 1,
                    "allocations": [{"component_key": "vat-payment", "amount_fen": 1}],
                    "bank_transaction_references": [],
                },
            ],
        }
    )
    before_events = session.scalar(select(func.count()).select_from(BusinessEvent))
    preview = ComponentService(session).preview(request)
    assert preview.status == "calculated", preview
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == before_events
    assert session.scalar(select(func.count()).select_from(TaxPeriod)) == 0
    reviewed = RecordEventRequest.model_validate(preview.data["reviewed_request"])
    assert reviewed.components[0].calculation_hash == preview.data["confirmation_hashes"]["period"]

    posted = FinanceService(session).record_event(reviewed)
    assert posted.status == "posted", posted
    period = session.scalar(select(TaxPeriod))
    assert period is not None
    assert period.calculation["vat_accrued_fen"] == 1
    assert period.calculation["vat_relief_fen"] == 0
    assert period.calculation["surtax_total_fen"] == 0
    assert period.calculation["vat_payable_fen"] == 1
    assert len(period.calculation["source_event_snapshots"]) == 1
    assert period.calculation["source_review_snapshots"] == [
        {
            "event_idempotency_key": reviewed.idempotency_key,
            "component_key": "sale",
            "gross_fen": 101,
            "net_fen": 100,
            "vat_fen": 1,
            "exemption_eligible": False,
        }
    ]
    relief = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == posted.event_id,
            BusinessEventComponent.key == "period",
        )
    )
    assert relief.derived["_posting_entries"] == []
    assert relief.derived["source_review_proofs"] == period.calculation["source_review_snapshots"]
    assert session.scalar(select(func.count()).select_from(TaxPeriodSource)) == 1


def test_two_disjoint_projected_tax_periods_reverse_with_their_shared_event(session, organization):
    evidence = _evidence(session, organization, "two-projected-tax-periods.txt")
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "two-projected-tax-periods",
            "posting_date": date(2026, 6, 30),
            "description": "同事件两期销售与税期确认",
            "evidence_references": [evidence.id],
            "components": [
                _relief("q2-period", date(2026, 4, 1), date(2026, 6, 30)),
                _relief("q1-period", date(2026, 1, 1), date(2026, 3, 31)),
                _sale("q1-sale", date(2026, 3, 5), 101, invoice_type="ordinary"),
                _sale("q2-sale", date(2026, 6, 5), 101, invoice_type="ordinary"),
            ],
            "funds": [
                {
                    "key": "cash",
                    "account_code": "1001",
                    "direction": "receipt",
                    "payment_date": date(2026, 6, 30),
                    "amount_fen": 202,
                    "allocations": [
                        {"component_key": "q1-sale", "amount_fen": 101},
                        {"component_key": "q2-sale", "amount_fen": 101},
                    ],
                    "bank_transaction_references": [],
                }
            ],
        }
    )
    preview = ComponentService(session).preview(request)
    assert preview.status == "calculated", preview
    posted = FinanceService(session).record_event(
        RecordEventRequest.model_validate(preview.data["reviewed_request"])
    )
    assert posted.status == "posted", posted
    periods = list(session.scalars(select(TaxPeriod).order_by(TaxPeriod.start_date)))
    assert len(periods) == 2
    assert [period.calculation["vat_relief_fen"] for period in periods] == [1, 1]
    assert [len(period.sources) for period in periods] == [1, 1]

    reversed_result = FinanceService(session).reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=posted.event_id,
            posting_date=date(2026, 7, 1),
            reason="撤销同事件的两期确认",
            idempotency_key="reverse-two-projected-tax-periods",
        )
    )
    assert reversed_result.status == "posted", reversed_result
    session.expire_all()
    assert all(period.status == "reversed" for period in periods)
