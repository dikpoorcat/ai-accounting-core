from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st
from sqlalchemy.orm import Session

from ai_accounting.models import BusinessEvent, Organization
from ai_accounting.schemas import ReverseEventRequest, TaxPeriodRequest
from ai_accounting.service import FinanceService
from ai_accounting.tax import calculate_tax_period, split_tax_inclusive


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
    below = calculate_tax_period(session, organization, date(2026, 1, 1), date(2026, 3, 31))
    assert below.net_sales_fen == 29_999_999
    assert below.vat_relief_fen == 300_000
    assert below.vat_payable_fen == 0

    add_taxable_event(session, organization, net_fen=1, vat_fen=0)
    reached = calculate_tax_period(session, organization, date(2026, 1, 1), date(2026, 3, 31))
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
    result = calculate_tax_period(session, organization, date(2026, 1, 1), date(2026, 3, 31))
    assert result.vat_relief_fen == 0
    assert result.vat_payable_fen == 10_000
    assert result.surtax_total_fen > 0


def test_tax_period_adjustment_cannot_be_posted_twice(
    session: Session, organization: Organization
) -> None:
    add_taxable_event(session, organization, net_fen=1_000_000, vat_fen=10_000)
    service = FinanceService(session)
    first = service.calculate_tax(
        TaxPeriodRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            post_adjustment=True,
            idempotency_key="tax-q1-first",
        )
    )
    assert first["posting"]["status"] == "posted"

    second = service.calculate_tax(
        TaxPeriodRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            post_adjustment=True,
            idempotency_key="tax-q1-different-key",
        )
    )
    assert second["posting"]["status"] == "rejected"
    assert second["posting"]["errors"] == ["TAX_PERIOD_ALREADY_POSTED"]

    reversed_result = service.reverse_event(
        ReverseEventRequest(
            org_id=organization.id,
            event_id=first["posting"]["event_id"],
            idempotency_key="reverse-tax-q1",
            reason="更正期间税务事实",
            posting_date=date(2026, 4, 1),
        )
    )
    assert reversed_result.status == "posted"
    reposted = service.calculate_tax(
        TaxPeriodRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            post_adjustment=True,
            idempotency_key="tax-q1-after-reversal",
        )
    )
    assert reposted["posting"]["status"] == "posted"


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
