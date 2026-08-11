from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ai_accounting.models import (
    BusinessEvent,
    OpenItem,
    Organization,
    TaxPeriod,
    Voucher,
    VoucherLine,
)
from ai_accounting.schemas import (
    RecordEventRequest,
    ReverseEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)
from ai_accounting.service import FinanceService

TAX_FACT_FIELDS = (
    "taxable",
    "rate_percent",
    "invoice_type",
    "waive_exemption",
    "tax_due_on_event",
)


def _sale_payload(
    organization: Organization,
    *,
    key: str,
    business_date: date = date(2026, 1, 15),
    gross_fen: int = 10_100,
    tax_facts: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "org_id": organization.id,
        "idempotency_key": key,
        "event_type": "service_cash_sale",
        "business_dates": {
            "business_date": business_date,
            "fulfillment_date": business_date,
            "payment_date": business_date,
            "tax_obligation_date": business_date,
            "posting_date": business_date,
        },
        "amounts": {"gross_amount_fen": gross_fen},
        "tax_facts": tax_facts,
    }


def _explicit_tax_facts(*, invoice_type: str = "ordinary") -> dict[str, object]:
    return {
        "taxable": True,
        "rate_percent": "1",
        "invoice_type": invoice_type,
        "waive_exemption": False,
        "tax_due_on_event": True,
    }


def _record(service: FinanceService, payload: dict[str, object]):
    return service.record_event(RecordEventRequest.model_validate(payload))


def _preview(
    service: FinanceService, organization: Organization, start: date, end: date
) -> dict[str, object]:
    return service.preview_tax_period(
        TaxPeriodPreviewRequest(org_id=organization.id, start_date=start, end_date=end)
    )


def _confirm(
    service: FinanceService,
    organization: Organization,
    start: date,
    end: date,
    calculation_hash: str,
    key: str,
):
    return service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=start,
            end_date=end,
            calculation_hash=calculation_hash,
            idempotency_key=key,
        )
    )


def _count(session: Session, model: type[object]) -> int:
    return int(session.scalar(select(func.count()).select_from(model)) or 0)


@pytest.mark.parametrize("missing_field", TAX_FACT_FIELDS)
def test_tax_fact_each_required_field_is_needs_information_without_formal_side_effects(
    session: Session, organization: Organization, missing_field: str
) -> None:
    """Tax facts are facts to request, never accounting defaults to infer."""

    facts = _explicit_tax_facts()
    del facts[missing_field]
    before = (_count(session, Voucher), _count(session, OpenItem))

    result = _record(
        FinanceService(session),
        _sale_payload(organization, key=f"tax-fact-missing-{missing_field}", tax_facts=facts),
    )

    assert result.status.value == "needs_information"
    assert result.missing_information == [f"tax_facts.{missing_field}"]
    assert (_count(session, Voucher), _count(session, OpenItem)) == before
    rejected = session.get(BusinessEvent, result.event_id)
    assert rejected is not None
    assert rejected.status == "needs_information"
    assert rejected.facts.get("derived") is None


def test_empty_tax_facts_lists_all_five_fields_and_does_not_formally_post(
    session: Session, organization: Organization
) -> None:
    before = (_count(session, Voucher), _count(session, OpenItem))

    result = _record(
        FinanceService(session),
        _sale_payload(organization, key="tax-facts-empty", tax_facts={}),
    )

    assert result.status.value == "needs_information"
    assert result.missing_information == [f"tax_facts.{field}" for field in TAX_FACT_FIELDS]
    assert (_count(session, Voucher), _count(session, OpenItem)) == before
    decision = session.get(BusinessEvent, result.event_id)
    assert decision is not None
    assert decision.status == "needs_information"
    assert not decision.vouchers


@pytest.mark.parametrize(
    ("event_type", "extra"),
    [
        ("expense_cash", {}),
        ("expense_payable", {"counterparty": {"kind": "supplier", "name": "费用供应商"}}),
        (
            "employee_reimbursement",
            {
                "counterparty": {"kind": "employee", "name": "报销员工"},
                "details": {"paid_now": True},
            },
        ),
    ],
)
def test_expense_account_role_is_required_only_for_expense_events(
    session: Session,
    organization: Organization,
    event_type: str,
    extra: dict[str, object],
) -> None:
    payload: dict[str, object] = {
        "org_id": organization.id,
        "idempotency_key": f"missing-expense-role-{event_type}",
        "event_type": event_type,
        "business_dates": {
            "business_date": date(2026, 1, 15),
            "payment_date": date(2026, 1, 15),
            "posting_date": date(2026, 1, 15),
        },
        "amounts": {"amount_fen": 1_000},
        **extra,
    }
    result = _record(FinanceService(session), payload)
    assert result.status.value == "needs_information"
    assert result.missing_information == ["amounts.expense_account_role"]
    assert _count(session, Voucher) == 0

    non_expense = _record(
        FinanceService(session),
        _sale_payload(
            organization,
            key=f"role-is-irrelevant-{event_type}",
            tax_facts=_explicit_tax_facts(),
        ),
    )
    assert non_expense.status.value == "posted"


def test_explicit_tax_facts_keep_balanced_voucher_and_tax_derivation(
    session: Session, organization: Organization
) -> None:
    result = _record(
        FinanceService(session),
        _sale_payload(
            organization,
            key="explicit-tax-facts-balanced",
            tax_facts=_explicit_tax_facts(invoice_type="special"),
        ),
    )
    assert result.status.value == "posted"
    event = session.get(BusinessEvent, result.event_id)
    assert event is not None
    assert event.facts["derived"] == {
        "taxable_gross_fen": 10_100,
        "net_sales_fen": 10_000,
        "vat_fen": 100,
        "exemption_eligible": False,
    }
    voucher = session.get(Voucher, result.voucher_id)
    assert voucher is not None
    lines = session.scalars(select(VoucherLine).where(VoucherLine.voucher_id == voucher.id)).all()
    assert sum(line.debit_fen for line in lines) == sum(line.credit_fen for line in lines)
    assert sum(line.debit_fen for line in lines) == 10_100
    assert {item["stage"] for item in event.rule_trace} >= {"facts_validated", "entries_derived"}


@pytest.mark.parametrize(
    ("filing_cycle", "valid_start", "valid_end", "invalid_end"),
    [
        ("monthly", date(2026, 1, 1), date(2026, 1, 31), date(2026, 1, 30)),
        ("quarterly", date(2026, 1, 1), date(2026, 3, 31), date(2026, 3, 30)),
    ],
)
def test_tax_period_requires_complete_natural_month_or_quarter(
    session: Session,
    organization: Organization,
    filing_cycle: str,
    valid_start: date,
    valid_end: date,
    invalid_end: date,
) -> None:
    organization.filing_cycle = filing_cycle
    session.flush()
    service = FinanceService(session)

    valid = _preview(service, organization, valid_start, valid_end)
    assert valid["status"] == "calculated", valid
    invalid = _preview(service, organization, valid_start, invalid_end)
    assert invalid["status"] == "rejected"
    assert invalid["errors"] == ["TAX_PERIOD_INVALID_BOUNDARY"]


def test_confirm_rejects_stale_hash_after_source_fact_changes(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    _record(
        service,
        _sale_payload(
            organization,
            key="stale-source-one",
            tax_facts=_explicit_tax_facts(invoice_type="special"),
        ),
    )
    preview = _preview(service, organization, date(2026, 1, 1), date(2026, 3, 31))
    _record(
        service,
        _sale_payload(
            organization,
            key="stale-source-two",
            business_date=date(2026, 2, 15),
            tax_facts=_explicit_tax_facts(invoice_type="special"),
        ),
    )

    stale = _confirm(
        service,
        organization,
        date(2026, 1, 1),
        date(2026, 3, 31),
        str(preview["calculation_hash"]),
        "confirm-stale-preview",
    )
    assert stale.status.value == "rejected"
    assert stale.errors == ["TAX_PERIOD_CALCULATION_STALE"]
    assert _count(session, TaxPeriod) == 0


def test_tax_confirmation_replay_conflict_and_same_period_are_stable(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    _record(
        service,
        _sale_payload(
            organization,
            key="confirmation-replay-source",
            tax_facts=_explicit_tax_facts(invoice_type="special"),
        ),
    )
    start, end = date(2026, 1, 1), date(2026, 3, 31)
    preview = _preview(service, organization, start, end)
    first = _confirm(
        service, organization, start, end, str(preview["calculation_hash"]), "confirm-once"
    )
    replay = _confirm(
        service, organization, start, end, str(preview["calculation_hash"]), "confirm-once"
    )
    mismatch = _confirm(service, organization, start, end, "f" * 64, "confirm-once")
    duplicate = _confirm(
        service, organization, start, end, str(preview["calculation_hash"]), "confirm-new-key"
    )

    assert first.status.value == "posted"
    assert replay.status.value == "posted"
    assert replay.event_id == first.event_id
    assert replay.voucher_id == first.voucher_id
    assert mismatch.status.value == "rejected"
    assert mismatch.errors == ["TAX_PERIOD_IDEMPOTENCY_PAYLOAD_MISMATCH"]
    assert duplicate.status.value == "rejected"
    assert duplicate.errors == ["TAX_PERIOD_ALREADY_POSTED"]


def test_tax_confirmation_detects_overlap_after_organization_cycle_changes(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    _record(
        service,
        _sale_payload(
            organization,
            key="overlap-source",
            tax_facts=_explicit_tax_facts(invoice_type="special"),
        ),
    )
    quarterly = _preview(service, organization, date(2026, 1, 1), date(2026, 3, 31))
    posted = _confirm(
        service,
        organization,
        date(2026, 1, 1),
        date(2026, 3, 31),
        str(quarterly["calculation_hash"]),
        "confirm-quarter",
    )
    assert posted.status.value == "posted"

    organization.filing_cycle = "monthly"
    session.flush()
    january = _preview(service, organization, date(2026, 1, 1), date(2026, 1, 31))
    overlap = _confirm(
        service,
        organization,
        date(2026, 1, 1),
        date(2026, 1, 31),
        str(january["calculation_hash"]),
        "confirm-overlap-january",
    )
    assert overlap.status.value == "rejected"
    assert overlap.errors == ["TAX_PERIOD_OVERLAP"]


def test_source_correction_requires_reversing_tax_period_first(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    source = _record(
        service,
        _sale_payload(
            organization,
            key="locked-source",
            tax_facts=_explicit_tax_facts(invoice_type="special"),
        ),
    )
    assert source.status.value == "posted"
    start, end = date(2026, 1, 1), date(2026, 3, 31)
    preview = _preview(service, organization, start, end)
    confirmed = _confirm(
        service, organization, start, end, str(preview["calculation_hash"]), "lock-period"
    )
    assert confirmed.status.value == "posted"

    blocked_new_source = _record(
        service,
        _sale_payload(
            organization,
            key="source-while-locked",
            business_date=date(2026, 2, 15),
            tax_facts=_explicit_tax_facts(invoice_type="special"),
        ),
    )
    blocked_reversal = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=source.event_id,
            idempotency_key="reverse-source-while-locked",
            reason="验收期间来源更正",
            posting_date=date(2026, 4, 1),
        )
    )
    assert blocked_new_source.status.value == "rejected"
    assert blocked_new_source.errors == ["TAX_PERIOD_SOURCE_LOCKED"]
    assert blocked_reversal.status.value == "rejected"
    assert blocked_reversal.errors == ["TAX_PERIOD_SOURCE_LOCKED"]

    period_reversal = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=confirmed.event_id,
            idempotency_key="reverse-tax-period-before-correction",
            reason="先冲正税期调整",
            posting_date=date(2026, 4, 1),
        )
    )
    assert period_reversal.status.value == "posted"
    source_reversal = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=source.event_id,
            idempotency_key="reverse-source-after-period",
            reason="税期冲正后更正来源",
            posting_date=date(2026, 4, 1),
        )
    )
    assert source_reversal.status.value == "posted"
    corrected = _record(
        service,
        _sale_payload(
            organization,
            key="corrected-source-after-period-reversal",
            business_date=date(2026, 1, 20),
            gross_fen=20_200,
            tax_facts=_explicit_tax_facts(invoice_type="special"),
        ),
    )
    assert corrected.status.value == "posted"
    recalculated = _preview(service, organization, start, end)
    reconfirmed = _confirm(
        service,
        organization,
        start,
        end,
        str(recalculated["calculation_hash"]),
        "confirm-corrected-period",
    )
    assert reconfirmed.status.value == "posted", reconfirmed.errors
    original_period = session.scalar(
        select(TaxPeriod).where(TaxPeriod.adjustment_event_id == confirmed.event_id)
    )
    assert original_period is not None
    assert original_period.status == "reversed"
