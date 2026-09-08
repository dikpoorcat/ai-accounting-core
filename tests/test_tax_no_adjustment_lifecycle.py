from __future__ import annotations

from datetime import date

from sqlalchemy import func, select

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import (
    BusinessEventComponent,
    BusinessEventDependency,
    Evidence,
    TaxPeriod,
    Voucher,
    VoucherLine,
    ZeroTaxPeriodConfirmation,
)
from ai_accounting.schemas import TaxPeriodConfirmRequest, TaxPeriodPreviewRequest
from ai_accounting.service import FinanceService


def test_tiny_vat_no_adjustment_confirmation_can_settle_without_fake_voucher(session, organization):
    evidence = Evidence(
        org_id=organization.id,
        original_name="tiny-vat.txt",
        storage_path="tiny-vat.txt",
        sha256="8" * 64,
        size_bytes=1,
        media_type="text/plain",
        source="test",
    )
    session.add(evidence)
    session.flush()
    sale = ComponentService(session).record(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "sqlite-tiny-vat-source",
                "posting_date": "2026-03-05",
                "evidence_references": [evidence.id],
                "components": [
                    {
                        "key": "tiny-special",
                        "kind": "service_sale",
                        "business_date": "2026-03-05",
                        "payment_date": "2026-03-05",
                        "amount_fen": 51,
                        "counterparty": {"kind": "customer", "name": "tiny-customer"},
                        "recognition_basis": "immediate",
                        "fulfillment_date": "2026-03-05",
                        "tax_obligation_date": "2026-03-05",
                        "tax_facts": {
                            "taxable": True,
                            "rate_percent": "1",
                            "invoice_type": "special",
                            "waive_exemption": False,
                            "tax_due_on_event": True,
                        },
                    }
                ],
                "funds": [
                    {
                        "key": "receipt",
                        "account_code": "1001",
                        "direction": "receipt",
                        "payment_date": "2026-03-05",
                        "amount_fen": 51,
                        "allocations": [{"component_key": "tiny-special", "amount_fen": 51}],
                    }
                ],
            }
        )
    )
    assert sale.status == "posted", sale

    period_request = TaxPeriodPreviewRequest(
        org_id=organization.id,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        adjustment_posting_date=date(2026, 3, 31),
    )
    service = FinanceService(session)
    preview = service.preview_tax_period(period_request)
    voucher_count = session.scalar(select(func.count()).select_from(Voucher))
    confirmation_result = service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            **period_request.model_dump(),
            calculation_hash=preview["calculation_hash"],
            idempotency_key="sqlite-tiny-vat-confirmation",
        )
    )
    assert confirmation_result.status == "posted", confirmation_result
    assert confirmation_result.event_id is None and confirmation_result.voucher_id is None
    assert session.scalar(select(func.count()).select_from(Voucher)) == voucher_count
    assert session.scalar(select(func.count()).select_from(TaxPeriod)) == 0
    confirmation = session.scalar(select(ZeroTaxPeriodConfirmation))
    assert confirmation.calculation["vat_payable_fen"] == 1
    assert confirmation.calculation["vat_relief_fen"] == 0
    assert confirmation.calculation["surtax_total_fen"] == 0

    payment_request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "sqlite-tiny-vat-payment",
            "posting_date": "2026-04-01",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "vat-payment",
                    "kind": "tax_settlement",
                    "business_date": "2026-04-01",
                    "payment_date": "2026-04-01",
                    "amount_fen": 1,
                    "tax_type": "vat",
                    "settlement_kind": "payment",
                    "period_start": "2026-01-01",
                    "period_end": "2026-03-31",
                }
            ],
            "funds": [
                {
                    "key": "payment",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": "2026-04-01",
                    "amount_fen": 1,
                    "allocations": [{"component_key": "vat-payment", "amount_fen": 1}],
                }
            ],
        }
    )
    paid = ComponentService(session).record(payment_request)
    replay = ComponentService(session).record(payment_request)
    assert paid.status == replay.status == "posted"
    assert paid.event_id == replay.event_id
    payment_component = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == paid.event_id,
            BusinessEventComponent.kind == "tax_settlement",
        )
    )
    source_component = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == sale.event_id,
            BusinessEventComponent.key == "tiny-special",
        )
    )
    line = session.scalar(
        select(VoucherLine).where(VoucherLine.component_id == payment_component.id)
    )
    dependency = session.scalar(
        select(BusinessEventDependency).where(
            BusinessEventDependency.child_component_id == payment_component.id
        )
    )
    assert line.account.code == "222101"
    assert (line.debit_fen, line.credit_fen) == (1, 0)
    assert payment_component.derived["tax_confirmation_id"] == str(confirmation.id)
    assert payment_component.derived["tax_confirmation_hash"] == confirmation.calculation_hash
    assert dependency.parent_component_id == source_component.id
    assert dependency.amount_fen == 1
