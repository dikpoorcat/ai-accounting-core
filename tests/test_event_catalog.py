from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.models import (
    Account,
    BusinessEvent,
    Evidence,
    OpenItem,
    Organization,
    Settlement,
    Voucher,
    VoucherLine,
)
from ai_accounting.service import FinanceService


@pytest.fixture(autouse=True)
def confirmed_bank_scope(session: Session, organization: Organization) -> None:
    configured_at = datetime.now(UTC)
    primary = session.scalar(
        select(Account).where(Account.org_id == organization.id, Account.code == "1002")
    )
    assert primary is not None
    primary.requires_bank_reconciliation = True
    primary.bank_reconciliation_start_date = date(2000, 1, 1)
    primary.bank_reconciliation_configured_at = configured_at
    session.flush()
    set_committed_value(organization, "bank_reconciliation_scope_current_action_id", uuid.uuid4())
    set_committed_value(organization, "bank_reconciliation_scope_confirmed_at", configured_at)


@pytest.fixture
def evidence(session: Session, organization: Organization) -> Evidence:
    item = Evidence(
        org_id=organization.id,
        sha256="c" * 64,
        original_name="component-catalog.txt",
        media_type="text/plain",
        source="test",
        size_bytes=1,
        storage_path="test/component-catalog.txt",
    )
    session.add(item)
    session.flush()
    return item


def request(
    organization: Organization,
    evidence: Evidence,
    *,
    key: str,
    posting_date: date,
    components: list[dict],
    funds: list[dict] | None = None,
) -> RecordEventRequest:
    """Build the public composition protocol, never a legacy event envelope."""

    return RecordEventRequest(
        org_id=organization.id,
        idempotency_key=key,
        posting_date=posting_date,
        evidence_references=[evidence.id],
        components=components,
        funds=funds or [],
    )


def assert_balanced(session: Session, voucher_id: object) -> None:
    lines = session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)).all()
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines) > 0


def payment_funds(key: str, amount_fen: int, *component_keys: tuple[str, int]) -> dict:
    return {
        "key": key,
        "account_code": "1002",
        "direction": "payment",
        "payment_date": "2026-08-02",
        "amount_fen": amount_fen,
        "allocations": [
            {"component_key": component_key, "amount_fen": amount}
            for component_key, amount in component_keys
        ],
    }


def test_payable_purchase_and_supplier_payment_preserve_balance_and_idempotency(
    session: Session, organization: Organization, evidence: Evidence
) -> None:
    service = FinanceService(session)
    supplier = {"kind": "supplier", "name": "甲供应商"}
    purchase = service.record_event(
        request(
            organization,
            evidence,
            key="payable-expense",
            posting_date=date(2026, 8, 1),
            components=[
                {
                    "key": "purchase",
                    "kind": "expense",
                    "business_date": "2026-08-01",
                    "amount_fen": 30_000,
                    "expense_class": "general_expense",
                    "payment_basis": "supplier_credit",
                    "counterparty": supplier,
                }
            ],
        )
    )
    assert purchase.status == "posted", purchase.errors
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == purchase.event_id))
    assert item is not None and item.status == "open"

    payload = request(
        organization,
        evidence,
        key="supplier-payment",
        posting_date=date(2026, 8, 2),
        components=[
            {
                "key": "settlement",
                "kind": "payable_settlement",
                "business_date": "2026-08-02",
                "payment_date": "2026-08-02",
                "counterparty": supplier,
                "allocations": [{"open_item_id": item.id, "amount_fen": 30_000}],
            }
        ],
        funds=[payment_funds("bank", 30_000, ("settlement", 30_000))],
    )
    payment = service.record_event(payload)
    replay = service.record_event(payload)
    assert payment.status == replay.status == "posted"
    assert replay.event_id == payment.event_id
    assert item.status == "settled"
    assert_balanced(session, payment.voucher_id)


def test_multiple_expenses_payable_and_bank_fee_share_one_atomic_voucher(
    session: Session, organization: Organization, evidence: Evidence
) -> None:
    supplier = {"kind": "supplier", "name": "组合供应商"}
    funds = payment_funds("bank", 1_100, ("operations", 1_000), ("fee", 100))
    result = FinanceService(session).record_event(
        request(
            organization,
            evidence,
            key="expense-ap-fee",
            posting_date=date(2026, 8, 2),
            components=[
                {
                    "key": "operations",
                    "kind": "expense",
                    "business_date": "2026-08-02",
                    "payment_date": "2026-08-02",
                    "amount_fen": 1_000,
                    "expense_class": "general_expense",
                    "payment_basis": "immediate",
                },
                {
                    "key": "credit-purchase",
                    "kind": "expense",
                    "business_date": "2026-08-02",
                    "amount_fen": 4_000,
                    "expense_class": "sales_expense",
                    "payment_basis": "supplier_credit",
                    "counterparty": supplier,
                },
                {
                    "key": "fee",
                    "kind": "expense",
                    "business_date": "2026-08-02",
                    "payment_date": "2026-08-02",
                    "amount_fen": 100,
                    "expense_class": "finance_expense",
                    "expense_nature": "bank_service_fee",
                    "payment_basis": "immediate",
                },
            ],
            funds=[funds],
        )
    )
    assert result.status == "posted", (result.errors, result.missing_information)
    assert session.scalar(select(func.count()).select_from(Voucher)) == 1
    assert session.scalar(select(func.count()).select_from(OpenItem)) == 1
    assert_balanced(session, result.voucher_id)


def test_invalid_component_rolls_back_the_entire_composition(
    session: Session, organization: Organization, evidence: Evidence
) -> None:
    before = (
        session.scalar(select(func.count()).select_from(BusinessEvent)),
        session.scalar(select(func.count()).select_from(Voucher)),
    )
    result = FinanceService(session).record_event(
        request(
            organization,
            evidence,
            key="atomic-invalid-expense",
            posting_date=date(2026, 8, 2),
            components=[
                {
                    "key": "valid",
                    "kind": "expense",
                    "business_date": "2026-08-02",
                    "amount_fen": 100,
                    "expense_class": "general_expense",
                    "payment_basis": "immediate",
                },
                {
                    "key": "invalid",
                    "kind": "expense",
                    "business_date": "2026-08-02",
                    "amount_fen": 200,
                    "expense_class": None,
                    "payment_basis": "immediate",
                },
            ],
            funds=[payment_funds("bank", 300, ("valid", 100), ("invalid", 200))],
        )
    )
    assert result.status == "needs_information"
    assert result.missing_information == ["components.invalid.expense_class"]
    after = (
        session.scalar(select(func.count()).select_from(BusinessEvent)),
        session.scalar(select(func.count()).select_from(Voucher)),
    )
    assert after == before


def test_ar_advance_and_multiple_pass_through_creditors_compose_in_one_event(
    session: Session, organization: Organization, evidence: Evidence
) -> None:
    customer = {"kind": "customer", "name": "组合客户"}
    result = FinanceService(session).record_event(
        request(
            organization,
            evidence,
            key="ar-advance-pass-through",
            posting_date=date(2026, 8, 2),
            components=[
                {
                    "key": "ar",
                    "kind": "service_sale",
                    "business_date": "2026-08-02",
                    "fulfillment_date": "2026-08-02",
                    "amount_fen": 10_100,
                    "counterparty": customer,
                    "recognition_basis": "credit",
                    "tax_obligation_date": "2026-08-02",
                    "tax_facts": {
                        "taxable": True,
                        "rate_percent": "1",
                        "invoice_type": "ordinary",
                        "waive_exemption": False,
                        "tax_due_on_event": True,
                    },
                },
                {
                    "key": "advance",
                    "kind": "customer_advance",
                    "business_date": "2026-08-02",
                    "payment_date": "2026-08-02",
                    "amount_fen": 5_000,
                    "counterparty": customer,
                    "tax_facts": {"tax_due_on_event": False},
                },
                {
                    "key": "pass-a",
                    "kind": "pass_through",
                    "business_date": "2026-08-02",
                    "payment_date": "2026-08-02",
                    "amount_fen": 2_000,
                    "beneficiary": {"kind": "other", "name": "受益方甲"},
                    "creditor": {"kind": "other", "name": "受益方甲"},
                    "creditor_basis": "beneficiary",
                    "purpose": "代收甲款项",
                },
                {
                    "key": "pass-b",
                    "kind": "pass_through",
                    "business_date": "2026-08-02",
                    "payment_date": "2026-08-02",
                    "amount_fen": 3_000,
                    "beneficiary": {"kind": "other", "name": "受益方乙"},
                    "creditor": {"kind": "other", "name": "受益方乙"},
                    "creditor_basis": "beneficiary",
                    "purpose": "代收乙款项",
                },
            ],
            funds=[
                {
                    "key": "receipt",
                    "account_code": "1002",
                    "direction": "receipt",
                    "payment_date": "2026-08-02",
                    "amount_fen": 10_000,
                    "allocations": [
                        {"component_key": "advance", "amount_fen": 5_000},
                        {"component_key": "pass-a", "amount_fen": 2_000},
                        {"component_key": "pass-b", "amount_fen": 3_000},
                    ],
                }
            ],
        )
    )
    assert result.status == "posted", (result.errors, result.missing_information)
    items = session.scalars(
        select(OpenItem).where(OpenItem.source_event_id == result.event_id)
    ).all()
    assert sorted(item.item_type for item in items) == ["payable", "payable", "receivable"]
    assert len({item.source_component_id for item in items}) == 3
    assert_balanced(session, result.voucher_id)


def test_person_debt_transfer_and_cash_settlement_keep_source_lineage(
    session: Session, organization: Organization, evidence: Evidence
) -> None:
    supplier = {"kind": "supplier", "name": "垫付供应商"}
    employee = {"kind": "employee", "name": "垫付员工"}
    service = FinanceService(session)
    source = service.record_event(
        request(
            organization,
            evidence,
            key="person-transfer-sources",
            posting_date=date(2026, 8, 1),
            components=[
                {
                    "key": key,
                    "kind": "expense",
                    "business_date": "2026-08-01",
                    "amount_fen": amount,
                    "expense_class": "general_expense",
                    "payment_basis": "supplier_credit",
                    "counterparty": supplier,
                }
                for key, amount in (("claim-a", 25_000), ("claim-b", 35_000))
            ],
        )
    )
    claims = session.scalars(
        select(OpenItem)
        .where(OpenItem.source_event_id == source.event_id)
        .order_by(OpenItem.original_amount_fen)
    ).all()
    transferred = service.record_event(
        request(
            organization,
            evidence,
            key="person-debt-transfer",
            posting_date=date(2026, 8, 2),
            components=[
                {
                    "key": "transfer",
                    "kind": "debt_transfer",
                    "business_date": "2026-08-02",
                    "payment_date": "2026-08-02",
                    "payer": employee,
                    "allocations": [
                        {"open_item_id": item.id, "amount_fen": item.original_amount_fen}
                        for item in claims
                    ],
                }
            ],
        )
    )
    assert transferred.status == "posted", transferred.errors
    person_item = session.scalar(
        select(OpenItem).where(OpenItem.source_event_id == transferred.event_id)
    )
    assert person_item is not None and person_item.original_amount_fen == 60_000
    paid = service.record_event(
        request(
            organization,
            evidence,
            key="person-cash-settlement",
            posting_date=date(2026, 8, 2),
            components=[
                {
                    "key": "settlement",
                    "kind": "payable_settlement",
                    "business_date": "2026-08-02",
                    "payment_date": "2026-08-02",
                    "counterparty": employee,
                    "allocations": [{"open_item_id": person_item.id, "amount_fen": 60_000}],
                }
            ],
            funds=[
                {
                    **payment_funds("cash", 60_000, ("settlement", 60_000)),
                    "account_code": "1001",
                }
            ],
        )
    )
    assert paid.status == "posted", paid.errors
    assert all(item.status == "settled" for item in [*claims, person_item])
    assert (
        session.scalar(
            select(func.count())
            .select_from(Settlement)
            .where(Settlement.payment_event_id == transferred.event_id)
        )
        == 2
    )
    assert_balanced(session, paid.voucher_id)


def test_advance_refund_cannot_exceed_unused_source_component(
    session: Session, organization: Organization, evidence: Evidence
) -> None:
    service = FinanceService(session)
    customer = {"kind": "customer", "name": "退款客户"}
    advance = service.record_event(
        request(
            organization,
            evidence,
            key="advance-refund-source",
            posting_date=date(2026, 8, 1),
            components=[
                {
                    "key": "advance",
                    "kind": "customer_advance",
                    "business_date": "2026-08-01",
                    "payment_date": "2026-08-01",
                    "amount_fen": 50_000,
                    "counterparty": customer,
                    "tax_facts": {"tax_due_on_event": False},
                }
            ],
            funds=[
                {
                    "key": "receipt",
                    "account_code": "1002",
                    "direction": "receipt",
                    "payment_date": "2026-08-01",
                    "amount_fen": 50_000,
                    "allocations": [{"component_key": "advance", "amount_fen": 50_000}],
                }
            ],
        )
    )
    source_component_id = next(
        row["id"] for row in advance.data["components"] if row["kind"] == "customer_advance"
    )

    def refund(key: str, amount: int):
        return service.record_event(
            request(
                organization,
                evidence,
                key=key,
                posting_date=date(2026, 8, 2),
                components=[
                    {
                        "key": "refund",
                        "kind": "customer_refund",
                        "business_date": "2026-08-02",
                        "payment_date": "2026-08-02",
                        "amount_fen": amount,
                        "counterparty": customer,
                        "source": {"component_id": source_component_id},
                        "refund_kind": "advance",
                    }
                ],
                funds=[payment_funds("payment", amount, ("refund", amount))],
            )
        )

    partial = refund("advance-refund-partial", 30_000)
    excess = refund("advance-refund-excess", 30_000)
    assert partial.status == "posted"
    assert excess.status == "rejected"
    assert excess.errors == ["COMPONENT_SOURCE_AMOUNT_EXCEEDED"]
