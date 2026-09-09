from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.component_schemas import (
    COMPONENT_TYPES,
    FundsSettlement,
    RecordEventRequest,
)
from ai_accounting.models import (
    Account,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Evidence,
    Organization,
    Voucher,
    VoucherLine,
)
from ai_accounting.service import FinanceService


def _confirm_scope(session: Session, organization: Organization, *additional_codes: str) -> None:
    configured_at = datetime.now(UTC)
    primary = session.scalar(
        select(Account).where(Account.org_id == organization.id, Account.code == "1002")
    )
    assert primary is not None
    primary.requires_bank_reconciliation = True
    primary.bank_reconciliation_start_date = date(2020, 1, 1)
    primary.bank_reconciliation_configured_at = configured_at
    for code in additional_codes:
        session.add(
            Account(
                org_id=organization.id,
                code=code,
                name=f"测试银行 {code}",
                category="asset",
                normal_side="debit",
                active=True,
                business_class="bank",
                requires_bank_reconciliation=True,
                bank_reconciliation_start_date=date(2020, 1, 1),
                bank_reconciliation_configured_at=configured_at,
            )
        )
    session.flush()
    set_committed_value(organization, "bank_reconciliation_scope_current_action_id", uuid.uuid4())
    set_committed_value(organization, "bank_reconciliation_scope_confirmed_at", configured_at)


@pytest.fixture
def evidence(session: Session, organization: Organization) -> Evidence:
    row = Evidence(
        org_id=organization.id,
        sha256="b" * 64,
        original_name="funds-selection.txt",
        media_type="text/plain",
        source="test",
        size_bytes=1,
        storage_path="test/funds-selection.txt",
    )
    session.add(row)
    session.flush()
    return row


def _cash_sale(
    organization: Organization,
    evidence: Evidence,
    *,
    key: str,
    account_code: str,
    references: list[dict[str, object]] | None = None,
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "posting_date": "2026-08-08",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "sale",
                    "kind": "service_sale",
                    "business_date": "2026-08-08",
                    "fulfillment_date": "2026-08-08",
                    "payment_date": "2026-08-08",
                    "amount_fen": 100,
                    "recognition_basis": "immediate",
                    "tax_facts": {
                        "taxable": False,
                        "rate_percent": "0",
                        "invoice_type": "none",
                        "waive_exemption": False,
                        "tax_due_on_event": False,
                    },
                    "metadata": {"counterparty": {"kind": "customer", "name": "资金账户客户"}},
                }
            ],
            "funds": [
                {
                    "key": "receipt",
                    "account_code": account_code,
                    "direction": "receipt",
                    "payment_date": "2026-08-08",
                    "amount_fen": 100,
                    "allocations": [{"component_key": "sale", "amount_fen": 100}],
                    "bank_transaction_references": references or [],
                }
            ],
        }
    )


def _voucher_lines(session: Session, voucher_id: uuid.UUID) -> list[tuple[str, int, int]]:
    rows = session.execute(
        select(Account.code, VoucherLine.debit_fen, VoucherLine.credit_fen)
        .join(VoucherLine, VoucherLine.account_id == Account.id)
        .where(VoucherLine.voucher_id == voucher_id)
        .order_by(VoucherLine.line_number)
    ).all()
    assert sum(row.debit_fen for row in rows) == sum(row.credit_fen for row in rows)
    return [(row.code, row.debit_fen, row.credit_fen) for row in rows]


def test_public_composition_schema_makes_funds_account_selection_explicit() -> None:
    request_schema = RecordEventRequest.model_json_schema()
    funds_schema = FundsSettlement.model_json_schema()
    assert set(funds_schema["required"]) >= {
        "key",
        "account_code",
        "direction",
        "payment_date",
        "amount_fen",
        "allocations",
    }
    assert "bank_transaction_references" in funds_schema["properties"]
    assert "bank_transaction_references" not in funds_schema["required"]
    assert "bank_account_code" not in request_schema["properties"]
    assert "bank_transaction_references" not in request_schema["properties"]
    assert "funds_transfer" in COMPONENT_TYPES


def test_funds_allocations_must_equal_the_exact_real_movement() -> None:
    with pytest.raises(ValidationError, match="funds allocations must equal"):
        FundsSettlement.model_validate(
            {
                "key": "bad",
                "account_code": "1002",
                "direction": "payment",
                "payment_date": "2026-08-08",
                "amount_fen": 100,
                "allocations": [{"component_key": "expense", "amount_fen": 99}],
            }
        )


def test_unconfirmed_scope_does_not_block_an_explicit_valid_bank_account(
    session: Session, organization: Organization, evidence: Evidence
) -> None:
    set_committed_value(organization, "bank_reconciliation_scope_current_action_id", None)
    set_committed_value(organization, "bank_reconciliation_scope_confirmed_at", None)
    result = FinanceService(session).record_event(
        _cash_sale(organization, evidence, key="scope-required", account_code="1002")
    )
    assert result.status == "posted", result
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 1
    assert session.scalar(select(func.count()).select_from(Voucher)) == 1


def test_selected_bank_account_is_frozen_by_idempotent_composition(
    session: Session, organization: Organization, evidence: Evidence
) -> None:
    _confirm_scope(session, organization, "1003")
    service = FinanceService(session)
    payload = _cash_sale(organization, evidence, key="selected-second-account", account_code="1003")
    posted = service.record_event(payload)
    replay = service.record_event(payload)
    assert posted.status == "posted"
    assert replay.event_id == posted.event_id
    assert ("1003", 100, 0) in _voucher_lines(session, posted.voucher_id)
    changed = payload.model_copy(deep=True)
    changed.funds[0].account_code = "1002"
    assert service.record_event(changed).errors == ["IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"]


def test_uncontrolled_bank_reference_is_rejected_without_matching(
    session: Session, organization: Organization, evidence: Evidence
) -> None:
    _confirm_scope(session, organization)
    row = BankTransaction(
        org_id=organization.id,
        bank_account_code="1002",
        fingerprint="f" * 64,
        booking_date=date(2026, 8, 8),
        amount_fen=100,
        currency="CNY",
        memo="uncontrolled",
        source_sha256="a" * 64,
    )
    session.add(row)
    session.flush()
    result = FinanceService(session).record_event(
        _cash_sale(
            organization,
            evidence,
            key="uncontrolled-bank-row",
            account_code="1002",
            references=[{"id": row.id}],
        )
    )
    assert result.errors == ["BANK_TRANSACTION_REQUIRES_CONTROLLED_IMPORT_ACTION"]
    assert row.matched_event_id is None
    assert session.scalar(select(func.count()).select_from(BankTransactionMatch)) == 0


def test_internal_transfer_requires_equal_explicit_payment_and_receipt_funds(
    session: Session, organization: Organization, evidence: Evidence
) -> None:
    _confirm_scope(session, organization, "1003")
    payload = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "bank-to-bank-transfer",
            "posting_date": "2026-08-08",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "transfer",
                    "kind": "funds_transfer",
                    "business_date": "2026-08-08",
                    "amount_fen": 100,
                }
            ],
            "funds": [
                {
                    "key": "out",
                    "account_code": "1002",
                    "direction": "payment",
                    "payment_date": "2026-08-08",
                    "amount_fen": 100,
                    "allocations": [{"component_key": "transfer", "amount_fen": 100}],
                },
                {
                    "key": "in",
                    "account_code": "1003",
                    "direction": "receipt",
                    "payment_date": "2026-08-08",
                    "amount_fen": 100,
                    "allocations": [{"component_key": "transfer", "amount_fen": 100}],
                },
            ],
        }
    )
    posted = FinanceService(session).record_event(payload)
    assert posted.status == "posted", posted.errors
    assert _voucher_lines(session, posted.voucher_id) == [
        ("1002", 0, 100),
        ("1003", 100, 0),
    ]
