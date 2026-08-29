from __future__ import annotations

import hashlib
import json
import uuid
from datetime import date
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.models import (
    BusinessEvent,
    Organization,
    TaxPeriod,
    TaxPeriodSource,
    Voucher,
    ZeroTaxPeriodConfirmation,
)
from ai_accounting.schemas import (
    ReverseEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)
from ai_accounting.service import FinanceService
from ai_accounting.tax import (
    _below_threshold,
    _threshold_expression,
    active_surtax_rule,
    active_tax_rule,
    calculate_tax_period,
    canonical_tax_calculation_json,
    split_tax_inclusive,
)


def test_historical_tax_rule_versions_are_selected_by_effective_date(
    session: Session, organization: Organization
) -> None:
    assert active_tax_rule(session, organization, date(2022, 9, 20)).version == "2022.15"
    assert active_surtax_rule(session, organization, date(2022, 9, 20)).version == (
        "2022.10-ZJ.4"
    )
    assert active_tax_rule(session, organization, date(2025, 6, 30)).version == "2023.19"
    assert active_tax_rule(session, organization, date(2026, 6, 30)).version == "2026.1"


def test_threshold_operator_preserves_historical_inclusive_boundary() -> None:
    assert _below_threshold(10_000_000, 10_000_000, "at_or_below") is True
    assert _threshold_expression("at_or_below") == "net_sales_fen <= threshold_fen"
    assert _below_threshold(10_000_000, 10_000_000, "strictly_below") is False
    assert _threshold_expression("strictly_below") == "net_sales_fen < threshold_fen"


def add_taxable_event(
    session: Session,
    organization: Organization,
    *,
    net_fen: int,
    vat_fen: int,
    exemption_eligible: bool = True,
) -> None:
    session.add(
        BusinessEvent(
            org_id=organization.id,
            idempotency_key=f"tax-{uuid.uuid4()}",
            event_type="service_cash_sale",
            status="posted",
            description="threshold fixture",
            facts={
                "derived": {
                    "taxable_gross_fen": net_fen + vat_fen,
                    "net_sales_fen": net_fen,
                    "vat_fen": vat_fen,
                    "exemption_eligible": exemption_eligible,
                }
            },
            business_date=date(2026, 3, 31),
            tax_obligation_date=date(2026, 3, 31),
            posting_date=date(2026, 3, 31),
            rule_trace=[],
        )
    )
    session.flush()


def test_quarterly_threshold_is_strictly_below(
    session: Session, organization: Organization
) -> None:
    add_taxable_event(session, organization, net_fen=29_999_999, vat_fen=300_000)
    below = calculate_tax_period(
        session, organization, date(2026, 1, 1), date(2026, 3, 31), date(2026, 3, 31)
    )
    assert below.net_sales_fen == 29_999_999
    assert below.vat_relief_fen == 300_000
    assert below.vat_payable_fen == 0

    add_taxable_event(session, organization, net_fen=1, vat_fen=0)
    reached = calculate_tax_period(
        session, organization, date(2026, 1, 1), date(2026, 3, 31), date(2026, 3, 31)
    )
    assert reached.net_sales_fen == 30_000_000
    assert reached.vat_relief_fen == 0
    assert reached.vat_payable_fen == 300_000


def test_special_invoice_is_not_relieved_below_threshold(
    session: Session, organization: Organization
) -> None:
    add_taxable_event(
        session,
        organization,
        net_fen=1_000_000,
        vat_fen=10_000,
        exemption_eligible=False,
    )
    result = calculate_tax_period(
        session, organization, date(2026, 1, 1), date(2026, 3, 31), date(2026, 3, 31)
    )
    assert result.vat_relief_fen == 0
    assert result.vat_payable_fen == 10_000
    assert result.surtax_total_fen > 0


def test_tax_hash_payload_is_exported_reproducible_and_reload_stable(
    session: Session, organization: Organization
) -> None:
    add_taxable_event(session, organization, net_fen=1_000_000, vat_fen=10_000)
    first = calculate_tax_period(
        session, organization, date(2026, 1, 1), date(2026, 3, 31), date(2026, 3, 31)
    )
    decoded = json.loads(first.calculation_hash_payload)
    assert decoded["organization"]["urban_maintenance_rate"] == "0.07000"
    assert canonical_tax_calculation_json(decoded) == first.calculation_hash_payload
    assert hashlib.sha256(first.calculation_hash_payload.encode("utf-8")).hexdigest() == (
        first.calculation_hash
    )

    session.flush()
    session.expire(organization, ["urban_maintenance_rate"])
    reloaded = calculate_tax_period(
        session, organization, date(2026, 1, 1), date(2026, 3, 31), date(2026, 3, 31)
    )
    assert reloaded.calculation_hash_payload == first.calculation_hash_payload
    assert reloaded.calculation_hash == first.calculation_hash


def test_zero_adjustment_records_confirmation_without_event_or_voucher(
    session: Session, organization: Organization
) -> None:
    service = FinanceService(session)
    preview = service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
        )
    )
    before_events = session.query(BusinessEvent).count()
    result = service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
            calculation_hash=preview["calculation_hash"],
            idempotency_key="zero-tax-adjustment",
        )
    )
    assert result.status == "posted"
    assert result.event_id is None
    assert result.voucher_id is None
    assert result.data["no_accounting_adjustment"] is True
    assert result.data["idempotent_replay"] is False
    confirmation = session.get(
        ZeroTaxPeriodConfirmation,
        uuid.UUID(result.data["zero_tax_period_confirmation_id"]),
    )
    assert confirmation is not None
    assert confirmation.calculation_hash == preview["calculation_hash"]
    assert session.query(BusinessEvent).count() == before_events
    assert session.query(Voucher).count() == 0
    assert session.query(TaxPeriod).count() == 0
    assert session.query(TaxPeriodSource).count() == 0

    replay = service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
            calculation_hash=preview["calculation_hash"],
            idempotency_key="zero-tax-adjustment",
        )
    )
    assert replay.status == "posted"
    assert replay.data["idempotent_replay"] is True
    assert replay.data["zero_tax_period_confirmation_id"] == str(confirmation.id)


def test_tax_period_adjustment_cannot_be_posted_twice(
    session: Session, organization: Organization
) -> None:
    add_taxable_event(session, organization, net_fen=1_000_000, vat_fen=10_000)
    service = FinanceService(session)
    preview = service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
        )
    )
    first = service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
            calculation_hash=preview["calculation_hash"],
            idempotency_key="tax-q1-first",
        )
    )
    assert first.status == "posted"
    period = session.scalar(
        select(TaxPeriod).where(TaxPeriod.adjustment_event_id == first.event_id)
    )
    assert period.calculation_hash_payload == preview["calculation_hash_payload"]
    assert period.filing_cycle_snapshot == organization.filing_cycle
    assert period.jurisdiction_snapshot == organization.jurisdiction
    assert period.urban_maintenance_rate_snapshot == Decimal("0.07000")

    second = service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
            calculation_hash=preview["calculation_hash"],
            idempotency_key="tax-q1-different-key",
        )
    )
    assert second.status == "rejected"
    assert second.errors == ["TAX_PERIOD_ALREADY_POSTED"]

    reversed_result = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=first.event_id,
            idempotency_key="reverse-tax-q1",
            reason="更正期间税务事实",
            posting_date=date(2026, 4, 1),
        )
    )
    assert reversed_result.status == "posted"
    refreshed = service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
        )
    )
    reposted = service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
            calculation_hash=refreshed["calculation_hash"],
            idempotency_key="tax-q1-after-reversal",
        )
    )
    assert reposted.status == "posted"


@given(
    gross_fen=st.integers(min_value=1, max_value=1_000_000_000),
    rate=st.sampled_from([Decimal("0"), Decimal("1"), Decimal("3")]),
)
@settings(max_examples=100)
def test_tax_split_is_conservative(gross_fen: int, rate: Decimal) -> None:
    net, tax = split_tax_inclusive(gross_fen, rate)
    assert net >= 0
    assert tax >= 0
    assert net + tax == gross_fen
