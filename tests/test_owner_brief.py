from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

from sqlalchemy.orm import Session

from ai_accounting.models import (
    AccountingPeriod,
    AccountingPeriodAction,
    AccountingPeriodCalendar,
    AccountingPeriodClose,
    BankTransaction,
    BusinessEvent,
    Counterparty,
    OpenItem,
    Organization,
)
from ai_accounting.owner_brief import OwnerBriefService


def _event(
    organization: Organization,
    *,
    key: str,
    event_type: str,
    posting_date: date,
    description: str,
) -> BusinessEvent:
    return BusinessEvent(
        org_id=organization.id,
        idempotency_key=key,
        request_payload_hash="a" * 64,
        event_type=event_type,
        status="posted",
        description=description,
        facts={},
        business_date=posting_date,
        posting_date=posting_date,
        rule_trace=[],
        rule_version="owner-brief-test-v1",
    )


def _seed_periods(session: Session, organization: Organization) -> AccountingPeriodClose:
    calendar = AccountingPeriodCalendar(
        org_id=organization.id,
        calendar_year=2026,
        rule_version="calendar-test-v1",
        rule_effective_from=date(2026, 1, 1),
        source_urls=["https://example.test/calendar"],
    )
    january_generation = AccountingPeriodAction(
        org_id=organization.id,
        action_type="period_generation",
        idempotency_key="owner-brief-period-2026-01",
        request_payload_hash="1" * 64,
        status="posted",
        input_facts={},
        missing_information=[],
        errors=[],
        confirmed_by="owner",
        confirmation_note="test",
    )
    session.add_all([calendar, january_generation])
    session.flush()

    january = AccountingPeriod(
        org_id=organization.id,
        calendar_id=calendar.id,
        generation_action_id=january_generation.id,
        calendar_year=2026,
        calendar_month=1,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 1, 31),
        status="open",
    )
    session.add(january)
    session.flush()

    close_action = AccountingPeriodAction(
        org_id=organization.id,
        action_type="period_close",
        idempotency_key="owner-brief-close-2026-01",
        request_payload_hash="2" * 64,
        status="posted",
        input_facts={},
        missing_information=[],
        errors=[],
        confirmed_by="owner",
        confirmation_note="test",
    )
    session.add(close_action)
    session.flush()

    closed_at = datetime(2026, 2, 5, tzinfo=UTC)
    close = AccountingPeriodClose(
        org_id=organization.id,
        period_id=january.id,
        action_id=close_action.id,
        calculation={},
        calculation_payload="{}",
        calculation_hash="c" * 64,
        rule_version="close-test-v1",
        rule_effective_from=date(2026, 1, 1),
        source_urls=["https://example.test/close"],
        previous_close_hash=None,
        checker_version="checker-test-v1",
        confirmed_at=closed_at,
        voucher_count=0,
        line_count=0,
        total_debit_fen=0,
        total_credit_fen=0,
    )
    session.add(close)
    session.flush()
    january.status = "closed"
    january.closed_at = closed_at
    january.close_id = close.id

    february_generation = AccountingPeriodAction(
        org_id=organization.id,
        action_type="period_generation",
        idempotency_key="owner-brief-period-2026-02",
        request_payload_hash="3" * 64,
        status="posted",
        input_facts={},
        missing_information=[],
        errors=[],
        confirmed_by="owner",
        confirmation_note="test",
    )
    session.add(february_generation)
    session.flush()
    session.add(
        AccountingPeriod(
            org_id=organization.id,
            calendar_id=calendar.id,
            generation_action_id=february_generation.id,
            calendar_year=2026,
            calendar_month=2,
            start_date=date(2026, 2, 1),
            end_date=date(2026, 2, 28),
            status="open",
        )
    )
    session.flush()
    return close


def test_owner_brief_empty_state_does_not_claim_external_completeness(
    session: Session,
    organization: Organization,
) -> None:
    result = OwnerBriefService(session, current_date=date(2026, 2, 20)).get(organization.id)

    assert result["status"] == "ok"
    assert result["organization"] == {
        "id": str(organization.id),
        "name": organization.name,
    }
    assert result["accounting_periods"] == {
        "open_count": 0,
        "oldest_open": None,
        "latest_closed": None,
    }
    assert result["known_work_queue"] == {
        "unmatched_bank_transactions": {
            "count": 0,
            "inflow_fen": 0,
            "outflow_fen": 0,
            "oldest_booking_date": None,
        },
        "pending_late_bank_evidence_count": 0,
        "receivables": {
            "count": 0,
            "amount_fen": 0,
            "overdue_count": 0,
            "overdue_amount_fen": 0,
        },
        "payables": {
            "count": 0,
            "amount_fen": 0,
            "overdue_count": 0,
            "overdue_amount_fen": 0,
        },
    }
    assert result["latest_posted_event"] is None
    assert result["external_materials_completeness"] == "not_established"
    assert set(result) == {
        "status",
        "generated_at",
        "organization",
        "accounting_periods",
        "known_work_queue",
        "latest_posted_event",
        "external_materials_completeness",
    }


def test_owner_brief_aggregates_known_work_and_isolates_company(
    session: Session,
    organization: Organization,
) -> None:
    close = _seed_periods(session, organization)
    customer = Counterparty(org_id=organization.id, kind="customer", name="客户甲")
    supplier = Counterparty(org_id=organization.id, kind="supplier", name="供应商乙")
    receivable_event = _event(
        organization,
        key="owner-brief-receivable",
        event_type="service_credit_sale",
        posting_date=date(2026, 1, 10),
        description="一月服务收入",
    )
    payable_event = _event(
        organization,
        key="owner-brief-payable",
        event_type="expense_payable",
        posting_date=date(2026, 2, 15),
        description="二月办公服务费",
    )
    session.add_all([customer, supplier, receivable_event, payable_event])
    session.flush()
    session.add_all(
        [
            OpenItem(
                org_id=organization.id,
                counterparty_id=customer.id,
                source_event_id=receivable_event.id,
                item_type="receivable",
                original_amount_fen=10_000,
                settled_amount_fen=2_500,
                status="partial",
                due_date=date(2026, 1, 31),
            ),
            OpenItem(
                org_id=organization.id,
                counterparty_id=supplier.id,
                source_event_id=payable_event.id,
                item_type="payable",
                original_amount_fen=8_000,
                settled_amount_fen=0,
                status="open",
                due_date=date(2026, 3, 1),
            ),
            BankTransaction(
                org_id=organization.id,
                bank_account_code="1002",
                fingerprint="owner-brief-inflow",
                booking_date=date(2026, 2, 1),
                amount_fen=15_000,
                currency="CNY",
                memo="unmatched inflow",
                source_sha256="a" * 64,
            ),
            BankTransaction(
                org_id=organization.id,
                bank_account_code="1002",
                fingerprint="owner-brief-outflow",
                booking_date=date(2026, 2, 2),
                amount_fen=-4_000,
                currency="CNY",
                memo="unmatched outflow",
                source_sha256="b" * 64,
            ),
            BankTransaction(
                org_id=organization.id,
                bank_account_code="1002",
                fingerprint="owner-brief-late",
                booking_date=date(2026, 1, 20),
                amount_fen=500,
                currency="CNY",
                memo="pending late evidence",
                source_sha256="d" * 64,
                is_late=True,
                original_close_id=close.id,
                original_close_hash=close.calculation_hash,
                original_closed_at=close.confirmed_at,
            ),
        ]
    )

    other = Organization(
        name="其他公司",
        taxpayer_identification_number="91330108MABXE0HA3F",
    )
    session.add(other)
    session.flush()
    session.add(
        BankTransaction(
            org_id=other.id,
            bank_account_code="1002",
            fingerprint="other-company-unmatched",
            booking_date=date(2026, 1, 1),
            amount_fen=999_999,
            currency="CNY",
            memo="must stay isolated",
            source_sha256="e" * 64,
        )
    )
    session.flush()

    result = OwnerBriefService(session, current_date=date(2026, 2, 20)).get(organization.id)

    assert result["accounting_periods"]["open_count"] == 1
    assert result["accounting_periods"]["oldest_open"]["period"] == "2026-02"
    assert result["accounting_periods"]["latest_closed"]["period"] == "2026-01"
    assert result["known_work_queue"] == {
        "unmatched_bank_transactions": {
            "count": 2,
            "inflow_fen": 15_000,
            "outflow_fen": 4_000,
            "oldest_booking_date": "2026-02-01",
        },
        "pending_late_bank_evidence_count": 1,
        "receivables": {
            "count": 1,
            "amount_fen": 7_500,
            "overdue_count": 1,
            "overdue_amount_fen": 7_500,
        },
        "payables": {
            "count": 1,
            "amount_fen": 8_000,
            "overdue_count": 0,
            "overdue_amount_fen": 0,
        },
    }
    assert result["latest_posted_event"] == {
        "event_type": "expense_payable",
        "posting_date": "2026-02-15",
        "description": "二月办公服务费",
    }
    money_values = (
        result["known_work_queue"]["unmatched_bank_transactions"]["inflow_fen"],
        result["known_work_queue"]["unmatched_bank_transactions"]["outflow_fen"],
        result["known_work_queue"]["receivables"]["amount_fen"],
        result["known_work_queue"]["receivables"]["overdue_amount_fen"],
        result["known_work_queue"]["payables"]["amount_fen"],
        result["known_work_queue"]["payables"]["overdue_amount_fen"],
    )
    assert all(type(value) is int for value in money_values)
    assert result["external_materials_completeness"] == "not_established"


def test_owner_brief_rejects_unknown_company(session: Session) -> None:
    assert OwnerBriefService(session).get(uuid.uuid4()) == {
        "status": "rejected",
        "errors": ["ORGANIZATION_NOT_FOUND"],
    }
