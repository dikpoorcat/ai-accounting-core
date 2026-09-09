from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.models import (
    Account,
    BankTransaction,
    DeferredOutputVatTransfer,
    Evidence,
    OpenItem,
    Organization,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import (
    BankTransactionReference,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService


@pytest.fixture(autouse=True)
def event_evidence(session: Session, organization: Organization) -> Evidence:
    evidence = Evidence(
        org_id=organization.id,
        original_name="service-test.txt",
        storage_path="service-test.txt",
        sha256="e" * 64,
        size_bytes=1,
        media_type="text/plain",
        source="test",
    )
    session.add(evidence)
    session.flush()
    return evidence


@pytest.fixture(autouse=True)
def confirmed_bank_scope(session: Session, organization: Organization) -> None:
    account = session.scalar(
        select(Account).where(Account.org_id == organization.id, Account.code == "1002")
    )
    account.requires_bank_reconciliation = True
    account.bank_reconciliation_start_date = date(2000, 1, 1)
    account.bank_reconciliation_configured_at = datetime.now(UTC)
    session.flush()
    set_committed_value(organization, "bank_reconciliation_scope_current_action_id", uuid.uuid4())
    set_committed_value(organization, "bank_reconciliation_scope_confirmed_at", datetime.now(UTC))


def sale_request(
    organization: Organization,
    *,
    event_type: str = "service_cash_sale",
    amount_fen: int = 1_010_000,
    key: str | None = None,
) -> RecordEventRequest:
    session = organization._sa_instance_state.session
    evidence = session.scalar(select(Evidence).where(Evidence.org_id == organization.id))
    assert evidence is not None
    component = {
        "key": "sale",
        "kind": "service_sale",
        "business_date": "2026-08-08",
        "payment_date": "2026-08-08" if event_type == "service_cash_sale" else None,
        "amount_fen": amount_fen,
        "metadata": {"counterparty": {"kind": "customer", "name": "甲客户"}},
        "recognition_basis": "immediate" if event_type == "service_cash_sale" else "credit",
        "fulfillment_date": "2026-08-08",
        "tax_obligation_date": "2026-08-08",
        "tax_facts": {
            "taxable": True,
            "rate_percent": "1",
            "invoice_type": "ordinary",
            "waive_exemption": False,
            "tax_due_on_event": True,
        },
    }
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key or f"sale-{uuid.uuid4()}",
            "posting_date": "2026-08-08",
            "description": "咨询服务",
            "evidence_references": [evidence.id],
            "components": [component],
            "funds": (
                [
                    {
                        "key": "sale-receipt",
                        "account_code": "1002",
                        "direction": "receipt",
                        "payment_date": "2026-08-08",
                        "amount_fen": amount_fen,
                        "allocations": [{"component_key": "sale", "amount_fen": amount_fen}],
                    }
                ]
                if event_type == "service_cash_sale"
                else []
            ),
        }
    )


def voucher_totals(session: Session, voucher_id: uuid.UUID) -> tuple[int, int]:
    lines = session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == voucher_id)).all()
    return sum(line.debit_fen for line in lines), sum(line.credit_fen for line in lines)


def receivable_request(
    organization: Organization, item: OpenItem, *, key: str, amount_fen: int, day: str
) -> RecordEventRequest:
    session = organization._sa_instance_state.session
    evidence = session.scalar(select(Evidence).where(Evidence.org_id == organization.id))
    assert evidence is not None
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "posting_date": day,
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "receivable",
                    "kind": "receivable_settlement",
                    "business_date": day,
                    "payment_date": day,
                    "allocations": [{"open_item_id": item.id, "amount_fen": amount_fen}],
                }
            ],
            "funds": [
                {
                    "key": "receipt",
                    "account_code": "1002",
                    "direction": "receipt",
                    "payment_date": day,
                    "amount_fen": amount_fen,
                    "allocations": [{"component_key": "receivable", "amount_fen": amount_fen}],
                }
            ],
        }
    )


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
    evidence = session.scalar(select(Evidence).where(Evidence.org_id == organization.id))
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "unknown-receipt",
            "posting_date": "2026-08-08",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "advance",
                    "kind": "customer_advance",
                    "business_date": "2026-08-08",
                    "payment_date": "2026-08-08",
                    "amount_fen": 29_849_401,
                    "metadata": {"counterparty": {"kind": "customer", "name": "未知客户"}},
                }
            ],
            "funds": [
                {
                    "key": "receipt",
                    "account_code": "1002",
                    "direction": "receipt",
                    "payment_date": "2026-08-08",
                    "amount_fen": 29_849_401,
                    "allocations": [{"component_key": "advance", "amount_fen": 29_849_401}],
                }
            ],
        }
    )
    result = FinanceService(session).record_event(request)

    assert result.status == "needs_information"
    assert result.voucher_id is None
    assert result.missing_information == ["components.advance.tax_facts"]


def test_credit_sale_partial_settlement_and_oversettlement_rejected(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    sale = service.record_event(
        sale_request(organization, event_type="service_credit_sale", amount_fen=101_000)
    )
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == sale.event_id))
    assert item.original_amount_fen == 101_000

    partial = receivable_request(
        organization, item, key="partial-receipt", amount_fen=40_000, day="2026-08-09"
    )
    assert service.record_event(partial).status == "posted"
    assert item.settled_amount_fen == 40_000
    assert item.status == "partial"

    excessive = receivable_request(
        organization, item, key="excessive-receipt", amount_fen=70_000, day="2026-08-10"
    )
    rejected = service.record_event(excessive)
    assert rejected.status == "rejected"
    assert rejected.errors == ["SETTLEMENT_EXCEEDS_OPEN_BALANCE"]
    assert item.settled_amount_fen == 40_000


def test_credit_sale_defers_vat_and_receipt_transfers_it_on_tax_date(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    sale_request_payload = sale_request(
        organization,
        event_type="service_credit_sale",
        amount_fen=149_400,
        key="march-revenue-deferred-vat",
    ).model_dump(mode="json")
    sale_request_payload["posting_date"] = "2026-03-31"
    sale_request_payload["components"][0] |= {
        "business_date": "2026-03-31",
        "fulfillment_date": "2026-03-31",
        "payment_date": None,
        "tax_obligation_date": "2026-04-02",
    }
    sale_request_payload["components"][0]["tax_facts"]["tax_due_on_event"] = False
    sale = service.record_event(RecordEventRequest.model_validate(sale_request_payload))

    assert sale.status == "posted"
    sale_derived = next(c["derived"] for c in sale.data["components"] if c["key"] == "sale")
    assert sale_derived["vat_fen"] == 1_479, sale_derived
    assert sale_derived["vat_recognition"] == "deferred"
    sale_voucher = session.get(Voucher, sale.voucher_id)
    sale_by_role = {line.account.system_role: line for line in sale_voucher.lines}
    assert sale_by_role["deferred_output_vat"].credit_fen == 1_479
    assert "vat_payable" not in sale_by_role

    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == sale.event_id))
    receipt = service.record_event(
        receivable_request(
            organization,
            item,
            key="april-receipt-transfers-vat",
            amount_fen=149_400,
            day="2026-04-02",
        )
    )

    assert receipt.status == "posted"
    receipt_voucher = session.get(Voucher, receipt.voucher_id)
    receipt_by_role = {line.account.system_role: line for line in receipt_voucher.lines}
    assert receipt_by_role["deferred_output_vat"].debit_fen == 1_479
    assert receipt_by_role["vat_payable"].credit_fen == 1_479
    transfer = session.scalar(
        select(DeferredOutputVatTransfer).where(
            DeferredOutputVatTransfer.transfer_event_id == receipt.event_id
        )
    )
    assert transfer.source_event_id == sale.event_id
    assert transfer.source_open_item_id == item.id
    assert transfer.tax_obligation_date == date(2026, 4, 2)
    assert transfer.amount_fen == 1_479
    assert transfer.accounting_rule_source_url == FinanceService.DEFERRED_OUTPUT_VAT_RULE_SOURCE_URL


def test_credit_sale_same_day_tax_obligation_still_credits_vat_payable(
    session: Session, organization: Organization
) -> None:
    result = FinanceService(session).record_event(
        sale_request(
            organization,
            event_type="service_credit_sale",
            amount_fen=101_000,
            key="same-day-credit-sale-vat",
        )
    )

    voucher = session.get(Voucher, result.voucher_id)
    by_role = {line.account.system_role: line for line in voucher.lines}
    assert by_role["vat_payable"].credit_fen == 1_000
    assert "deferred_output_vat" not in by_role
    derived = next(item["derived"] for item in result.data["components"] if item["key"] == "sale")
    assert derived["vat_recognition"] == "payable"


def test_idempotency_replays_original_result(session: Session, organization: Organization) -> None:
    request = sale_request(organization, key="stable-bank-row-1")
    service = FinanceService(session)
    first = service.record_event(request)
    second = service.record_event(request)

    assert first.event_id == second.event_id
    assert first.voucher_id == second.voucher_id
    assert session.query(Voucher).count() == 1


def test_enabled_period_control_fails_closed_before_first_generation(
    session: Session, organization: Organization
) -> None:
    organization.accounting_period_control_enabled = True
    organization.accounting_period_control_start_date = None
    session.flush()
    result = FinanceService(session).record_event(sale_request(organization))
    assert result.status == "rejected"
    assert result.errors == ["ACCOUNTING_PERIOD_NOT_GENERATED"]


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
    session: Session, organization: Organization, event_evidence: Evidence
) -> None:
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "expense-1",
            "posting_date": "2026-08-08",
            "evidence_references": [event_evidence.id],
            "components": [
                {
                    "key": "expense",
                    "kind": "expense",
                    "business_date": "2026-08-08",
                    "payment_date": "2026-08-08",
                    "amount_fen": 10_300,
                    "expense_class": "general_expense",
                    "payment_basis": "immediate",
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
            ],
            "funds": [
                {
                    "key": "payment",
                    "account_code": "1002",
                    "direction": "payment",
                    "payment_date": "2026-08-08",
                    "amount_fen": 10_300,
                    "allocations": [{"component_key": "expense", "amount_fen": 10_300}],
                }
            ],
        }
    )
    result = FinanceService(session).record_event(request)
    voucher = session.get(Voucher, result.voucher_id)
    assert len(voucher.lines) == 2
    assert next(line for line in voucher.lines if line.account.code == "5602").debit_fen == 10_300


def test_customer_advance_cannot_be_fulfilled_twice(
    session: Session, organization: Organization, event_evidence: Evidence
) -> None:
    service = FinanceService(session)
    advance = service.record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "advance-1",
                "posting_date": "2026-08-01",
                "evidence_references": [event_evidence.id],
                "components": [
                    {
                        "key": "advance",
                        "kind": "customer_advance",
                        "business_date": "2026-08-01",
                        "payment_date": "2026-08-01",
                        "tax_obligation_date": "2026-08-01",
                        "amount_fen": 101_000,
                        "metadata": {"counterparty": {"kind": "customer", "name": "乙客户"}},
                        "tax_facts": {
                            "taxable": True,
                            "rate_percent": "1",
                            "invoice_type": "ordinary",
                            "waive_exemption": False,
                            "tax_due_on_event": True,
                        },
                    }
                ],
                "funds": [
                    {
                        "key": "receipt",
                        "account_code": "1002",
                        "direction": "receipt",
                        "payment_date": "2026-08-01",
                        "amount_fen": 101_000,
                        "allocations": [{"component_key": "advance", "amount_fen": 101_000}],
                    }
                ],
            }
        )
    )
    assert advance.status == "posted"
    source_component_id = next(
        item["id"] for item in advance.data["components"] if item["key"] == "advance"
    )

    def fulfillment(key: str, amount: int) -> RecordEventRequest:
        return RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": key,
                "posting_date": "2026-08-10",
                "evidence_references": [event_evidence.id],
                "components": [
                    {
                        "key": "fulfillment",
                        "kind": "service_fulfillment",
                        "business_date": "2026-08-10",
                        "fulfillment_date": "2026-08-10",
                        "amount_fen": amount,
                        "metadata": {"counterparty": {"kind": "customer", "name": "乙客户"}},
                        "source": {"component_id": source_component_id},
                        "tax_obligation_date": "2026-08-01",
                        "tax_facts": {
                            "taxable": True,
                            "rate_percent": "1",
                            "invoice_type": "ordinary",
                            "waive_exemption": False,
                            "tax_due_on_event": False,
                        },
                    }
                ],
            }
        )

    first = service.record_event(fulfillment("fulfill-1", 101_000))
    assert first.status == "posted"
    voucher = session.get(Voucher, first.voucher_id)
    assert sum(line.debit_fen for line in voucher.lines) == 100_000
    assert sum(line.credit_fen for line in voucher.lines) == 100_000

    duplicate_consumption = service.record_event(fulfillment("fulfill-2", 1))
    assert duplicate_consumption.status == "rejected"
    assert duplicate_consumption.errors == ["COMPONENT_SOURCE_AMOUNT_EXCEEDED"]


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
    request.funds[0].bank_transaction_references.append(BankTransactionReference(id=bank_row.id))
    result = FinanceService(session).record_event(request)
    assert result.status == "rejected"
    assert result.errors == ["FUNDS_BANK_AMOUNT_MISMATCH"]
    session.refresh(bank_row)
    assert bank_row.matched_event_id is None
