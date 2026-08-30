from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.models import (
    Account,
    BankTransaction,
    Evidence,
    OpenItem,
    Organization,
    Settlement,
    VoucherLine,
)
from ai_accounting.schemas import RecordEventRequest
from ai_accounting.service import FinanceService


@pytest.fixture(autouse=True)
def confirmed_bank_scope(session: Session, organization: Organization) -> None:
    configured_at = datetime.now(UTC)
    primary = session.scalar(
        select(Account).where(Account.org_id == organization.id, Account.code == "1002")
    )
    primary.requires_bank_reconciliation = True
    primary.bank_reconciliation_start_date = date(2000, 1, 1)
    primary.bank_reconciliation_configured_at = configured_at
    session.add(
        Account(
            org_id=organization.id,
            code="1003",
            name="事件目录测试银行二户",
            category="asset",
            normal_side="debit",
            active=True,
            requires_bank_reconciliation=True,
            bank_reconciliation_start_date=date(2000, 1, 1),
            bank_reconciliation_configured_at=configured_at,
        )
    )
    session.flush()
    set_committed_value(organization, "bank_reconciliation_scope_current_action_id", uuid.uuid4())
    set_committed_value(organization, "bank_reconciliation_scope_confirmed_at", configured_at)


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
                "bank_account_code": "1002",
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
                "bank_account_code": "1002",
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
        "bank_account_code": "1002",
    }
    partial = service.record_event(
        request(
            organization,
            {
                **repayment_payload,
                "idempotency_key": "owner-repayment-1",
                "amounts": {"amount_fen": 60_020},
                "details": {"owner_repayment_fee_fen": 20},
            },
        )
    )
    assert partial.status == "posted"
    assert partial.data["derived"] == {
        "owner_repayment_principal_fen": 60_000,
        "owner_repayment_fee_fen": 20,
    }
    assert_balanced(session, partial.voucher_id)
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
    evidence = Evidence(
        org_id=organization.id,
        sha256="9" * 64,
        original_name="payment-platform-owner-confirmation.txt",
        media_type="text/plain",
        source="test",
        size_bytes=1,
        storage_path="test/payment-platform-owner-confirmation.txt",
    )
    platform_bank_row = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="8" * 64,
        booking_date=date(2026, 8, 1),
        amount_fen=-10_000,
        currency="CNY",
        memo="转入支付宝",
        source_sha256="7" * 64,
    )
    session.add_all([evidence, platform_bank_row])
    session.flush()
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
            "bank_account_code": "1002",
        },
        {
            "idempotency_key": "bank-to-bank-transfer",
            "event_type": "internal_transfer",
            "business_dates": {"business_date": "2026-08-01", "posting_date": "2026-08-01"},
            "amounts": {"amount_fen": 10_000},
            "source_bank_account_code": "1002",
            "destination_bank_account_code": "1003",
        },
        {
            "idempotency_key": "cash-withdrawal",
            "event_type": "cash_bank_transfer",
            "business_dates": {"business_date": "2026-08-01", "posting_date": "2026-08-01"},
            "amounts": {"amount_fen": 10_000},
            "direction": "cash_withdrawal",
            "bank_account_code": "1002",
        },
        {
            "idempotency_key": "bank-to-payment-platform",
            "event_type": "payment_platform_transfer",
            "business_dates": {"business_date": "2026-08-01", "posting_date": "2026-08-01"},
            "amounts": {"amount_fen": 10_000},
            "direction": "to_platform",
            "bank_account_code": "1002",
            "bank_transaction_references": [{"id": platform_bank_row.id}],
            "evidence_references": [evidence.id],
            "description": "转入公司自有支付平台账户",
        },
    ]
    for payload in payloads:
        result = service.record_event(request(organization, payload))
        assert result.status == "posted"
        assert_balanced(session, result.voucher_id)


def test_employee_reimbursement_claims_create_payables_and_one_payment_settles_them(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    employee = {"kind": "employee", "name": "测试员工甲"}
    expense = service.record_event(
        request(
            organization,
            {
                "idempotency_key": "employee-expense-claim",
                "event_type": "employee_reimbursement",
                "business_dates": {
                    "business_date": "2026-04-10",
                    "posting_date": "2026-04-10",
                },
                "counterparty": employee,
                "amounts": {
                    "gross_amount_fen": 1_234_500,
                    "expense_account_role": "labor_service_cost",
                },
                "details": {"paid_now": False, "reimbursement_kind": "expense"},
            },
        )
    )
    deposit = service.record_event(
        request(
            organization,
            {
                "idempotency_key": "employee-deposit-claim",
                "event_type": "employee_reimbursement",
                "business_dates": {
                    "business_date": "2026-04-10",
                    "posting_date": "2026-04-10",
                },
                "counterparty": employee,
                "deposit_holder": {"kind": "other", "name": "出租方"},
                "amounts": {"gross_amount_fen": 345_600},
                "details": {
                    "paid_now": False,
                    "reimbursement_kind": "refundable_deposit",
                },
            },
        )
    )
    assert expense.status == deposit.status == "posted"
    expense_lines = session.scalars(
        select(VoucherLine).where(VoucherLine.voucher_id == expense.voucher_id)
    ).all()
    labor_service_cost_account = session.scalar(
        select(Account).where(
            Account.org_id == organization.id,
            Account.system_role == "labor_service_cost",
        )
    )
    assert labor_service_cost_account is not None
    assert any(
        line.account_id == labor_service_cost_account.id
        and line.debit_fen == 1_234_500
        for line in expense_lines
    )
    claims = session.scalars(
        select(OpenItem)
        .where(OpenItem.source_event_id.in_([expense.event_id, deposit.event_id]))
        .order_by(OpenItem.original_amount_fen)
    ).all()
    assert [item.original_amount_fen for item in claims] == [345_600, 1_234_500]
    assert all(item.status == "open" for item in claims)

    bank = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="e" * 64,
        booking_date=date(2026, 4, 10),
        amount_fen=-1_580_100,
        currency="CNY",
        memo="员工报销",
        source_sha256="f" * 64,
    )
    session.add(bank)
    session.flush()
    payment = service.record_event(
        request(
            organization,
            {
                "idempotency_key": "employee-payment",
                "event_type": "employee_reimbursement_payment",
                "business_dates": {
                    "business_date": "2026-04-10",
                    "posting_date": "2026-04-10",
                    "payment_date": "2026-04-10",
                },
                "counterparty": employee,
                "amounts": {"amount_fen": 1_580_100},
                "bank_account_code": "1002",
                "bank_transaction_references": [{"id": bank.id}],
                "allocations": [
                    {"open_item_id": item.id, "amount_fen": item.original_amount_fen}
                    for item in claims
                ],
            },
        )
    )

    assert payment.status == "posted"
    assert_balanced(session, payment.voucher_id)
    assert bank.matched_event_id == payment.event_id
    assert all(item.status == "settled" for item in claims)
    assert session.query(Settlement).filter_by(payment_event_id=payment.event_id).count() == 2


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
                "bank_account_code": "1002",
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
        "bank_account_code": "1002",
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
                "bank_account_code": "1002",
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
        "bank_account_code": "1002",
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
