from __future__ import annotations

import uuid
from datetime import UTC, date, datetime

import pytest
from pydantic import ValidationError
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import Session

from ai_accounting.accounting_period_schemas import GenerateAccountingPeriodRequest
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.accounting_periods import canonical_json, canonical_sha256
from ai_accounting.coa import seed_organization
from ai_accounting.dashboard_brief import load_brief_dashboard
from ai_accounting.database import Base
from ai_accounting.models import (
    Account,
    AccountingPeriod,
    AccountingPeriodAction,
    AccountingPeriodClose,
    AccountingPeriodCloseCommentary,
    AuditLog,
)
from ai_accounting.period_commentary import (
    BackfillPeriodCommentaryRequest,
    PeriodCommentaryService,
    PreviewPeriodCommentaryRequest,
)


@pytest.fixture
def historical_close():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        org = seed_organization(
            session, name="历史结论测试企业", taxpayer_identification_number="91330106MA1234567T"
        )
        generated = AccountingPeriodService(session).generate_accounting_period(
            GenerateAccountingPeriodRequest(
                org_id=org.id, period_month="2026-08", idempotency_key="generate-august"
            )
        )
        period = session.get(AccountingPeriod, generated.period_id)
        revenue = session.scalar(
            select(Account).where(Account.org_id == org.id, Account.category == "revenue")
        )
        expense = session.scalar(
            select(Account).where(Account.org_id == org.id, Account.category == "expense")
        )
        calculation = {
            "account_totals": [
                {"id": str(revenue.id), "credit_fen": 20000, "debit_fen": 0},
                {"id": str(expense.id), "credit_fen": 0, "debit_fen": 9000},
            ]
        }
        # Reproduce the persisted shape of a historical close made without commentary.
        action = AccountingPeriodAction(
            org_id=org.id,
            action_type="period_close",
            idempotency_key="legacy-close",
            request_payload_hash="a" * 64,
            status="posted",
            input_facts={},
        )
        session.add(action)
        session.flush()
        closed_at = datetime(2026, 9, 1, tzinfo=UTC)
        close = AccountingPeriodClose(
            org_id=org.id,
            period_id=period.id,
            action_id=action.id,
            calculation=calculation,
            calculation_payload=canonical_json(calculation),
            calculation_hash=canonical_sha256(calculation),
            rule_version="test",
            rule_effective_from=date(2026, 1, 1),
            source_urls=[],
            checker_version="test",
            confirmed_at=closed_at,
            voucher_count=0,
            line_count=0,
            total_debit_fen=0,
            total_credit_fen=0,
        )
        session.add(close)
        session.flush()
        period.status = "closed"
        period.close_id = close.id
        period.closed_at = closed_at
        session.commit()
        yield session, period, close
    engine.dispose()


def _request(session, period):
    preview = PeriodCommentaryService(session).preview(
        PreviewPeriodCommentaryRequest(org_id=period.org_id, period_id=period.id)
    )
    return BackfillPeriodCommentaryRequest(
        org_id=period.org_id,
        period_id=period.id,
        close_id=preview["close_id"],
        context_hash=preview["context_hash"],
        commentary="本月收入覆盖费用，实现盈利。",
    )


def test_backfill_uses_frozen_amounts_and_preserves_close_and_is_idempotent(historical_close):
    session, period, close = historical_close
    before = (close.calculation_payload, close.calculation_hash, period.closed_at, close.action_id)
    request = _request(session, period)
    service = PeriodCommentaryService(session)
    posted = service.backfill(request)
    session.commit()
    stored = session.scalars(select(AccountingPeriodCloseCommentary)).one()
    assert stored.context_payload["current_period"]["result_fen"] == 11000
    assert stored.generation_method == "historical_ai_backfill"
    assert stored.context_payload["source_basis"] == "immutable_close_snapshots"
    assert (
        close.calculation_payload,
        close.calculation_hash,
        period.closed_at,
        close.action_id,
    ) == before
    assert period.status == "closed"
    repeated = service.backfill(request)
    assert repeated["commentary_id"] == posted["commentary_id"]
    assert repeated["idempotent_replay"] is True
    assert len(session.scalars(select(AuditLog)).all()) == 1
    assert service.backfill(request.model_copy(update={"commentary": "替换结论"}))["errors"] == [
        "ACCOUNTING_PERIOD_COMMENTARY_ALREADY_EXISTS"
    ]
    preview = service.preview(
        PreviewPeriodCommentaryRequest(org_id=period.org_id, period_id=period.id)
    )
    assert preview["can_backfill"] is False
    assert preview["commentary"] == request.commentary
    session.commit()
    brief = load_brief_dashboard(session.get_bind(), org_id=period.org_id, period_key="2026-08")
    assert brief["data"]["management_commentary"] == request.commentary


@pytest.mark.parametrize(
    "change,code",
    [
        ({"context_hash": "0" * 64}, "ACCOUNTING_PERIOD_COMMENTARY_CONTEXT_STALE"),
        ({"close_id": uuid.uuid4()}, "ACCOUNTING_PERIOD_COMMENTARY_CLOSE_MISMATCH"),
        ({"org_id": uuid.uuid4()}, "ACCOUNTING_PERIOD_CLOSED_SNAPSHOT_REQUIRED"),
        ({"period_id": uuid.uuid4()}, "ACCOUNTING_PERIOD_CLOSED_SNAPSHOT_REQUIRED"),
    ],
)
def test_backfill_rejects_stale_and_cross_company_targets(historical_close, change, code):
    session, period, close = historical_close
    result = PeriodCommentaryService(session).backfill(
        _request(session, period).model_copy(update=change)
    )
    assert result["errors"] == [code]
    assert session.scalars(select(AccountingPeriodCloseCommentary)).all() == []
    assert session.scalars(select(AuditLog)).all() == []
    assert period.close_id == close.id


def test_backfill_rejects_open_period(historical_close):
    session, period, _close = historical_close
    request = _request(session, period)
    generated = AccountingPeriodService(session).generate_accounting_period(
        GenerateAccountingPeriodRequest(
            org_id=period.org_id, period_month="2026-09", idempotency_key="generate-open-month"
        )
    )
    assert PeriodCommentaryService(session).backfill(
        request.model_copy(update={"period_id": generated.period_id})
    )["errors"] == ["ACCOUNTING_PERIOD_CLOSED_SNAPSHOT_REQUIRED"]


def test_backfill_rolls_back_commentary_if_audit_write_fails(historical_close):
    session, period, _close = historical_close
    request = _request(session, period)

    def fail_audit(session, _context, _instances):
        if any(isinstance(row, AuditLog) for row in session.new):
            raise RuntimeError("audit write failed")

    event.listen(session, "before_flush", fail_audit)
    try:
        with pytest.raises(RuntimeError, match="audit write failed"):
            PeriodCommentaryService(session).backfill(request)
    finally:
        event.remove(session, "before_flush", fail_audit)
    assert session.scalars(select(AccountingPeriodCloseCommentary)).all() == []
    assert period.status == "closed"


@pytest.mark.parametrize("text", ["", " \n\t", "\u3000"])
def test_backfill_rejects_blank_text(historical_close, text):
    session, period, _close = historical_close
    request = _request(session, period).model_dump() | {"commentary": text}
    with pytest.raises(ValidationError):
        BackfillPeriodCommentaryRequest.model_validate(request)
