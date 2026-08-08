from __future__ import annotations

import uuid
from datetime import date

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.models import (
    AccountingPeriod,
    BankTransaction,
    OpenItem,
    Organization,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import (
    BankTransactionReference,
    RecordEventRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService


def sale_request(
    organization: Organization,
    *,
    event_type: str = "service_cash_sale",
    amount_fen: int = 1_010_000,
    key: str | None = None,
) -> RecordEventRequest:
    payload = {
        "org_id": str(organization.id),
        "idempotency_key": key or f"sale-{uuid.uuid4()}",
        "event_type": event_type,
        "business_dates": {
            "business_date": "2026-08-08",
            "posting_date": "2026-08-08",
            "fulfillment_date": "2026-08-08",
            "payment_date": "2026-08-08" if event_type == "service_cash_sale" else None,
            "tax_obligation_date": "2026-08-08",
        },
        "amounts": {"gross_amount_fen": amount_fen},
        "tax_facts": {
            "taxable": True,
            "rate_percent": "1",
            "invoice_type": "ordinary",
            "tax_due_on_event": True,
        },
        "description": "咨询服务",
    }
    if event_type == "service_credit_sale":
        payload["counterparty"] = {"kind": "customer", "name": "甲客户"}
    return RecordEventRequest.model_validate(payload)


def voucher_totals(session: Session, voucher_id: uuid.UUID) -> tuple[int, int]:
    lines = session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)).all()
    return sum(line.debit_fen for line in lines), sum(line.credit_fen for line in lines)


def test_cash_sale_10100_posts_expected_voucher(
    session: Session, organization: Organization
) -> None:
    result = FinanceService(session).record_event(sale_request(organization))

    assert result.status == "posted"
    assert voucher_totals(session, result.voucher_id) == (1_010_000, 1_010_000)
    voucher = session.get(Voucher, result.voucher_id)
    by_code = {line.account.code: line for line in voucher.lines}
    assert by_code["1002"].debit_fen == 1_010_000
    assert by_code["5001"].credit_fen == 1_000_000
    assert by_code["222101"].credit_fen == 10_000


def test_unclassified_receipt_never_credits_receivable(
    session: Session, organization: Organization
) -> None:
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "unknown-receipt",
            "event_type": "customer_receipt",
            "business_dates": {
                "business_date": "2026-08-08",
                "posting_date": "2026-08-08",
                "payment_date": "2026-08-08",
            },
            "counterparty": {"kind": "customer", "name": "未知客户"},
            "amounts": {"amount_fen": 29_849_401},
            "description": "银行收到款项，性质未确认",
        }
    )
    result = FinanceService(session).record_event(request)

    assert result.status == "needs_information"
    assert result.voucher_id is None
    assert "allocations or details.unallocated_treatment='advance'" in result.missing_information


def test_credit_sale_partial_settlement_and_oversettlement_rejected(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    sale = service.record_event(
        sale_request(organization, event_type="service_credit_sale", amount_fen=101_000)
    )
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == sale.event_id))
    assert item.original_amount_fen == 101_000

    partial = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "partial-receipt",
            "event_type": "customer_receipt",
            "business_dates": {
                "business_date": "2026-08-09",
                "posting_date": "2026-08-09",
                "payment_date": "2026-08-09",
            },
            "counterparty": {"kind": "customer", "name": "甲客户"},
            "amounts": {"amount_fen": 40_000},
            "allocations": [{"open_item_id": item.id, "amount_fen": 40_000}],
        }
    )
    assert service.record_event(partial).status == "posted"
    assert item.settled_amount_fen == 40_000
    assert item.status == "open"

    excessive = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "excessive-receipt",
            "event_type": "customer_receipt",
            "business_dates": {
                "business_date": "2026-08-10",
                "posting_date": "2026-08-10",
                "payment_date": "2026-08-10",
            },
            "counterparty": {"kind": "customer", "name": "甲客户"},
            "amounts": {"amount_fen": 70_000},
            "allocations": [{"open_item_id": item.id, "amount_fen": 70_000}],
        }
    )
    rejected = service.record_event(excessive)
    assert rejected.status == "rejected"
    assert "exceeds open amount" in rejected.errors[0]
    assert item.settled_amount_fen == 40_000


def test_idempotency_replays_original_result(session: Session, organization: Organization) -> None:
    request = sale_request(organization, key="stable-bank-row-1")
    service = FinanceService(session)
    first = service.record_event(request)
    second = service.record_event(request)

    assert first.event_id == second.event_id
    assert first.voucher_id == second.voucher_id
    assert second.data["idempotent_replay"] is True
    assert session.query(Voucher).count() == 1


def test_closed_period_rejects_posting(session: Session, organization: Organization) -> None:
    session.add(
        AccountingPeriod(
            org_id=organization.id,
            start_date=date(2026, 8, 1),
            end_date=date(2026, 8, 31),
            status="closed",
        )
    )
    session.flush()
    result = FinanceService(session).record_event(sale_request(organization))
    assert result.status == "rejected"
    assert "closed" in result.errors[0]


def test_reversal_swaps_lines_and_keeps_original_voucher(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    posted = service.record_event(sale_request(organization))
    original_lines = session.scalars(
        select(VoucherLine).where(VoucherLine.voucher_id == posted.voucher_id)
    ).all()

    reversed_result = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=posted.event_id,
            idempotency_key="reverse-sale-1",
            reason="银行退回重复收款",
            posting_date=date(2026, 8, 9),
        )
    )
    assert reversed_result.status == "posted"
    reversal_lines = session.scalars(
        select(VoucherLine).where(VoucherLine.voucher_id == reversed_result.voucher_id)
    ).all()
    assert [(line.debit_fen, line.credit_fen) for line in reversal_lines] == [
        (line.credit_fen, line.debit_fen) for line in original_lines
    ]
    assert session.get(Voucher, posted.voucher_id) is not None


def test_small_taxpayer_purchase_is_gross_expense(
    session: Session, organization: Organization
) -> None:
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "expense-1",
            "event_type": "expense_cash",
            "business_dates": {
                "business_date": "2026-08-08",
                "posting_date": "2026-08-08",
                "payment_date": "2026-08-08",
                "invoice_date": "2026-08-08",
            },
            "amounts": {"gross_amount_fen": 10_300, "expense_account_role": "general_expense"},
            "invoice_references": [
                {
                    "number": "IN-001",
                    "direction": "input",
                    "invoice_type": "ordinary",
                    "issue_date": "2026-08-08",
                    "gross_amount_fen": 10_300,
                    "tax_amount_fen": 300,
                }
            ],
        }
    )
    result = FinanceService(session).record_event(request)
    voucher = session.get(Voucher, result.voucher_id)
    assert len(voucher.lines) == 2
    assert next(line for line in voucher.lines if line.account.code == "5602").debit_fen == 10_300


def test_customer_advance_cannot_be_fulfilled_twice(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    advance = service.record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "advance-1",
                "event_type": "customer_advance",
                "business_dates": {
                    "business_date": "2026-08-01",
                    "posting_date": "2026-08-01",
                    "payment_date": "2026-08-01",
                    "tax_obligation_date": "2026-08-01",
                },
                "counterparty": {"kind": "customer", "name": "乙客户"},
                "amounts": {"gross_amount_fen": 101_000},
                "tax_facts": {
                    "taxable": True,
                    "rate_percent": "1",
                    "tax_due_on_event": True,
                },
            }
        )
    )
    assert advance.status == "posted"

    def fulfillment(key: str, amount: int) -> RecordEventRequest:
        return RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": key,
                "event_type": "service_fulfillment",
                "business_dates": {
                    "business_date": "2026-08-10",
                    "posting_date": "2026-08-10",
                    "fulfillment_date": "2026-08-10",
                },
                "counterparty": {"kind": "customer", "name": "乙客户"},
                "amounts": {"gross_amount_fen": amount},
                "tax_facts": {
                    "taxable": True,
                    "rate_percent": "1",
                    "tax_due_on_event": False,
                },
                "details": {
                    "recognition_source": "contract_liability",
                    "tax_previously_accrued": True,
                    "original_event_id": str(advance.event_id),
                },
            }
        )

    first = service.record_event(fulfillment("fulfill-1", 101_000))
    assert first.status == "posted"
    voucher = session.get(Voucher, first.voucher_id)
    assert sum(line.debit_fen for line in voucher.lines) == 100_000
    assert sum(line.credit_fen for line in voucher.lines) == 100_000

    duplicate_consumption = service.record_event(fulfillment("fulfill-2", 1))
    assert duplicate_consumption.status == "rejected"
    assert "exceeds the unused customer advance" in duplicate_consumption.errors[0]


def test_mismatched_bank_row_is_not_linked(session: Session, organization: Organization) -> None:
    bank_row = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="a" * 64,
        booking_date=date(2026, 8, 8),
        amount_fen=999,
        currency="CNY",
        memo="金额不一致",
        source_sha256="b" * 64,
    )
    session.add(bank_row)
    session.flush()
    request = sale_request(organization, amount_fen=1_010_000)
    request.bank_transaction_references.append(BankTransactionReference(id=bank_row.id))
    result = FinanceService(session).record_event(request)
    assert result.status == "rejected"
    assert "does not match event amount" in result.errors[0]
    session.refresh(bank_row)
    assert bank_row.matched_event_id is None
