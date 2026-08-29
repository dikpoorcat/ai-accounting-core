from __future__ import annotations

import calendar
import hashlib
import json
from dataclasses import asdict, dataclass
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import BusinessEvent, Organization, OrganizationProfileVersion, TaxRule
from .organization_profiles import profile_as_of


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


def canonical_tax_calculation_json(payload: dict[str, Any]) -> str:
    """Return the exact canonical UTF-8 text committed by a tax-period hash."""

    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def _five_place_rate(value: Decimal) -> str:
    return format(Decimal(value), ".5f")


def _rule_snapshot(rule: TaxRule) -> dict[str, Any]:
    return {
        "id": str(rule.id),
        "code": rule.code,
        "jurisdiction": rule.jurisdiction,
        "version": rule.version,
        "effective_from": rule.effective_from.isoformat(),
        "effective_to": rule.effective_to.isoformat() if rule.effective_to else None,
        "source_url": rule.source_url,
        "parameters": rule.parameters,
    }


def _below_threshold(net_sales_fen: int, threshold_fen: int, operator: str) -> bool:
    if operator == "strictly_below":
        return net_sales_fen < threshold_fen
    if operator == "at_or_below":
        return net_sales_fen <= threshold_fen
    raise ValueError("TAX_RULE_THRESHOLD_OPERATOR_INVALID")


def _threshold_expression(operator: str) -> str:
    if operator == "strictly_below":
        return "net_sales_fen < threshold_fen"
    if operator == "at_or_below":
        return "net_sales_fen <= threshold_fen"
    raise ValueError("TAX_RULE_THRESHOLD_OPERATOR_INVALID")


@dataclass(frozen=True)
class TaxPeriodResult:
    start_date: date
    end_date: date
    adjustment_posting_date: date
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
    vat_rule_id: str
    surtax_rule_id: str
    vat_rule: dict[str, Any]
    surtax_rule: dict[str, Any]
    source_events: list[dict[str, Any]]
    calculation_hash_payload: str
    calculation_hash: str
    trace: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["start_date"] = self.start_date.isoformat()
        payload["end_date"] = self.end_date.isoformat()
        payload["adjustment_posting_date"] = self.adjustment_posting_date.isoformat()
        payload["source_event_snapshots"] = payload["source_events"]
        payload["source_events"] = [row["event_id"] for row in self.source_events]
        return payload


def _active_rule(
    session: Session,
    organization: Organization | OrganizationProfileVersion,
    on_date: date,
    *,
    code: str,
    label: str,
) -> TaxRule:
    rules = session.scalars(
        select(TaxRule).where(
            TaxRule.code == code,
            TaxRule.jurisdiction == organization.jurisdiction,
            TaxRule.effective_from <= on_date,
            (TaxRule.effective_to.is_(None) | (TaxRule.effective_to >= on_date)),
        )
    ).all()
    if not rules:
        raise ValueError(f"no effective {label} rule for {on_date.isoformat()}")
    if len(rules) != 1:
        raise ValueError("TAX_RULE_AMBIGUOUS")
    return rules[0]


def active_tax_rule(
    session: Session,
    organization: Organization | OrganizationProfileVersion,
    on_date: date,
) -> TaxRule:
    return _active_rule(
        session,
        organization,
        on_date,
        code="small_scale_vat_2026_2027",
        label="small-scale VAT",
    )


def active_surtax_rule(
    session: Session,
    organization: Organization | OrganizationProfileVersion,
    on_date: date,
) -> TaxRule:
    return _active_rule(
        session,
        organization,
        on_date,
        code="small_scale_surtax_2023_2027",
        label="small-scale surtax",
    )


def _rules_overlap(first: TaxRule, second: TaxRule) -> bool:
    first_end = first.effective_to or date.max
    second_end = second.effective_to or date.max
    return first.effective_from <= second_end and second.effective_from <= first_end


def _period_rule(
    session: Session,
    organization: Organization | OrganizationProfileVersion,
    start_date: date,
    end_date: date,
    *,
    code: str,
    label: str,
) -> TaxRule:
    candidates = session.scalars(
        select(TaxRule).where(
            TaxRule.code == code,
            TaxRule.jurisdiction == organization.jurisdiction,
            TaxRule.effective_from <= end_date,
            (TaxRule.effective_to.is_(None) | (TaxRule.effective_to >= start_date)),
        )
    ).all()
    if not candidates:
        # Preserve the existing missing-rule wording for callers that already
        # classify this separately from a period spanning a known rule change.
        raise ValueError(f"no effective {label} rule for {end_date.isoformat()}")
    if any(
        _rules_overlap(first, second)
        for index, first in enumerate(candidates)
        for second in candidates[index + 1 :]
    ):
        raise ValueError("TAX_RULE_AMBIGUOUS")
    if len(candidates) != 1:
        raise ValueError("TAX_PERIOD_SPANS_RULE_CHANGE")
    rule = candidates[0]
    if rule.effective_from > start_date or (
        rule.effective_to is not None and rule.effective_to < end_date
    ):
        raise ValueError("TAX_PERIOD_SPANS_RULE_CHANGE")
    return rule


def _validate_natural_period(
    organization: Organization | OrganizationProfileVersion,
    start_date: date,
    end_date: date,
) -> None:
    if organization.filing_cycle == "monthly":
        expected_end = date(
            start_date.year,
            start_date.month,
            calendar.monthrange(start_date.year, start_date.month)[1],
        )
        valid = start_date.day == 1 and end_date == expected_end
    elif organization.filing_cycle == "quarterly":
        end_month = start_date.month + 2
        valid = start_date.day == 1 and start_date.month in {1, 4, 7, 10}
        expected_end = (
            date(
                start_date.year,
                end_month,
                calendar.monthrange(start_date.year, end_month)[1],
            )
            if valid
            else None
        )
        valid = valid and end_date == expected_end
    else:  # The database constrains this, but keep the public failure stable.
        valid = False
    if not valid:
        raise ValueError("TAX_PERIOD_INVALID_BOUNDARY")


def calculate_tax_period(
    session: Session,
    organization: Organization,
    start_date: date,
    end_date: date,
    adjustment_posting_date: date,
) -> TaxPeriodResult:
    profile = profile_as_of(
        session,
        org_id=organization.id,
        as_of=start_date,
    )
    _validate_natural_period(profile, start_date, end_date)
    if adjustment_posting_date < end_date:
        raise ValueError("TAX_PERIOD_ADJUSTMENT_POSTING_DATE_INVALID")
    rule = _period_rule(
        session,
        profile,
        start_date,
        end_date,
        code="small_scale_vat_2026_2027",
        label="small-scale VAT",
    )
    surtax_rule = _period_rule(
        session,
        profile,
        start_date,
        end_date,
        code="small_scale_surtax_2023_2027",
        label="small-scale surtax",
    )
    params = rule.parameters
    surtax_params = surtax_rule.parameters
    threshold_key = f"{profile.filing_cycle}_threshold_fen"
    threshold_fen = int(params[threshold_key])

    events = session.scalars(
        select(BusinessEvent)
        .where(
            BusinessEvent.org_id == organization.id,
            BusinessEvent.status == "posted",
            BusinessEvent.tax_obligation_date >= start_date,
            BusinessEvent.tax_obligation_date <= end_date,
        )
        .order_by(BusinessEvent.id)
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
    taxable_rows.sort(key=lambda row: row["event_id"])

    gross_sales = sum(row["gross_fen"] for row in taxable_rows)
    net_sales = sum(row["net_fen"] for row in taxable_rows)
    vat_accrued = sum(row["vat_fen"] for row in taxable_rows)
    threshold_operator = str(params["threshold_operator"])
    below_threshold = _below_threshold(net_sales, threshold_fen, threshold_operator)
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
    urban = round_fen(Decimal(vat_payable) * profile.urban_maintenance_rate * reduction)
    education = round_fen(
        Decimal(vat_payable)
        * Decimal(str(surtax_params["education_surcharge_rate"]))
        * reduction
    )
    local_education = round_fen(
        Decimal(vat_payable)
        * Decimal(str(surtax_params["local_education_surcharge_rate"]))
        * reduction
    )
    vat_snapshot = _rule_snapshot(rule)
    surtax_snapshot = _rule_snapshot(surtax_rule)
    calculation = {
        "threshold_fen": threshold_fen,
        "net_sales_fen": net_sales,
        "gross_sales_fen": gross_sales,
        "vat_accrued_fen": vat_accrued,
        "vat_relief_fen": vat_relief,
        "vat_payable_fen": vat_payable,
        "urban_maintenance_tax_fen": urban,
        "education_surcharge_fen": education,
        "local_education_surcharge_fen": local_education,
        "surtax_total_fen": urban + education + local_education,
    }
    calculation_hash_input = {
        "organization": {
            "id": str(organization.id),
            "filing_cycle": profile.filing_cycle,
            "jurisdiction": profile.jurisdiction,
            "urban_maintenance_rate": _five_place_rate(
                profile.urban_maintenance_rate
            ),
        },
        "period": {
            "start_date": start_date.isoformat(),
            "end_date": end_date.isoformat(),
            "adjustment_posting_date": adjustment_posting_date.isoformat(),
        },
        "vat_rule": vat_snapshot,
        "surtax_rule": surtax_snapshot,
        "source_events": taxable_rows,
        "calculation": calculation,
    }
    calculation_hash_payload = canonical_tax_calculation_json(calculation_hash_input)
    calculation_hash = hashlib.sha256(
        calculation_hash_payload.encode("utf-8")
    ).hexdigest()
    trace = [
        {
            "rule": rule.code,
            "version": rule.version,
            "threshold_operator": _threshold_expression(threshold_operator),
            "below_threshold": below_threshold,
            "taxable_event_count": len(taxable_rows),
            "adjustment_posting_date": adjustment_posting_date.isoformat(),
        },
        {
            "rule": surtax_rule.code,
            "version": surtax_rule.version,
            "reduction_factor": str(reduction),
            "urban_maintenance_rate": _five_place_rate(profile.urban_maintenance_rate),
        },
        {"events": taxable_rows},
        {"stage": "calculation_hash", "sha256": calculation_hash},
    ]
    return TaxPeriodResult(
        start_date=start_date,
        end_date=end_date,
        adjustment_posting_date=adjustment_posting_date,
        filing_cycle=profile.filing_cycle,
        **calculation,
        rule_version=f"{rule.version}+{surtax_rule.version}",
        source_url=rule.source_url,
        surtax_source_url=surtax_rule.source_url,
        basis_source_urls=list(surtax_params["basis_source_urls"]),
        vat_rule_id=str(rule.id),
        surtax_rule_id=str(surtax_rule.id),
        vat_rule=vat_snapshot,
        surtax_rule=surtax_snapshot,
        source_events=taxable_rows,
        calculation_hash_payload=calculation_hash_payload,
        calculation_hash=calculation_hash,
        trace=trace,
    )
