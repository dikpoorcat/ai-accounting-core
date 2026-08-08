from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BusinessEvent, Organization, TaxRule


def round_fen(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def split_tax_inclusive(gross_fen: int, rate_percent: Decimal) -> tuple[int, int]:
    if gross_fen <= 0:
        raise ValueError("gross amount must be positive")
    if rate_percent < 0:
        raise ValueError("tax rate must not be negative")
    if rate_percent == 0:
        return gross_fen, 0
    rate = rate_percent / Decimal("100")
    tax_fen = round_fen(Decimal(gross_fen) * rate / (Decimal("1") + rate))
    return gross_fen - tax_fen, tax_fen


@dataclass(frozen=True)
class TaxPeriodResult:
    start_date: date
    end_date: date
    filing_cycle: str
    threshold_fen: int
    net_sales_fen: int
    gross_sales_fen: int
    vat_accrued_fen: int
    vat_relief_fen: int
    vat_payable_fen: int
    urban_maintenance_tax_fen: int
    education_surcharge_fen: int
    local_education_surcharge_fen: int
    surtax_total_fen: int
    rule_version: str
    source_url: str
    surtax_source_url: str
    basis_source_urls: list[str]
    trace: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start_date"] = self.start_date.isoformat()
        payload["end_date"] = self.end_date.isoformat()
        return payload


def active_tax_rule(session: Session, organization: Organization, on_date: date) -> TaxRule:
    rule = session.scalar(
        select(TaxRule)
        .where(
            TaxRule.code == "small_scale_vat_2026_2027",
            TaxRule.jurisdiction == organization.jurisdiction,
            TaxRule.effective_from <= on_date,
            (TaxRule.effective_to.is_(None) | (TaxRule.effective_to >= on_date)),
        )
        .order_by(TaxRule.effective_from.desc())
    )
    if rule is None:
        raise ValueError(f"no effective small-scale VAT rule for {on_date.isoformat()}")
    return rule


def active_surtax_rule(session: Session, organization: Organization, on_date: date) -> TaxRule:
    rule = session.scalar(
        select(TaxRule)
        .where(
            TaxRule.code == "small_scale_surtax_2023_2027",
            TaxRule.jurisdiction == organization.jurisdiction,
            TaxRule.effective_from <= on_date,
            (TaxRule.effective_to.is_(None) | (TaxRule.effective_to >= on_date)),
        )
        .order_by(TaxRule.effective_from.desc())
    )
    if rule is None:
        raise ValueError(f"no effective small-scale surtax rule for {on_date.isoformat()}")
    return rule


def calculate_tax_period(
    session: Session, organization: Organization, start_date: date, end_date: date
) -> TaxPeriodResult:
    rule = active_tax_rule(session, organization, end_date)
    surtax_rule = active_surtax_rule(session, organization, end_date)
    params = rule.parameters
    surtax_params = surtax_rule.parameters
    threshold_key = f"{organization.filing_cycle}_threshold_fen"
    threshold_fen = int(params[threshold_key])

    events = session.scalars(
        select(BusinessEvent).where(
            BusinessEvent.org_id == organization.id,
            BusinessEvent.status == "posted",
            BusinessEvent.tax_obligation_date >= start_date,
            BusinessEvent.tax_obligation_date <= end_date,
        )
    ).all()
    taxable_rows: list[dict[str, Any]] = []
    for event in events:
        derived = event.facts.get("derived", {})
        if int(derived.get("taxable_gross_fen", 0)) == 0:
            continue
        taxable_rows.append(
            {
                "event_id": str(event.id),
                "gross_fen": int(derived["taxable_gross_fen"]),
                "net_fen": int(derived["net_sales_fen"]),
                "vat_fen": int(derived["vat_fen"]),
                "exemption_eligible": bool(derived.get("exemption_eligible", False)),
            }
        )

    gross_sales = sum(row["gross_fen"] for row in taxable_rows)
    net_sales = sum(row["net_fen"] for row in taxable_rows)
    vat_accrued = sum(row["vat_fen"] for row in taxable_rows)
    below_threshold = net_sales < threshold_fen
    vat_relief = max(
        0,
        (
            sum(row["vat_fen"] for row in taxable_rows if row["exemption_eligible"])
            if below_threshold
            else 0
        ),
    )
    vat_payable = max(0, vat_accrued - vat_relief)

    reduction = Decimal(str(surtax_params["small_tax_reduction_factor"]))
    urban = round_fen(Decimal(vat_payable) * organization.urban_maintenance_rate * reduction)
    education = round_fen(
        Decimal(vat_payable) * Decimal(str(surtax_params["education_surcharge_rate"])) * reduction
    )
    local_education = round_fen(
        Decimal(vat_payable)
        * Decimal(str(surtax_params["local_education_surcharge_rate"]))
        * reduction
    )
    trace = [
        {
            "rule": rule.code,
            "version": rule.version,
            "threshold_operator": "net_sales_fen < threshold_fen",
            "below_threshold": below_threshold,
            "taxable_event_count": len(taxable_rows),
        },
        {
            "rule": surtax_rule.code,
            "version": surtax_rule.version,
            "reduction_factor": str(reduction),
            "urban_maintenance_rate": str(organization.urban_maintenance_rate),
        },
        {"events": taxable_rows},
    ]
    return TaxPeriodResult(
        start_date=start_date,
        end_date=end_date,
        filing_cycle=organization.filing_cycle,
        threshold_fen=threshold_fen,
        net_sales_fen=net_sales,
        gross_sales_fen=gross_sales,
        vat_accrued_fen=vat_accrued,
        vat_relief_fen=vat_relief,
        vat_payable_fen=vat_payable,
        urban_maintenance_tax_fen=urban,
        education_surcharge_fen=education,
        local_education_surcharge_fen=local_education,
        surtax_total_fen=urban + education + local_education,
        rule_version=f"{rule.version}+{surtax_rule.version}",
        source_url=rule.source_url,
        surtax_source_url=surtax_rule.source_url,
        basis_source_urls=list(surtax_params["basis_source_urls"]),
        trace=trace,
    )
