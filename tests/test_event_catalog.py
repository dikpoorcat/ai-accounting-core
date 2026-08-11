from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.models import OpenItem, Organization, VoucherLine
from ai_accounting.schemas import RecordEventRequest
from ai_accounting.service import FinanceService


def request(organization: Organization, payload: dict) -> RecordEventRequest:
    return RecordEventRequest.model_validate({"org_id": organization.id, **payload})


def assert_balanced(session: Session, voucher_id: object) -> None:
    lines = session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)).all()
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines) > 0


def test_payable_purchase_and_supplier_payment_workflow(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    purchase = service.record_event(
        request(
            organization,
            {
                "idempotency_key": "payable-expense",
                "event_type": "expense_payable",
                "business_dates": {
                    "business_date": "2026-08-01",
                    "posting_date": "2026-08-01",
                },
                "counterparty": {"kind": "supplier", "name": "甲供应商"},
                "amounts": {
                    "gross_amount_fen": 30_000,
                    "expense_account_role": "general_expense",
                },
            },
        )
    )
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == purchase.event_id))
    payment = service.record_event(
        request(
            organization,
            {
                "idempotency_key": "supplier-payment",
                "event_type": "supplier_payment",
                "business_dates": {
                    "business_date": "2026-08-02",
                    "posting_date": "2026-08-02",
                    "payment_date": "2026-08-02",
                },
                "counterparty": {"kind": "supplier", "name": "甲供应商"},
                "amounts": {"amount_fen": 30_000},
                "allocations": [{"open_item_id": item.id, "amount_fen": 30_000}],
            },
        )
    )
    assert payment.status == "posted"
    assert item.status == "settled"
    assert_balanced(session, payment.voucher_id)


def test_owner_loan_repayment_is_limited_by_counterparty_balance(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    received = service.record_event(
        request(
            organization,
            {
                "idempotency_key": "owner-loan",
                "event_type": "owner_loan_received",
                "business_dates": {
                    "business_date": "2026-08-01",
                    "posting_date": "2026-08-01",
                    "payment_date": "2026-08-01",
                },
                "counterparty": {"kind": "owner", "name": "张股东"},
                "amounts": {"amount_fen": 100_000},
            },
        )
    )
    assert received.status == "posted"
    repayment_payload = {
        "event_type": "owner_repayment",
        "business_dates": {
            "business_date": "2026-08-02",
            "posting_date": "2026-08-02",
            "payment_date": "2026-08-02",
        },
        "counterparty": {"kind": "owner", "name": "张股东"},
    }
    partial = service.record_event(
        request(
            organization,
            {
                **repayment_payload,
                "idempotency_key": "owner-repayment-1",
                "amounts": {"amount_fen": 60_000},
            },
        )
    )
    assert partial.status == "posted"
    excess = service.record_event(
        request(
            organization,
            {
                **repayment_payload,
                "idempotency_key": "owner-repayment-2",
                "amounts": {"amount_fen": 50_000},
            },
        )
    )
    assert excess.status == "rejected"
    assert "exceeds payable balance" in excess.errors[0]


def test_employee_bank_and_internal_transfer_events_are_balanced(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    payloads = [
        {
            "idempotency_key": "employee-unpaid",
            "event_type": "employee_reimbursement",
            "business_dates": {"business_date": "2026-08-01", "posting_date": "2026-08-01"},
            "counterparty": {"kind": "employee", "name": "李员工"},
            "amounts": {
                "gross_amount_fen": 8_800,
                "expense_account_role": "general_expense",
            },
            "details": {"paid_now": False},
        },
        {
            "idempotency_key": "bank-fee",
            "event_type": "bank_fee",
            "business_dates": {
                "business_date": "2026-08-01",
                "posting_date": "2026-08-01",
                "payment_date": "2026-08-01",
            },
            "amounts": {"amount_fen": 100},
        },
        {
            "idempotency_key": "cash-withdrawal",
            "event_type": "internal_transfer",
            "business_dates": {"business_date": "2026-08-01", "posting_date": "2026-08-01"},
            "amounts": {"amount_fen": 10_000},
            "details": {
                "source_account_code": "1002",
                "destination_account_code": "1001",
            },
        },
    ]
    for payload in payloads:
        result = service.record_event(request(organization, payload))
        assert result.status == "posted"
        assert_balanced(session, result.voucher_id)


def test_vat_payment_cannot_exceed_posted_liability(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    sale = service.record_event(
        request(
            organization,
            {
                "idempotency_key": "taxable-sale-for-payment",
                "event_type": "service_cash_sale",
                "business_dates": {
                    "business_date": "2026-08-01",
                    "posting_date": "2026-08-01",
                    "fulfillment_date": "2026-08-01",
                    "payment_date": "2026-08-01",
                    "tax_obligation_date": "2026-08-01",
                },
                "amounts": {"gross_amount_fen": 101_000},
                "tax_facts": {
                    "taxable": True,
                    "rate_percent": "1",
                    "invoice_type": "ordinary",
                    "waive_exemption": False,
                    "tax_due_on_event": True,
                },
            },
        )
    )
    assert sale.status == "posted"

    common = {
        "event_type": "tax_payment",
        "business_dates": {
            "business_date": "2026-08-02",
            "posting_date": "2026-08-02",
            "payment_date": "2026-08-02",
        },
        "details": {"tax_type": "vat"},
    }
    paid = service.record_event(
        request(
            organization,
            {**common, "idempotency_key": "vat-payment", "amounts": {"amount_fen": 1_000}},
        )
    )
    assert paid.status == "posted"
    excess = service.record_event(
        request(
            organization,
            {**common, "idempotency_key": "vat-overpayment", "amounts": {"amount_fen": 1}},
        )
    )
    assert excess.status == "rejected"


def test_advance_refund_cannot_exceed_unused_advance(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    advance = service.record_event(
        request(
            organization,
            {
                "idempotency_key": "advance-refund-source",
                "event_type": "customer_advance",
                "business_dates": {
                    "business_date": "2026-08-01",
                    "posting_date": "2026-08-01",
                    "payment_date": "2026-08-01",
                },
                "counterparty": {"kind": "customer", "name": "丙客户"},
                "amounts": {"gross_amount_fen": 50_000},
                "tax_facts": {
                    "taxable": True,
                    "rate_percent": "1",
                    "invoice_type": "none",
                    "waive_exemption": False,
                    "tax_due_on_event": False,
                },
            },
        )
    )
    common = {
        "event_type": "customer_refund",
        "business_dates": {
            "business_date": "2026-08-02",
            "posting_date": "2026-08-02",
            "payment_date": "2026-08-02",
        },
        "counterparty": {"kind": "customer", "name": "丙客户"},
        "details": {"refund_kind": "advance", "original_event_id": str(advance.event_id)},
    }
    partial = service.record_event(
        request(
            organization,
            {**common, "idempotency_key": "advance-refund-1", "amounts": {"amount_fen": 30_000}},
        )
    )
    assert partial.status == "posted"
    excess = service.record_event(
        request(
            organization,
            {**common, "idempotency_key": "advance-refund-2", "amounts": {"amount_fen": 30_000}},
        )
    )
    assert excess.status == "rejected"
