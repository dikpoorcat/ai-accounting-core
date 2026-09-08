from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from conftest import import_test_bank_transaction, prepare_authenticated_bank_account
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from sqlalchemy.orm.attributes import set_committed_value

from ai_accounting.borrowing_schemas import (
    BorrowingLenderReference,
    ConfirmBorrowingInterestRequest,
    DrawBorrowingRequest,
    PreviewBorrowingInterestRequest,
)
from ai_accounting.borrowing_service import (
    ACCOUNTING_RULE_SOURCE_URL,
    BorrowingService,
)
from ai_accounting.borrowings import MAX_FEN, SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION
from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.models import (
    Account,
    BankTransaction,
    Borrowing,
    BorrowingInterestAccrual,
    BorrowingPayment,
    BusinessEvent,
    BusinessEventComponent,
    Counterparty,
    Evidence,
    Organization,
    VoucherLine,
)
from ai_accounting.schemas import ReverseEventRequest
from ai_accounting.service import FinanceService


@pytest.fixture
def session(committable_session: Session) -> Session:
    return committable_session


def _confirm_bank_scope(session: Session, organization: Organization) -> None:
    if session.info.get("test_authenticated_bank_authority") is not None:
        return
    set_committed_value(organization, "bank_reconciliation_scope_current_action_id", None)
    set_committed_value(organization, "bank_reconciliation_scope_confirmed_at", None)
    prepare_authenticated_bank_account(
        session,
        organization,
        booking_date=date(2024, 2, 29),
        accounts=[
            {
                "bank_account_code": "1002",
                "account_name": "银行存款",
                "start_date": date(2000, 1, 1),
            },
            {
                "bank_account_code": "1003",
                "account_name": "测试银行二户",
                "start_date": date(2000, 1, 1),
            },
        ],
    )
    prepare_authenticated_bank_account(
        session,
        organization,
        booking_date=date(2028, 1, 1),
    )
    organization.accounting_period_control_enabled = False
    organization.accounting_period_control_start_date = None
    session.flush()


@pytest.fixture(autouse=True)
def confirmed_bank_scope(session: Session, organization: Organization) -> None:
    _confirm_bank_scope(session, organization)


def _evidence(session: Session, organization: Organization, seed: str) -> Evidence:
    row = Evidence(
        org_id=organization.id,
        sha256=(seed * 64)[:64],
        original_name=f"{seed}.pdf",
        media_type="application/pdf",
        source="test",
        size_bytes=1,
        storage_path=f"test/{seed}",
    )
    session.add(row)
    session.flush()
    return row


def _bank_row(
    session: Session,
    organization: Organization,
    *,
    amount_fen: int,
    booking_date: date,
    seed: str,
    currency: str = "CNY",
    account_code: str = "1002",
    controlled: bool = True,
) -> BankTransaction:
    if controlled:
        if currency != "CNY":
            raise AssertionError("non-CNY negative fixtures must be explicitly uncontrolled")
        return import_test_bank_transaction(
            session,
            organization,
            amount_fen=amount_fen,
            key=seed,
            booking_date=booking_date,
            bank_account_code=account_code,
        )
    row = BankTransaction(
        org_id=organization.id,
        bank_account_code=account_code,
        fingerprint=(seed * 64)[:64],
        booking_date=booking_date,
        amount_fen=amount_fen,
        currency=currency,
        memo=seed,
        source_sha256=(f"s{seed}" * 64)[:64],
    )
    session.add(row)
    session.flush()
    return row


def _interest_payment_request(
    session: Session,
    *,
    org_id: object,
    borrowing_id: object,
    accrual_event_id: object,
    idempotency_key: str,
    payment_date: date,
    posting_date: date,
    bank_account_code: str | None = None,
    bank_transaction_references: list[dict[str, object]] | None = None,
    evidence_references: list[object] | None = None,
) -> RecordEventRequest:
    accrual = session.scalar(
        select(BorrowingInterestAccrual).where(
            BorrowingInterestAccrual.event_id == accrual_event_id
        )
    )
    assert accrual is not None
    funds = []
    if bank_account_code is not None:
        funds = [
            {
                "key": "interest-funds",
                "account_code": bank_account_code,
                "direction": "payment",
                "payment_date": payment_date,
                "amount_fen": accrual.amount_fen,
                "allocations": [{"component_key": "interest", "amount_fen": accrual.amount_fen}],
                "bank_transaction_references": bank_transaction_references or [],
            }
        ]
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": idempotency_key,
            "posting_date": posting_date,
            "evidence_references": evidence_references or [],
            "components": [
                {
                    "key": "interest",
                    "kind": "borrowing_interest_payment",
                    "business_date": payment_date,
                    "payment_date": payment_date,
                    "borrowing_id": borrowing_id,
                    "accrual_event_id": accrual_event_id,
                    "amount_fen": accrual.amount_fen,
                }
            ],
            "funds": funds,
        }
    )


def _principal_payment_request(
    session: Session,
    *,
    org_id: object,
    borrowing_id: object,
    idempotency_key: str,
    repayment_date: date,
    posting_date: date,
    bank_account_code: str | None = None,
    bank_transaction_references: list[dict[str, object]] | None = None,
    evidence_references: list[object] | None = None,
) -> RecordEventRequest:
    borrowing = session.get(Borrowing, borrowing_id)
    assert borrowing is not None
    funds = []
    if bank_account_code is not None:
        funds = [
            {
                "key": "principal-funds",
                "account_code": bank_account_code,
                "direction": "payment",
                "payment_date": repayment_date,
                "amount_fen": borrowing.principal_fen,
                "allocations": [
                    {"component_key": "principal", "amount_fen": borrowing.principal_fen}
                ],
                "bank_transaction_references": bank_transaction_references or [],
            }
        ]
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": idempotency_key,
            "posting_date": posting_date,
            "evidence_references": evidence_references or [],
            "components": [
                {
                    "key": "principal",
                    "kind": "borrowing_principal_repayment",
                    "business_date": repayment_date,
                    "payment_date": repayment_date,
                    "borrowing_id": borrowing_id,
                    "amount_fen": borrowing.principal_fen,
                }
            ],
            "funds": funds,
        }
    )


def _draw_request(
    organization: Organization,
    evidence: Evidence,
    bank: BankTransaction,
    *,
    key: str = "loan-draw",
    borrowing_code: str = "LOAN-001",
    contract_name: str = "经营周转借款",
    drawdown_date: date = date(2026, 1, 1),
    due_date: date = date(2027, 1, 1),
    interest_due_dates: list[date] | None = None,
) -> DrawBorrowingRequest:
    if interest_due_dates is None:
        interest_due_dates = [date(2026, 7, 1), due_date]
    return DrawBorrowingRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "borrowing_code": borrowing_code,
            "contract_name": contract_name,
            "lender": {"name": "测试持牌银行"},
            "lender_is_licensed_financial_institution": True,
            "currency": "CNY",
            "principal_fen": 1_000_000,
            "drawdown_date": drawdown_date,
            "due_date": due_date,
            "posting_date": drawdown_date,
            "annual_rate_percent": "3.65",
            "day_count_basis": "actual_365",
            "interest_due_dates": interest_due_dates,
            "capitalization_applicable": False,
            "purpose_description": "仅用于日常经营周转",
            "term_facts": {
                "single_drawdown": True,
                "fixed_rate": True,
                "simple_interest": True,
                "bullet_principal_at_maturity": True,
                "allows_prepayment": False,
                "allows_extension": False,
                "has_penalty_interest": False,
                "has_financing_fees": False,
            },
            "bank_account_code": bank.bank_account_code,
            "bank_transaction_references": [{"id": bank.id}],
            "evidence_references": [evidence.id],
        }
    )


def _roles_for_voucher(session: Session, voucher_id: object) -> list[tuple[str, int, int]]:
    rows = session.execute(
        select(
            Account.system_role,
            Account.code,
            VoucherLine.debit_fen,
            VoucherLine.credit_fen,
        )
        .join(VoucherLine, VoucherLine.account_id == Account.id)
        .where(VoucherLine.voucher_id == voucher_id)
        .order_by(VoucherLine.line_number)
    ).all()
    assert sum(row.debit_fen for row in rows) == sum(row.credit_fen for row in rows)
    return [(row.system_role or row.code, row.debit_fen, row.credit_fen) for row in rows]


def test_bank_draw_requires_confirmed_scope_without_business_write(
    session: Session, organization: Organization
) -> None:
    evidence = _evidence(session, organization, "scope-borrowing")
    bank = _bank_row(
        session,
        organization,
        amount_fen=1_000_000,
        booking_date=date(2026, 1, 1),
        seed="scope-borrowing-bank",
    )
    request = _draw_request(
        organization,
        evidence,
        bank,
        key="scope-borrowing-draw",
        borrowing_code="LOAN-SCOPE",
    )
    set_committed_value(organization, "bank_reconciliation_scope_current_action_id", None)
    set_committed_value(organization, "bank_reconciliation_scope_confirmed_at", None)

    result = BorrowingService(session).draw_borrowing(request)

    assert result.status == "needs_information"
    assert result.event_id is None
    assert result.missing_information[0].fields == ["bank_reconciliation_scope_confirmation"]
    assert session.scalars(select(BusinessEvent)).all() == []
    assert session.scalars(select(Borrowing)).all() == []
    assert session.scalars(select(Counterparty)).all() == []
    assert session.scalars(select(VoucherLine)).all() == []
    assert bank.matched_event_id is None


def test_borrowing_write_preserves_period_control_error(
    session: Session, organization: Organization
) -> None:
    organization.accounting_period_control_enabled = True
    organization.accounting_period_control_start_date = None
    evidence = _evidence(session, organization, "period-loan")
    bank = _bank_row(
        session,
        organization,
        amount_fen=1_000_000,
        booking_date=date(2026, 1, 1),
        seed="period-loan-bank",
        controlled=False,
    )

    result = BorrowingService(session).draw_borrowing(
        _draw_request(
            organization,
            evidence,
            bank,
            key="borrowing-period-not-generated",
            borrowing_code="LOAN-PERIOD",
        )
    )

    assert result.status == "rejected"
    assert result.errors == ["ACCOUNTING_PERIOD_NOT_GENERATED"]
    assert result.event_id is None
    assert result.voucher_id is None


def _confirm_interest(
    service: BorrowingService,
    organization: Organization,
    borrowing_id: object,
    *,
    period_start: date,
    period_end: date,
    key: str,
) -> object:
    preview = service.preview_borrowing_interest(
        PreviewBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=borrowing_id,
            period_start=period_start,
            period_end=period_end,
        )
    )
    assert preview.status == "calculated"
    return service.confirm_borrowing_interest(
        ConfirmBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=borrowing_id,
            period_start=period_start,
            period_end=period_end,
            calculation_hash=preview.calculation_hash,
            idempotency_key=key,
        )
    )


def test_missing_information_decision_is_idempotent_and_has_no_voucher(
    session: Session, organization: Organization
) -> None:
    service = BorrowingService(session)
    request = DrawBorrowingRequest(org_id=organization.id, idempotency_key="missing-loan")

    first = service.draw_borrowing(request)
    replay = service.draw_borrowing(request)

    assert first.status == "needs_information"
    assert replay.event_id == first.event_id
    assert replay.voucher_id is None
    assert replay.voucher_number is None
    assert replay.data["idempotent_replay"] is True
    assert service.draw_borrowing(
        request.model_copy(update={"borrowing_code": "LOAN-CHANGED"})
    ).errors == ["BORROWING_IDEMPOTENCY_PAYLOAD_MISMATCH"]


def test_borrowing_full_lifecycle_is_balanced_idempotent_and_strictly_reversible(
    session: Session, organization: Organization
) -> None:
    service = BorrowingService(session)
    contract = _evidence(session, organization, "contract")
    draw_bank = _bank_row(
        session,
        organization,
        amount_fen=1_000_000,
        booking_date=date(2026, 1, 1),
        seed="draw",
        account_code="1003",
    )
    request = _draw_request(organization, contract, draw_bank)

    missing_draw_code = service.draw_borrowing(
        request.model_copy(
            update={
                "idempotency_key": "loan-draw-missing-code",
                "borrowing_code": "LOAN-MISSING-CODE",
                "bank_account_code": None,
                "bank_transaction_references": [],
            }
        )
    )
    assert missing_draw_code.status == "needs_information"
    assert any(
        "bank_account_code" in requirement.fields
        for requirement in missing_draw_code.missing_information
    )

    wrong_draw_bank = _bank_row(
        session,
        organization,
        amount_fen=1_000_000,
        booking_date=date(2026, 1, 1),
        seed="draw-wrong-bank",
        account_code="1002",
        controlled=False,
    )
    wrong_draw_request = DrawBorrowingRequest.model_validate(
        request.model_dump(mode="python")
        | {
            "idempotency_key": "loan-draw-wrong-bank",
            "borrowing_code": "LOAN-WRONG-BANK",
            "bank_transaction_references": [{"id": wrong_draw_bank.id}],
        }
    )
    assert service.draw_borrowing(wrong_draw_request).errors == [
        "FUNDS_BANK_ACCOUNT_OR_DIRECTION_MISMATCH"
    ]
    assert wrong_draw_bank.matched_event_id is None

    drawn = service.draw_borrowing(request)

    assert drawn.status == "posted", drawn.errors
    assert set(_roles_for_voucher(session, drawn.voucher_id)) == {
        ("1003", 1_000_000, 0),
        ("short_term_borrowing", 0, 1_000_000),
    }
    borrowing = session.get(Borrowing, drawn.borrowing_id)
    assert borrowing.single_drawdown is True
    assert borrowing.fixed_rate is True
    assert borrowing.simple_interest is True
    assert borrowing.bullet_principal_at_maturity is True
    assert borrowing.allows_prepayment is False
    assert borrowing.allows_extension is False
    assert borrowing.has_penalty_interest is False
    assert borrowing.has_financing_fees is False
    draw_event = session.get(BusinessEvent, drawn.event_id)
    assert draw_event.facts["accounting_rule_version"] == SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION
    assert draw_event.facts["accounting_rule_source_url"] == ACCOUNTING_RULE_SOURCE_URL
    assert draw_event.facts["bank_account_code"] == "1003"
    assert {item.id for item in draw_event.evidence} == {contract.id}

    replay = service.draw_borrowing(request)
    assert replay.event_id == drawn.event_id
    assert replay.voucher_id == drawn.voucher_id
    assert replay.voucher_number == drawn.voucher_number
    assert replay.data["idempotent_replay"] is True
    changed = request.model_copy(update={"contract_name": "已篡改合同名"})
    assert service.draw_borrowing(changed).errors == ["BORROWING_IDEMPOTENCY_PAYLOAD_MISMATCH"]
    assert service.draw_borrowing(
        request.model_copy(update={"bank_account_code": "1002"})
    ).errors == ["BORROWING_IDEMPOTENCY_PAYLOAD_MISMATCH"]

    preview = service.preview_borrowing_interest(
        PreviewBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=borrowing.id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 7, 1),
        )
    )
    assert preview.status == "calculated"
    confirmed_scope_action_id = organization.bank_reconciliation_scope_current_action_id
    confirmed_scope_at = organization.bank_reconciliation_scope_confirmed_at
    set_committed_value(organization, "bank_reconciliation_scope_current_action_id", None)
    set_committed_value(organization, "bank_reconciliation_scope_confirmed_at", None)
    stale = service.confirm_borrowing_interest(
        ConfirmBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=borrowing.id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 7, 1),
            calculation_hash="0" * 64,
            idempotency_key="interest-stale",
        )
    )
    assert stale.errors == ["BORROWING_CALCULATION_STALE"]
    first_accrual = service.confirm_borrowing_interest(
        ConfirmBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=borrowing.id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 7, 1),
            calculation_hash=preview.calculation_hash,
            idempotency_key="interest-first",
        )
    )
    assert first_accrual.status == "posted", first_accrual.errors
    replayed_accrual = service.confirm_borrowing_interest(
        ConfirmBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=borrowing.id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 7, 1),
            calculation_hash=preview.calculation_hash,
            idempotency_key="interest-first",
        )
    )
    assert replayed_accrual.event_id == first_accrual.event_id
    assert replayed_accrual.data["idempotent_replay"] is True
    changed_accrual = service.confirm_borrowing_interest(
        ConfirmBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=borrowing.id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 7, 1),
            calculation_hash="1" * 64,
            idempotency_key="interest-first",
        )
    )
    assert changed_accrual.errors == ["BORROWING_IDEMPOTENCY_PAYLOAD_MISMATCH"]
    assert _roles_for_voucher(session, first_accrual.voucher_id) == [
        ("borrowing_interest_expense", first_accrual.data["interest_fen"], 0),
        ("interest_payable", 0, first_accrual.data["interest_fen"]),
    ]
    first_row = session.scalar(
        select(BorrowingInterestAccrual).where(
            BorrowingInterestAccrual.event_id == first_accrual.event_id
        )
    )
    assert first_row.actual_days == 181
    set_committed_value(
        organization,
        "bank_reconciliation_scope_current_action_id",
        confirmed_scope_action_id,
    )
    set_committed_value(
        organization,
        "bank_reconciliation_scope_confirmed_at",
        confirmed_scope_at,
    )

    skipped = service.preview_borrowing_interest(
        PreviewBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=borrowing.id,
            period_start=date(2026, 7, 1),
            period_end=date(2026, 12, 31),
        )
    )
    assert skipped.errors == ["BORROWING_INTEREST_OUT_OF_SEQUENCE"]

    pay_evidence = _evidence(session, organization, "pay-one")
    premature_bank = _bank_row(
        session,
        organization,
        amount_fen=-first_row.amount_fen,
        booking_date=date(2026, 6, 30),
        seed="premature",
        account_code="1003",
    )
    premature = FinanceService(session).record_event(
        _interest_payment_request(
            session,
            org_id=organization.id,
            borrowing_id=borrowing.id,
            accrual_event_id=first_row.event_id,
            idempotency_key="pay-premature",
            payment_date=date(2026, 6, 30),
            posting_date=date(2026, 6, 30),
            bank_account_code="1003",
            bank_transaction_references=[{"id": premature_bank.id}],
            evidence_references=[pay_evidence.id],
        )
    )
    assert premature.errors == ["BORROWING_INTEREST_PAYMENT_BEFORE_DUE_DATE"]

    late_bank = _bank_row(
        session,
        organization,
        amount_fen=-first_row.amount_fen,
        booking_date=date(2027, 1, 2),
        seed="late-interest",
        account_code="1003",
    )
    late = FinanceService(session).record_event(
        _interest_payment_request(
            session,
            org_id=organization.id,
            borrowing_id=borrowing.id,
            accrual_event_id=first_row.event_id,
            idempotency_key="pay-after-maturity",
            payment_date=date(2027, 1, 2),
            posting_date=date(2027, 1, 2),
            bank_account_code="1003",
            bank_transaction_references=[{"id": late_bank.id}],
            evidence_references=[pay_evidence.id],
        )
    )
    assert late.errors == ["BORROWING_INTEREST_PAYMENT_DATE_INVALID"]
    assert late_bank.matched_event_id is None
    assert (
        session.scalars(
            select(BorrowingPayment).where(BorrowingPayment.borrowing_id == borrowing.id)
        ).all()
        == []
    )

    first_payment_bank = _bank_row(
        session,
        organization,
        amount_fen=-first_row.amount_fen,
        booking_date=date(2026, 7, 1),
        seed="pay-first",
        account_code="1003",
    )
    missing_interest_code = FinanceService(session).record_event(
        _interest_payment_request(
            session,
            org_id=organization.id,
            borrowing_id=borrowing.id,
            accrual_event_id=first_row.event_id,
            idempotency_key="pay-first-missing-code",
            payment_date=date(2026, 7, 1),
            posting_date=date(2026, 7, 1),
            bank_transaction_references=[],
            evidence_references=[pay_evidence.id],
        )
    )
    assert missing_interest_code.status == "rejected"
    assert missing_interest_code.errors == ["COMPONENT_FUNDS_CONSERVATION_FAILED:interest"]

    wrong_interest_bank = _bank_row(
        session,
        organization,
        amount_fen=-first_row.amount_fen,
        booking_date=date(2026, 7, 1),
        seed="pay-first-wrong-bank",
        account_code="1002",
        controlled=False,
    )
    wrong_interest = FinanceService(session).record_event(
        _interest_payment_request(
            session,
            org_id=organization.id,
            borrowing_id=borrowing.id,
            accrual_event_id=first_row.event_id,
            idempotency_key="pay-first-wrong-bank",
            payment_date=date(2026, 7, 1),
            posting_date=date(2026, 7, 1),
            bank_account_code="1003",
            bank_transaction_references=[{"id": wrong_interest_bank.id}],
            evidence_references=[pay_evidence.id],
        )
    )
    assert wrong_interest.errors == ["FUNDS_BANK_ACCOUNT_OR_DIRECTION_MISMATCH"]
    assert wrong_interest_bank.matched_event_id is None

    first_payment_request = _interest_payment_request(
        session,
        org_id=organization.id,
        borrowing_id=borrowing.id,
        accrual_event_id=first_row.event_id,
        idempotency_key="pay-first",
        payment_date=date(2026, 7, 1),
        posting_date=date(2026, 7, 1),
        bank_account_code="1003",
        bank_transaction_references=[{"id": first_payment_bank.id}],
        evidence_references=[pay_evidence.id],
    )
    first_payment = FinanceService(session).record_event(first_payment_request)
    assert first_payment.status == "posted"
    assert first_payment_bank.matched_event_id == first_payment.event_id
    assert _roles_for_voucher(session, first_payment.voucher_id) == [
        ("interest_payable", first_row.amount_fen, 0),
        ("1003", 0, first_row.amount_fen),
    ]
    assert (
        session.get(BusinessEvent, first_payment.event_id).facts["funds"][0]["account_code"]
        == "1003"
    )
    assert (
        FinanceService(session).record_event(first_payment_request).event_id
        == first_payment.event_id
    )
    changed_payment = first_payment_request.model_copy(deep=True)
    changed_payment.funds[0].account_code = "1002"
    assert FinanceService(session).record_event(changed_payment).errors == [
        "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"
    ]
    assert FinanceService(session).record_event(
        _interest_payment_request(
            session,
            org_id=organization.id,
            borrowing_id=borrowing.id,
            accrual_event_id=first_row.event_id,
            idempotency_key="pay-first-again",
            payment_date=date(2026, 7, 1),
            posting_date=date(2026, 7, 1),
            bank_account_code="1003",
            bank_transaction_references=[],
            evidence_references=[pay_evidence.id],
        )
    ).errors == ["BORROWING_INTEREST_ALREADY_PAID"]
    assert first_payment_request.components[0].borrowing_id == borrowing.id

    second_accrual = _confirm_interest(
        service,
        organization,
        borrowing.id,
        period_start=date(2026, 7, 1),
        period_end=date(2027, 1, 1),
        key="interest-second",
    )
    second_row = session.scalar(
        select(BorrowingInterestAccrual).where(
            BorrowingInterestAccrual.event_id == second_accrual.event_id
        )
    )
    _roles_for_voucher(session, second_accrual.voucher_id)
    second_evidence = _evidence(session, organization, "pay-two")
    second_payment_bank = _bank_row(
        session,
        organization,
        amount_fen=-second_row.amount_fen,
        booking_date=date(2027, 1, 1),
        seed="pay-second",
        account_code="1003",
    )
    second_payment = FinanceService(session).record_event(
        _interest_payment_request(
            session,
            org_id=organization.id,
            borrowing_id=borrowing.id,
            accrual_event_id=second_row.event_id,
            idempotency_key="pay-second",
            payment_date=date(2027, 1, 1),
            posting_date=date(2027, 1, 1),
            bank_account_code="1003",
            bank_transaction_references=[{"id": second_payment_bank.id}],
            evidence_references=[second_evidence.id],
        )
    )
    assert second_payment.status == "posted", second_payment.errors
    assert second_payment_bank.matched_event_id == second_payment.event_id
    _roles_for_voucher(session, second_payment.voucher_id)
    repayment_evidence = _evidence(session, organization, "principal")
    repayment_bank = _bank_row(
        session,
        organization,
        amount_fen=-borrowing.principal_fen,
        booking_date=date(2027, 1, 1),
        seed="principal",
        account_code="1003",
    )
    missing_principal_code = FinanceService(session).record_event(
        _principal_payment_request(
            session,
            org_id=organization.id,
            borrowing_id=borrowing.id,
            idempotency_key="principal-missing-code",
            repayment_date=date(2027, 1, 1),
            posting_date=date(2027, 1, 1),
            evidence_references=[repayment_evidence.id],
        )
    )
    assert missing_principal_code.status == "rejected"
    assert missing_principal_code.errors == ["COMPONENT_FUNDS_CONSERVATION_FAILED:principal"]

    wrong_principal_bank = _bank_row(
        session,
        organization,
        amount_fen=-borrowing.principal_fen,
        booking_date=date(2027, 1, 1),
        seed="principal-wrong-bank",
        account_code="1002",
        controlled=False,
    )
    wrong_principal = FinanceService(session).record_event(
        _principal_payment_request(
            session,
            org_id=organization.id,
            borrowing_id=borrowing.id,
            idempotency_key="principal-wrong-bank",
            repayment_date=date(2027, 1, 1),
            posting_date=date(2027, 1, 1),
            bank_account_code="1003",
            bank_transaction_references=[{"id": wrong_principal_bank.id}],
            evidence_references=[repayment_evidence.id],
        )
    )
    assert wrong_principal.errors == ["FUNDS_BANK_ACCOUNT_OR_DIRECTION_MISMATCH"]
    assert wrong_principal_bank.matched_event_id is None

    repayment_request = _principal_payment_request(
        session,
        org_id=organization.id,
        borrowing_id=borrowing.id,
        idempotency_key="principal",
        repayment_date=date(2027, 1, 1),
        posting_date=date(2027, 1, 1),
        bank_account_code="1003",
        bank_transaction_references=[{"id": repayment_bank.id}],
        evidence_references=[repayment_evidence.id],
    )
    repayment = FinanceService(session).record_event(repayment_request)
    assert repayment.status == "posted", repayment.errors
    assert repayment_bank.matched_event_id == repayment.event_id
    assert _roles_for_voucher(session, repayment.voucher_id) == [
        ("short_term_borrowing", borrowing.principal_fen, 0),
        ("1003", 0, borrowing.principal_fen),
    ]
    assert (
        session.get(BusinessEvent, repayment.event_id).facts["funds"][0]["account_code"] == "1003"
    )
    assert FinanceService(session).record_event(repayment_request).event_id == repayment.event_id
    changed_repayment = repayment_request.model_copy(deep=True)
    changed_repayment.funds[0].account_code = "1002"
    assert FinanceService(session).record_event(changed_repayment).errors == [
        "IDEMPOTENCY_KEY_PAYLOAD_MISMATCH"
    ]
    projection = service.get_borrowing(organization.id, borrowing.id)
    assert projection.data["state"] == "repaid"
    assert projection.data["outstanding_principal_fen"] == 0
    assert projection.data["unpaid_interest_fen"] == 0
    assert projection.data["accrued_interest_fen"] == projection.data["paid_interest_fen"]
    assert len(projection.data["accruals"]) == 2
    formal_events = [
        drawn.event_id,
        first_accrual.event_id,
        first_payment.event_id,
        second_accrual.event_id,
        second_payment.event_id,
        repayment.event_id,
    ]
    for event_id in formal_events:
        components = session.scalars(
            select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == event_id,
                BusinessEventComponent.kind != "funds",
            )
        ).all()
        assert len(components) == 1
        assert components[0].rule_version == SMALL_ENTERPRISE_BORROWINGS_RULE_VERSION

    blocked = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=drawn.event_id,
            idempotency_key="reverse-draw-blocked",
            reason="wrong order",
            posting_date=date(2027, 1, 2),
        )
    )
    assert blocked.errors == ["BORROWING_OPEN_DEPENDENCIES_EXIST"]
    reversal_order = [
        repayment.event_id,
        second_payment.event_id,
        second_accrual.event_id,
        first_payment.event_id,
        first_accrual.event_id,
        drawn.event_id,
    ]
    for sequence, event_id in enumerate(reversal_order, start=1):
        result = service.reverse_event(
            ReverseEventRequest(
                org_id=organization.id,
                event_id=event_id,
                idempotency_key=f"reverse-{sequence}",
                reason="correct lifecycle",
                posting_date=date(2027, 1, 2),
            )
        )
        assert result.status == "posted"
        assert session.get(BusinessEvent, event_id).status == "reversed"

    reversed_projection = service.get_borrowing(organization.id, borrowing.id)
    assert reversed_projection.status == "reversed"
    assert reversed_projection.data["state"] == "reversed"
    assert reversed_projection.data["on_book"] is False
    assert reversed_projection.data["outstanding_principal_fen"] == 0
    assert reversed_projection.data["unpaid_interest_fen"] == 0
    assert len(reversed_projection.data["accrual_history"]) == 2
    assert len(reversed_projection.data["payment_history"]) == 3
    assert {row["event_status"] for row in reversed_projection.data["accrual_history"]} == {
        "reversed"
    }

    cannot_reuse = service.draw_borrowing(
        request.model_copy(update={"idempotency_key": "loan-redraw"})
    )
    assert cannot_reuse.errors == ["BORROWING_CODE_ALREADY_EXISTS"]


def test_long_term_template_and_unsupported_terms_are_stable(
    session: Session, organization: Organization
) -> None:
    service = BorrowingService(session)
    evidence = _evidence(session, organization, "long-contract")
    bank = _bank_row(
        session,
        organization,
        amount_fen=1_000_000,
        booking_date=date(2024, 2, 29),
        seed="long-draw",
    )
    request = _draw_request(
        organization,
        evidence,
        bank,
        key="long-loan",
        borrowing_code="LOAN-LONG",
        drawdown_date=date(2024, 2, 29),
        due_date=date(2025, 3, 1),
        interest_due_dates=[date(2025, 3, 1)],
    )
    drawn = service.draw_borrowing(request)
    assert drawn.status == "posted"
    assert "long_term_borrowing" in {
        role for role, _debit, _credit in _roles_for_voucher(session, drawn.voucher_id)
    }

    unsupported = _draw_request(
        organization,
        evidence,
        bank,
        key="unsupported",
        borrowing_code="LOAN-UNSUPPORTED",
    )
    unsupported = unsupported.model_copy(
        update={"term_facts": unsupported.term_facts.model_copy(update={"fixed_rate": False})}
    )
    unsupported_result = service.draw_borrowing(unsupported)
    assert unsupported_result.errors == ["BORROWING_UNSUPPORTED_TERMS"]
    unsupported_replay = service.draw_borrowing(unsupported)
    assert unsupported_replay.event_id == unsupported_result.event_id
    assert unsupported_replay.errors == ["BORROWING_UNSUPPORTED_TERMS"]
    assert service.draw_borrowing(unsupported.model_copy(update={"currency": "USD"})).errors == [
        "BORROWING_IDEMPOTENCY_PAYLOAD_MISMATCH"
    ]

    capitalization = _draw_request(
        organization,
        evidence,
        bank,
        key="capitalization",
        borrowing_code="LOAN-CAPITALIZE",
    ).model_copy(update={"capitalization_applicable": True})
    assert service.draw_borrowing(capitalization).errors == ["BORROWING_CAPITALIZATION_NOT_ENABLED"]


def test_interest_hash_covers_active_accrual_lineage_after_reversal_and_repost(
    session: Session, organization: Organization
) -> None:
    service = BorrowingService(session)
    evidence = _evidence(session, organization, "lineage-contract")
    bank = _bank_row(
        session,
        organization,
        amount_fen=1_000_000,
        booking_date=date(2026, 1, 1),
        seed="lineage-draw",
    )
    drawn = service.draw_borrowing(
        _draw_request(
            organization,
            evidence,
            bank,
            key="lineage-draw",
            borrowing_code="LOAN-LINEAGE",
        )
    )
    first_preview = service.preview_borrowing_interest(
        PreviewBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=drawn.borrowing_id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 7, 1),
        )
    )
    first = service.confirm_borrowing_interest(
        ConfirmBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=drawn.borrowing_id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 7, 1),
            calculation_hash=first_preview.calculation_hash,
            idempotency_key="lineage-first",
        )
    )
    assert first.status == "posted", first.errors
    old_second = service.preview_borrowing_interest(
        PreviewBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=drawn.borrowing_id,
            period_start=date(2026, 7, 1),
            period_end=date(2027, 1, 1),
        )
    )
    reversal = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=first.event_id,
            idempotency_key="lineage-reverse-first",
            reason="replace first accrual",
            posting_date=date(2026, 7, 2),
        )
    )
    assert reversal.status == "posted"
    replacement_preview = service.preview_borrowing_interest(
        PreviewBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=drawn.borrowing_id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 7, 1),
        )
    )
    replacement = service.confirm_borrowing_interest(
        ConfirmBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=drawn.borrowing_id,
            period_start=date(2026, 1, 1),
            period_end=date(2026, 7, 1),
            calculation_hash=replacement_preview.calculation_hash,
            idempotency_key="lineage-first-replacement",
        )
    )
    assert replacement.event_id != first.event_id
    stale_second = service.confirm_borrowing_interest(
        ConfirmBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=drawn.borrowing_id,
            period_start=date(2026, 7, 1),
            period_end=date(2027, 1, 1),
            calculation_hash=old_second.calculation_hash,
            idempotency_key="lineage-second-stale",
        )
    )
    assert stale_second.errors == ["BORROWING_CALCULATION_STALE"]


def test_bank_currency_and_lender_identity_are_frozen(
    session: Session, organization: Organization
) -> None:
    service = BorrowingService(session)
    evidence = _evidence(session, organization, "identity-contract")
    session.execute(text("PRAGMA ignore_check_constraints = ON"))
    usd_bank = _bank_row(
        session,
        organization,
        amount_fen=1_000_000,
        booking_date=date(2026, 1, 1),
        seed="usd-draw",
        currency="USD",
        controlled=False,
    )
    session.execute(text("PRAGMA ignore_check_constraints = OFF"))
    currency_result = service.draw_borrowing(
        _draw_request(
            organization,
            evidence,
            usd_bank,
            key="usd-bank-row",
            borrowing_code="LOAN-USD-BANK",
        )
    )
    assert currency_result.errors == ["FUNDS_BANK_CURRENCY_MISMATCH"]
    assert usd_bank.matched_event_id is None
    assert (
        session.scalar(
            select(Counterparty.id).where(
                Counterparty.org_id == organization.id,
                Counterparty.kind == "other",
                Counterparty.name == "测试持牌银行",
            )
        )
        is None
    )

    wrong_kind = Counterparty(org_id=organization.id, kind="supplier", name="错误类型贷款人")
    other_organization = Organization(
        name="另一组织",
        taxpayer_identification_number="91330106MA1234567T",
    )
    session.add(other_organization)
    session.flush()
    foreign_lender = Counterparty(
        org_id=other_organization.id,
        kind="other",
        name="跨组织贷款人",
        external_ref="FOREIGN-BANK",
    )
    exact = Counterparty(
        org_id=organization.id,
        kind="other",
        name="冻结身份银行",
        external_ref="BANK-001",
    )
    session.add_all([wrong_kind, foreign_lender, exact])
    session.flush()
    base = _draw_request(
        organization,
        evidence,
        usd_bank,
        key="identity-base",
        borrowing_code="LOAN-IDENTITY",
    )

    wrong_kind_request = base.model_copy(
        update={
            "idempotency_key": "wrong-kind",
            "borrowing_code": "LOAN-WRONG-KIND",
            "lender": BorrowingLenderReference(id=wrong_kind.id),
        }
    )
    assert service.draw_borrowing(wrong_kind_request).errors == [
        "BORROWING_LENDER_NOT_FOUND_OR_INVALID"
    ]

    cross_org_request = base.model_copy(
        update={
            "idempotency_key": "cross-org-lender",
            "borrowing_code": "LOAN-CROSS-ORG",
            "lender": BorrowingLenderReference(id=foreign_lender.id),
        }
    )
    assert service.draw_borrowing(cross_org_request).errors == [
        "BORROWING_LENDER_NOT_FOUND_OR_INVALID"
    ]

    contradictory = base.model_copy(
        update={
            "idempotency_key": "contradictory-lender",
            "borrowing_code": "LOAN-CONTRADICTORY",
            "lender": BorrowingLenderReference(
                id=exact.id,
                name="另一银行",
                external_ref="BANK-001",
            ),
        }
    )
    assert service.draw_borrowing(contradictory).errors == ["BORROWING_LENDER_IDENTITY_MISMATCH"]

    same_name_different_ref = base.model_copy(
        update={
            "idempotency_key": "different-lender-ref",
            "borrowing_code": "LOAN-DIFFERENT-REF",
            "lender": BorrowingLenderReference(
                name=exact.name,
                external_ref="BANK-CHANGED",
            ),
        }
    )
    assert service.draw_borrowing(same_name_different_ref).errors == [
        "BORROWING_LENDER_IDENTITY_MISMATCH"
    ]


def test_nonposted_idempotency_race_reads_winner_without_leaking_unique_error(
    session: Session, organization: Organization, monkeypatch
) -> None:
    service = BorrowingService(session)
    evidence = _evidence(session, organization, "race-contract")
    bank = _bank_row(
        session,
        organization,
        amount_fen=1_000_000,
        booking_date=date(2026, 1, 1),
        seed="race-bank",
    )
    request = _draw_request(
        organization,
        evidence,
        bank,
        key="nonposted-race",
        borrowing_code="LOAN-RACE",
    )
    request = request.model_copy(
        update={"term_facts": request.term_facts.model_copy(update={"fixed_rate": False})}
    )
    payload_hash = service._borrowing_request_hash("finance_draw_borrowing", request)
    winner = BusinessEvent(
        org_id=organization.id,
        idempotency_key=request.idempotency_key,
        request_payload_hash=payload_hash,
        event_type="borrowing_drawdown",
        status="rejected",
        description="",
        facts={
            **request.model_dump(mode="json"),
            "_command": "finance_draw_borrowing",
            "_result_errors": ["BORROWING_UNSUPPORTED_TERMS"],
            "_result_missing_information": [],
        },
        business_date=request.drawdown_date,
        posting_date=request.posting_date,
        rule_trace=[{"stage": "validation", "status": "rejected"}],
    )
    session.add(winner)
    session.flush()
    original_lookup = service._idempotent_event
    calls = 0

    def race_lookup(org_id, idempotency_key):
        nonlocal calls
        calls += 1
        if calls == 1:
            return None
        return original_lookup(org_id, idempotency_key)

    monkeypatch.setattr(service, "_idempotent_event", race_lookup)
    result = service.draw_borrowing(request)

    assert result.status == "rejected"
    assert result.event_id == winner.id
    assert result.errors == ["BORROWING_UNSUPPORTED_TERMS"]
    assert service.draw_borrowing(request.model_copy(update={"currency": "USD"})).errors == [
        "BORROWING_IDEMPOTENCY_PAYLOAD_MISMATCH"
    ]


def test_extreme_interest_is_rejected_during_preview_before_database_write(
    session: Session, organization: Organization
) -> None:
    service = BorrowingService(session)
    evidence = _evidence(session, organization, "maximum-contract")
    bank = _bank_row(
        session,
        organization,
        amount_fen=MAX_FEN,
        booking_date=date(2026, 1, 1),
        seed="maximum-bank",
    )
    request = _draw_request(
        organization,
        evidence,
        bank,
        key="maximum-draw",
        borrowing_code="LOAN-MAXIMUM",
        due_date=date(2028, 1, 1),
        interest_due_dates=[date(2028, 1, 1)],
    ).model_copy(
        update={
            "principal_fen": MAX_FEN,
            "annual_rate_percent": Decimal("100.000000"),
        }
    )
    drawn = service.draw_borrowing(request)
    assert drawn.status == "posted", drawn.errors

    preview = service.preview_borrowing_interest(
        PreviewBorrowingInterestRequest(
            org_id=organization.id,
            borrowing_id=drawn.borrowing_id,
            period_start=date(2026, 1, 1),
            period_end=date(2028, 1, 1),
        )
    )
    assert preview.errors == ["BORROWING_INTEREST_AMOUNT_OUT_OF_RANGE"]
    assert (
        session.scalars(
            select(BorrowingInterestAccrual).where(
                BorrowingInterestAccrual.borrowing_id == drawn.borrowing_id
            )
        ).all()
        == []
    )


def test_normalized_rate_and_interest_hash_are_stable_across_sessions(tmp_path) -> None:
    database_path = tmp_path / "borrowing-rate.db"
    engine = make_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory() as first_session:
            organization = seed_organization(
                first_session,
                taxpayer_identification_number="91330106MA1234567T",
                name="跨会话借款测试组织",
            )
            organization.accounting_period_control_enabled = False
            first_session.flush()
            _confirm_bank_scope(first_session, organization)
            evidence = _evidence(first_session, organization, "cross-session-contract")
            bank = _bank_row(
                first_session,
                organization,
                amount_fen=1_000_000,
                booking_date=date(2026, 1, 1),
                seed="cross-session-bank",
            )
            service = BorrowingService(first_session)
            drawn = service.draw_borrowing(
                _draw_request(
                    organization,
                    evidence,
                    bank,
                    key="cross-session-draw",
                    borrowing_code="LOAN-CROSS-SESSION",
                )
            )
            first_preview = service.preview_borrowing_interest(
                PreviewBorrowingInterestRequest(
                    org_id=organization.id,
                    borrowing_id=drawn.borrowing_id,
                    period_start=date(2026, 1, 1),
                    period_end=date(2026, 7, 1),
                )
            )
            org_id = organization.id
            borrowing_id = drawn.borrowing_id
            first_session.commit()

        with factory() as second_session:
            reloaded = second_session.get(Borrowing, borrowing_id)
            second_preview = BorrowingService(second_session).preview_borrowing_interest(
                PreviewBorrowingInterestRequest(
                    org_id=org_id,
                    borrowing_id=borrowing_id,
                    period_start=date(2026, 1, 1),
                    period_end=date(2026, 7, 1),
                )
            )
            assert reloaded.annual_rate_percent == Decimal("3.650000")
            assert second_preview.calculation_hash == first_preview.calculation_hash
            assert second_preview.data["interest_fen"] == first_preview.data["interest_fen"]
            assert second_preview.data["annual_rate_percent"] == "3.650000"
    finally:
        engine.dispose()
