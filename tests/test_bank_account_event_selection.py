from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.models import (
    Account,
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Counterparty,
    Evidence,
    OpenItem,
    Organization,
    Settlement,
    Voucher,
    VoucherLine,
    event_evidence,
)
from ai_accounting.schemas import (
    EVENT_REQUIREMENTS,
    EventType,
    RecordEventRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService


def _confirm_scope(
    session: Session,
    organization: Organization,
    *additional_codes: str,
) -> None:
    configured_at = datetime.now(UTC)
    primary = session.scalar(
        select(Account).where(Account.org_id == organization.id, Account.code == "1002")
    )
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
                requires_bank_reconciliation=True,
                bank_reconciliation_start_date=date(2020, 1, 1),
                bank_reconciliation_configured_at=configured_at,
            )
        )
    session.flush()
    # Unit tests exercise the event boundary without rebuilding the owner-session
    # graph required by the immutable scope action.  Mark these two values as
    # already-loaded database facts so no synthetic scope row is persisted.
    set_committed_value(organization, "bank_reconciliation_scope_current_action_id", uuid.uuid4())
    set_committed_value(organization, "bank_reconciliation_scope_confirmed_at", configured_at)


def _bank_row(
    session: Session,
    organization: Organization,
    *,
    account_code: str,
    amount_fen: int,
    seed: str,
    fingerprint: str | None = None,
) -> BankTransaction:
    row = BankTransaction(
        org_id=organization.id,
        bank_account_code=account_code,
        fingerprint=fingerprint or (seed * 64)[:64],
        booking_date=date(2026, 8, 8),
        amount_fen=amount_fen,
        currency="CNY",
        memo=seed,
        source_sha256=(f"source-{seed}" * 64)[:64],
    )
    session.add(row)
    session.flush()
    return row


def _cash_sale(
    organization: Organization,
    *,
    key: str,
    bank_account_code: str | None,
    references: list[dict[str, object]] | None = None,
) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "event_type": "service_cash_sale",
            "business_dates": {
                "business_date": "2026-08-08",
                "posting_date": "2026-08-08",
                "fulfillment_date": "2026-08-08",
                "payment_date": "2026-08-08",
            },
            "amounts": {"gross_amount_fen": 100},
            "tax_facts": {
                "taxable": False,
                "rate_percent": "0",
                "invoice_type": "none",
                "waive_exemption": False,
                "tax_due_on_event": False,
            },
            "bank_account_code": bank_account_code,
            "bank_transaction_references": references or [],
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


def test_public_event_requirements_publish_explicit_bank_selection_and_late_evidence() -> None:
    unconditional_bank_events = {
        EventType.SERVICE_CASH_SALE,
        EventType.CUSTOMER_RECEIPT,
        EventType.CUSTOMER_ADVANCE,
        EventType.CUSTOMER_REFUND,
        EventType.EXPENSE_CASH,
        EventType.SUPPLIER_PAYMENT,
        EventType.OWNER_LOAN_RECEIVED,
        EventType.OWNER_CONTRIBUTION_RECEIVED,
        EventType.OWNER_REPAYMENT,
        EventType.OTHER_INCOME_RECEIVED,
        EventType.BANK_INTEREST_RECEIVED,
        EventType.REFUNDABLE_DEPOSIT_PAID,
        EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED,
        EventType.BANK_FEE,
        EventType.TAX_PAYMENT,
        EventType.SOCIAL_INSURANCE_PAYMENT,
        EventType.HOUSING_FUND_PAYMENT,
        EventType.INDIVIDUAL_INCOME_TAX_PAYMENT,
        EventType.BORROWING_DRAWDOWN,
        EventType.BORROWING_INTEREST_PAYMENT,
        EventType.BORROWING_PRINCIPAL_REPAYMENT,
    }
    conditional_bank_events = {
        EventType.EMPLOYEE_REIMBURSEMENT,
        EventType.SALARY_PAYMENT,
        EventType.FIXED_ASSET_ACQUISITION,
        EventType.FIXED_ASSET_DISPOSAL,
        EventType.INTANGIBLE_ASSET_ACQUISITION,
    }
    special_bank_events = {EventType.INTERNAL_TRANSFER, EventType.CASH_BANK_TRANSFER}
    required_bank_match_events = {
        EventType.OTHER_INCOME_RECEIVED,
        EventType.BANK_INTEREST_RECEIVED,
        EventType.REFUNDABLE_DEPOSIT_PAID,
        EventType.REFUNDABLE_DEPOSIT_RETURN_RECEIVED,
    }

    for event_type in unconditional_bank_events:
        requirements = EVENT_REQUIREMENTS[event_type.value]
        assert "bank_account_code" in requirements["required_fields"]
    for event_type in conditional_bank_events:
        requirements = EVENT_REQUIREMENTS[event_type.value]
        assert any(
            "bank_account_code" in fields
            for fields in requirements["conditional_required_fields"].values()
        )
    for event_type in (
        unconditional_bank_events
        | conditional_bank_events
        | special_bank_events
    ) - required_bank_match_events:
        references = EVENT_REQUIREMENTS[event_type.value]["bank_transaction_references"]
        assert references.startswith("optional;")
        assert "provided" in references
    for event_type in required_bank_match_events:
        references = EVENT_REQUIREMENTS[event_type.value]["bank_transaction_references"]
        assert references.startswith("required;")
        assert "exactly match" in references

    assert EVENT_REQUIREMENTS[EventType.INTERNAL_TRANSFER.value]["required_fields"] == [
        "source_bank_account_code",
        "destination_bank_account_code",
    ]
    assert EVENT_REQUIREMENTS[EventType.CASH_BANK_TRANSFER.value]["required_fields"] == [
        "direction",
        "bank_account_code",
    ]
    assert not any(
        "bank_transactions" in requirements for requirements in EVENT_REQUIREMENTS.values()
    )


def test_unconfirmed_scope_returns_needs_information_without_any_write(
    session: Session, organization: Organization
) -> None:
    set_committed_value(
        organization,
        "bank_reconciliation_scope_current_action_id",
        None,
    )
    set_committed_value(
        organization,
        "bank_reconciliation_scope_confirmed_at",
        None,
    )
    result = FinanceService(session).record_event(
        _cash_sale(organization, key="scope-required", bank_account_code="1002")
    )

    assert result.status == "needs_information"
    assert result.event_id is None
    assert result.missing_information == ["bank_reconciliation_scope_confirmation"]
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0
    assert session.scalar(select(func.count()).select_from(Voucher)) == 0
    assert session.scalar(select(func.count()).select_from(Counterparty)) == 0
    assert session.scalar(select(func.count()).select_from(BankTransactionMatch)) == 0


def test_retained_verification_payment_requires_and_freezes_exact_sources(
    session: Session, organization: Organization
) -> None:
    _confirm_scope(session, organization)
    service = FinanceService(session)
    incomplete = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "verification-income-incomplete",
            "event_type": "other_income_received",
            "business_dates": {
                "business_date": "2026-08-08",
                "posting_date": "2026-08-08",
                "payment_date": "2026-08-08",
            },
            "counterparty": {"kind": "other", "name": "验证平台"},
            "amounts": {"amount_fen": 1},
            "bank_account_code": "1002",
        }
    )
    needs_information = service.record_event(incomplete)
    assert needs_information.status == "needs_information"
    assert needs_information.missing_information == [
        "details.other_income_kind='retained_verification_payment'",
        "bank_transaction_references",
        "evidence_references",
        "description",
    ]

    bank = _bank_row(
        session,
        organization,
        account_code="1002",
        amount_fen=1,
        seed="retained-verification-payment",
    )
    evidence = Evidence(
        org_id=organization.id,
        sha256="e" * 64,
        original_name="verification.txt",
        media_type="text/plain",
        source="test",
        size_bytes=1,
        storage_path="test/verification.txt",
    )
    session.add(evidence)
    session.flush()
    wrong_kind = RecordEventRequest.model_validate(
        incomplete.model_dump(mode="python")
        | {
            "idempotency_key": "verification-income-wrong-counterparty-kind",
            "counterparty": {"kind": "supplier", "name": "测试供应方"},
            "bank_transaction_references": [{"id": bank.id}],
            "evidence_references": [evidence.id],
            "description": "无需退回且未抵扣的测试验证款",
            "details": {"other_income_kind": "retained_verification_payment"},
        }
    )
    rejected_kind = service.record_event(wrong_kind)
    assert rejected_kind.status == "rejected"
    assert rejected_kind.errors == ["other income requires an other counterparty"]
    assert bank.matched_event_id is None

    complete = RecordEventRequest.model_validate(
        incomplete.model_dump(mode="python")
        | {
            "idempotency_key": "verification-income-complete",
            "bank_transaction_references": [{"id": bank.id}],
            "evidence_references": [evidence.id],
            "description": "无需退回且未抵扣的商户验证款",
            "details": {"other_income_kind": "retained_verification_payment"},
        }
    )
    posted = service.record_event(complete)

    assert posted.status == "posted", posted.errors
    assert _voucher_lines(session, posted.voucher_id) == [
        ("1002", 1, 0),
        ("6301", 0, 1),
    ]
    assert posted.data["derived"] == {
        "other_income_kind": "retained_verification_payment",
        "non_operating_income_fen": 1,
    }
    assert bank.matched_event_id == posted.event_id
    assert session.scalar(
        select(func.count())
        .select_from(BankTransactionMatch)
        .where(BankTransactionMatch.event_id == posted.event_id)
    ) == 1
    assert session.scalar(
        select(func.count())
        .select_from(event_evidence)
        .where(event_evidence.c.event_id == posted.event_id)
    ) == 1


def test_bank_interest_received_credits_finance_expense_and_requires_bank_evidence(
    session: Session, organization: Organization
) -> None:
    _confirm_scope(session, organization)
    service = FinanceService(session)
    base = {
        "org_id": organization.id,
        "idempotency_key": "bank-interest-incomplete",
        "event_type": "bank_interest_received",
        "business_dates": {
            "business_date": "2026-08-08",
            "posting_date": "2026-08-08",
            "payment_date": "2026-08-08",
        },
        "amounts": {"amount_fen": 275},
        "bank_account_code": "1002",
    }
    incomplete = service.record_event(RecordEventRequest.model_validate(base))
    assert incomplete.status == "needs_information"
    assert incomplete.missing_information == [
        "bank_transaction_references",
        "evidence_references",
        "description",
    ]

    bank = _bank_row(
        session,
        organization,
        account_code="1002",
        amount_fen=275,
        seed="bank-interest-received",
    )
    evidence = Evidence(
        org_id=organization.id,
        sha256="i" * 64,
        original_name="bank-statement.xls",
        media_type="application/vnd.ms-excel",
        source="test",
        size_bytes=1,
        storage_path="test/bank-statement.xls",
    )
    session.add(evidence)
    session.flush()
    complete = RecordEventRequest.model_validate(
        base
        | {
            "idempotency_key": "bank-interest-complete",
            "bank_transaction_references": [{"id": bank.id}],
            "evidence_references": [evidence.id],
            "description": "银行结息",
        }
    )
    posted = service.record_event(complete)

    assert posted.status == "posted", posted.errors
    assert _voucher_lines(session, posted.voucher_id) == [
        ("1002", 275, 0),
        ("5603", 0, 275),
    ]
    assert posted.data["derived"] == {"bank_interest_income_fen": 275}
    assert bank.matched_event_id == posted.event_id
    assert session.scalar(
        select(func.count())
        .select_from(event_evidence)
        .where(event_evidence.c.event_id == posted.event_id)
    ) == 1


def test_refundable_deposit_payment_and_return_keep_only_the_real_balance_open(
    session: Session, organization: Organization
) -> None:
    _confirm_scope(session, organization)
    service = FinanceService(session)
    incomplete = service.record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "deposit-incomplete",
                "event_type": "refundable_deposit_paid",
                "business_dates": {
                    "business_date": "2026-08-08",
                    "posting_date": "2026-08-08",
                    "payment_date": "2026-08-08",
                },
                "amounts": {"amount_fen": 2_000_000},
                "bank_account_code": "1002",
            }
        )
    )
    assert incomplete.status == "needs_information"
    assert incomplete.missing_information == [
        "counterparty",
        "bank_transaction_references",
        "evidence_references",
        "description",
    ]

    evidence = Evidence(
        org_id=organization.id,
        sha256="d" * 64,
        original_name="deposit-confirmation.txt",
        media_type="text/plain",
        source="test",
        size_bytes=1,
        storage_path="test/deposit-confirmation.txt",
    )
    session.add(evidence)
    session.flush()
    wrong_payment_bank = _bank_row(
        session,
        organization,
        account_code="1002",
        amount_fen=-2_000_000,
        seed="wrong-deposit-payment",
    )
    correct_payment_bank = _bank_row(
        session,
        organization,
        account_code="1002",
        amount_fen=-2_000_000,
        seed="correct-deposit-payment",
    )
    return_bank = _bank_row(
        session,
        organization,
        account_code="1002",
        amount_fen=2_000_000,
        seed="wrong-deposit-return",
    )

    def post_payment(*, key: str, name: str, bank: BankTransaction):
        return service.record_event(
            RecordEventRequest.model_validate(
                {
                    "org_id": organization.id,
                    "idempotency_key": key,
                    "event_type": "refundable_deposit_paid",
                    "business_dates": {
                        "business_date": "2026-08-08",
                        "posting_date": "2026-08-08",
                        "payment_date": "2026-08-08",
                    },
                    "counterparty": {"kind": "supplier", "name": name},
                    "amounts": {"amount_fen": 2_000_000},
                    "bank_account_code": "1002",
                    "bank_transaction_references": [{"id": bank.id}],
                    "evidence_references": [evidence.id],
                    "description": "支付可退保证金",
                }
            )
        )

    wrong_payment = post_payment(
        key="wrong-deposit-payment",
        name="错误收款主体",
        bank=wrong_payment_bank,
    )
    correct_payment = post_payment(
        key="correct-deposit-payment",
        name="正确收款主体",
        bank=correct_payment_bank,
    )
    assert wrong_payment.status == "posted", wrong_payment.errors
    assert correct_payment.status == "posted", correct_payment.errors
    assert _voucher_lines(session, wrong_payment.voucher_id) == [
        ("1221", 2_000_000, 0),
        ("1002", 0, 2_000_000),
    ]
    wrong_item = session.scalar(
        select(OpenItem).where(OpenItem.source_event_id == wrong_payment.event_id)
    )
    correct_item = session.scalar(
        select(OpenItem).where(OpenItem.source_event_id == correct_payment.event_id)
    )
    assert wrong_item is not None and correct_item is not None

    returned = service.record_event(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "wrong-deposit-return",
                "event_type": "refundable_deposit_return_received",
                "business_dates": {
                    "business_date": "2026-08-08",
                    "posting_date": "2026-08-08",
                    "payment_date": "2026-08-08",
                },
                "counterparty": {"kind": "supplier", "name": "错误收款主体"},
                "amounts": {"amount_fen": 2_000_000},
                "bank_account_code": "1002",
                "bank_transaction_references": [{"id": return_bank.id}],
                "evidence_references": [evidence.id],
                "allocations": [
                    {"open_item_id": wrong_item.id, "amount_fen": 2_000_000}
                ],
                "description": "收回误付的可退保证金",
            }
        )
    )
    assert returned.status == "posted", returned.errors
    assert _voucher_lines(session, returned.voucher_id) == [
        ("1002", 2_000_000, 0),
        ("1221", 0, 2_000_000),
    ]
    session.refresh(wrong_item)
    session.refresh(correct_item)
    assert (wrong_item.settled_amount_fen, wrong_item.status) == (2_000_000, "settled")
    assert (correct_item.settled_amount_fen, correct_item.status) == (0, "open")
    assert session.scalar(
        select(func.count())
        .select_from(Settlement)
        .where(Settlement.payment_event_id == returned.event_id)
    ) == 1


def test_general_bank_events_freeze_selected_account_and_reject_cross_account_refs(
    session: Session, organization: Organization
) -> None:
    _confirm_scope(session, organization, "1003")
    service = FinanceService(session)

    missing = service.record_event(
        _cash_sale(organization, key="bank-account-missing", bank_account_code=None)
    )
    assert missing.status == "needs_information"
    assert missing.missing_information == ["bank_account_code"]

    sale_request = _cash_sale(organization, key="selected-second-account", bank_account_code="1003")
    posted = service.record_event(sale_request)
    assert posted.status == "posted"
    assert _voucher_lines(session, posted.voucher_id)[0] == ("1003", 100, 0)
    assert service.record_event(sale_request).event_id == posted.event_id
    changed = service.record_event(sale_request.model_copy(update={"bank_account_code": "1002"}))
    assert changed.errors == ["IDEMPOTENCY_PAYLOAD_MISMATCH"]

    wrong_row = _bank_row(
        session,
        organization,
        account_code="1002",
        amount_fen=-100,
        seed="wrong-outflow-account",
    )
    outflow = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "bank-fee-cross-account",
            "event_type": "bank_fee",
            "business_dates": {
                "business_date": "2026-08-08",
                "posting_date": "2026-08-08",
                "payment_date": "2026-08-08",
            },
            "amounts": {"amount_fen": 100},
            "bank_account_code": "1003",
            "bank_transaction_references": [{"id": wrong_row.id}],
        }
    )
    rejected = service.record_event(outflow)
    assert rejected.errors == ["BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH"]
    assert wrong_row.matched_event_id is None

    expense_code = service.record_event(
        _cash_sale(organization, key="not-a-bank-account", bank_account_code="5602")
    )
    assert expense_code.errors == ["BANK_ACCOUNT_NOT_CONFIRMED_FOR_RECONCILIATION"]


def test_internal_transfer_accepts_multiple_rows_per_side_and_reversal_invalidates_matches(
    session: Session, organization: Organization
) -> None:
    _confirm_scope(session, organization, "1003")
    evidence = Evidence(
        org_id=organization.id,
        sha256="e" * 64,
        original_name="transfer.pdf",
        media_type="application/pdf",
        source="test",
        size_bytes=1,
        storage_path="test/transfer.pdf",
    )
    session.add(evidence)
    source_rows = [
        _bank_row(
            session,
            organization,
            account_code="1002",
            amount_fen=amount,
            seed=f"source-{index}",
        )
        for index, amount in enumerate((-60, -40), start=1)
    ]
    destination_rows = [
        _bank_row(
            session,
            organization,
            account_code="1003",
            amount_fen=amount,
            seed=f"destination-{index}",
        )
        for index, amount in enumerate((70, 30), start=1)
    ]
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "multi-row-internal-transfer",
            "event_type": "internal_transfer",
            "business_dates": {
                "business_date": "2026-08-08",
                "posting_date": "2026-08-08",
            },
            "amounts": {"amount_fen": 100},
            "source_bank_account_code": "1002",
            "destination_bank_account_code": "1003",
            "bank_transaction_references": [
                {"id": row.id} for row in source_rows + destination_rows
            ],
            "evidence_references": [evidence.id],
        }
    )
    service = FinanceService(session)
    posted = service.record_event(request)

    assert posted.status == "posted", posted.errors
    assert _voucher_lines(session, posted.voucher_id) == [
        ("1003", 100, 0),
        ("1002", 0, 100),
    ]
    assert service.record_event(request).event_id == posted.event_id

    reversed_result = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=posted.event_id,
            idempotency_key="reverse-multi-row-transfer",
            reason="测试账户互转冲正",
            posting_date=date(2026, 8, 9),
        )
    )
    assert reversed_result.status == "posted"
    matches = session.scalars(
        select(BankTransactionMatch).where(BankTransactionMatch.event_id == posted.event_id)
    ).all()
    assert len(matches) == 4
    assert {row.invalidated_by_event_id for row in matches} == {reversed_result.event_id}
    assert all(row.matched_event_id is None for row in source_rows + destination_rows)
    inherited = session.scalars(
        select(event_evidence.c.evidence_id).where(
            event_evidence.c.event_id == reversed_result.event_id,
            event_evidence.c.relation_kind == "inherited",
        )
    ).all()
    assert inherited == [evidence.id]


def test_ambiguous_fingerprint_requires_id_even_with_selected_account(
    session: Session, organization: Organization
) -> None:
    _confirm_scope(session, organization, "1003")
    fingerprint = "f" * 64
    first = _bank_row(
        session,
        organization,
        account_code="1002",
        amount_fen=100,
        seed="ambiguous-first",
        fingerprint=fingerprint,
    )
    _bank_row(
        session,
        organization,
        account_code="1003",
        amount_fen=100,
        seed="ambiguous-second",
        fingerprint=fingerprint,
    )
    service = FinanceService(session)

    ambiguous = service.record_event(
        _cash_sale(
            organization,
            key="ambiguous-fingerprint",
            bank_account_code="1002",
            references=[{"fingerprint": fingerprint}],
        )
    )
    assert ambiguous.errors == ["BANK_TRANSACTION_FINGERPRINT_AMBIGUOUS_USE_ID"]
    resolved = service.record_event(
        _cash_sale(
            organization,
            key="resolved-by-id",
            bank_account_code="1002",
            references=[{"id": first.id}],
        )
    )
    assert resolved.status == "posted", resolved.errors


def test_cash_bank_transfer_has_fixed_cash_side_and_period_guard(
    session: Session, organization: Organization
) -> None:
    _confirm_scope(session, organization)
    service = FinanceService(session)
    deposit = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "cash-deposit",
            "event_type": "cash_bank_transfer",
            "business_dates": {
                "business_date": "2026-08-08",
                "posting_date": "2026-08-08",
            },
            "amounts": {"amount_fen": 100},
            "direction": "cash_deposit",
            "bank_account_code": "1002",
        }
    )
    posted = service.record_event(deposit)
    assert posted.status == "posted"
    assert _voucher_lines(session, posted.voucher_id) == [
        ("1002", 100, 0),
        ("1001", 0, 100),
    ]
    assert service.record_event(deposit).event_id == posted.event_id

    withdrawal_rows = [
        _bank_row(
            session,
            organization,
            account_code="1002",
            amount_fen=amount_fen,
            seed=seed,
        )
        for amount_fen, seed in ((-60, "cash-withdrawal-a"), (-40, "cash-withdrawal-b"))
    ]
    withdrawal = RecordEventRequest.model_validate(
        deposit.model_dump(mode="python")
        | {
            "idempotency_key": "cash-withdrawal",
            "direction": "cash_withdrawal",
            "bank_transaction_references": [{"id": row.id} for row in withdrawal_rows],
        }
    )
    withdrawn = service.record_event(withdrawal)
    assert withdrawn.status == "posted", withdrawn.errors
    assert _voucher_lines(session, withdrawn.voucher_id) == [
        ("1001", 100, 0),
        ("1002", 0, 100),
    ]
    assert service.record_event(withdrawal).event_id == withdrawn.event_id
    assert all(row.matched_event_id == withdrawn.event_id for row in withdrawal_rows)

    wrong_direction_row = _bank_row(
        session,
        organization,
        account_code="1002",
        amount_fen=100,
        seed="cash-withdrawal-wrong-direction",
    )
    wrong_direction = service.record_event(
        RecordEventRequest.model_validate(
            withdrawal.model_dump(mode="python")
            | {
                "idempotency_key": "cash-withdrawal-wrong-direction",
                "bank_transaction_references": [{"id": wrong_direction_row.id}],
            }
        )
    )
    assert wrong_direction.errors == ["CASH_BANK_TRANSFER_BANK_TRANSACTION_AMOUNT_MISMATCH"]
    assert wrong_direction_row.matched_event_id is None

    missing_direction = RecordEventRequest.model_validate(
        deposit.model_dump(mode="python")
        | {"idempotency_key": "cash-direction-missing", "direction": None}
    )
    missing = service.record_event(missing_direction)
    assert missing.status == "needs_information"
    assert missing.missing_information == ["direction"]

    organization.accounting_period_control_enabled = True
    organization.accounting_period_control_start_date = None
    period_blocked = service.record_event(
        deposit.model_copy(update={"idempotency_key": "cash-period-blocked"})
    )
    assert period_blocked.status == "rejected"
    assert period_blocked.errors == ["ACCOUNTING_PERIOD_NOT_GENERATED"]
