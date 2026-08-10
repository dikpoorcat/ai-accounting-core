"""Pure deterministic fixed-asset calculations.

This module deliberately has no database or journal-entry dependency.  Services
provide immutable asset facts and use these calculations to derive their fixed
posting templates.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import ROUND_HALF_UP, Decimal
from typing import Any

SMALL_ENTERPRISE_FIXED_ASSET_RULE_VERSION = "small_enterprise_fixed_asset_straight_line_2013.1"
SMALL_SCALE_USED_FIXED_ASSET_VAT_RULE_VERSION = "small_scale_used_fixed_asset_vat_2026.1"


class FixedAssetCalculationError(ValueError):
    """A deterministic fixed-asset calculation cannot be performed."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class AcquisitionCostResult:
    purchase_price_fen: int
    noncreditable_tax_fen: int
    transport_and_handling_fen: int
    installation_and_direct_cost_fen: int
    cost_fen: int


@dataclass(frozen=True)
class StraightLineDepreciationResult:
    cost_fen: int
    residual_value_fen: int
    depreciable_fen: int
    useful_life_months: int
    completed_months: int
    opening_accumulated_depreciation_fen: int
    base_monthly_fen: int
    depreciation_fen: int
    closing_accumulated_depreciation_fen: int
    is_final_month: bool


@dataclass(frozen=True)
class UsedFixedAssetVatResult:
    gross_proceeds_fen: int
    tax_sales_fen: int
    vat_fen: int
    taxable_rate: Decimal
    vat_rate: Decimal


def _require_fen(value: object, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise FixedAssetCalculationError("INVALID_FEN", f"{field} must be an integer fen amount")
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise FixedAssetCalculationError("INVALID_FEN", f"{field} must be {qualifier}")
    return value


def calculate_acquisition_cost(
    *,
    purchase_price_fen: int,
    noncreditable_tax_fen: int,
    transport_and_handling_fen: int,
    installation_and_direct_cost_fen: int,
) -> AcquisitionCostResult:
    """Add the only capitalisable Phase-1 cost components in integer fen."""

    components = {
        "purchase_price_fen": _require_fen(purchase_price_fen, "purchase_price_fen"),
        "noncreditable_tax_fen": _require_fen(noncreditable_tax_fen, "noncreditable_tax_fen"),
        "transport_and_handling_fen": _require_fen(
            transport_and_handling_fen, "transport_and_handling_fen"
        ),
        "installation_and_direct_cost_fen": _require_fen(
            installation_and_direct_cost_fen, "installation_and_direct_cost_fen"
        ),
    }
    cost_fen = sum(components.values())
    if cost_fen <= 0:
        raise FixedAssetCalculationError(
            "FIXED_ASSET_COST_MUST_BE_POSITIVE", "cost_fen must be positive"
        )
    return AcquisitionCostResult(**components, cost_fen=cost_fen)


def calculate_straight_line_depreciation(
    *,
    cost_fen: int,
    residual_value_fen: int,
    useful_life_months: int,
    completed_months: int,
    opening_accumulated_depreciation_fen: int,
) -> StraightLineDepreciationResult:
    """Calculate one continuous month of integer-fen straight-line depreciation.

    ``completed_months`` is the number of valid, unreversed depreciation
    periods preceding the period being calculated.  The last month receives
    every remainder so total depreciation closes exactly to depreciable cost.
    """

    cost = _require_fen(cost_fen, "cost_fen", positive=True)
    residual = _require_fen(residual_value_fen, "residual_value_fen")
    if residual >= cost:
        raise FixedAssetCalculationError(
            "FIXED_ASSET_INVALID_RESIDUAL_VALUE",
            "residual_value_fen must be less than cost_fen",
        )
    if isinstance(useful_life_months, bool) or not isinstance(useful_life_months, int):
        raise FixedAssetCalculationError(
            "FIXED_ASSET_INVALID_USEFUL_LIFE", "useful_life_months must be an integer"
        )
    if useful_life_months <= 0:
        raise FixedAssetCalculationError(
            "FIXED_ASSET_INVALID_USEFUL_LIFE", "useful_life_months must be positive"
        )
    if isinstance(completed_months, bool) or not isinstance(completed_months, int):
        raise FixedAssetCalculationError(
            "FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE", "completed_months must be an integer"
        )
    if completed_months < 0 or completed_months >= useful_life_months:
        raise FixedAssetCalculationError(
            "FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE",
            "completed_months must identify an unposted depreciation month",
        )
    opening = _require_fen(
        opening_accumulated_depreciation_fen, "opening_accumulated_depreciation_fen"
    )
    depreciable_fen = cost - residual
    if depreciable_fen < useful_life_months:
        raise FixedAssetCalculationError(
            "FIXED_ASSET_INVALID_DEPRECIATION_POLICY",
            "depreciable_fen must cover at least one fen in every useful-life month",
        )
    base_monthly_fen = depreciable_fen // useful_life_months
    expected_opening = base_monthly_fen * completed_months
    if opening != expected_opening:
        raise FixedAssetCalculationError(
            "FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE",
            "opening accumulated depreciation does not match continuous history",
        )
    is_final_month = completed_months == useful_life_months - 1
    depreciation_fen = (
        depreciable_fen - opening if is_final_month else base_monthly_fen
    )
    closing = opening + depreciation_fen
    return StraightLineDepreciationResult(
        cost_fen=cost,
        residual_value_fen=residual,
        depreciable_fen=depreciable_fen,
        useful_life_months=useful_life_months,
        completed_months=completed_months,
        opening_accumulated_depreciation_fen=opening,
        base_monthly_fen=base_monthly_fen,
        depreciation_fen=depreciation_fen,
        closing_accumulated_depreciation_fen=closing,
        is_final_month=is_final_month,
    )


def _round_fen(value: Decimal) -> int:
    return int(value.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def calculate_used_fixed_asset_vat(*, gross_proceeds_fen: int) -> UsedFixedAssetVatResult:
    """Apply the 2026 used-fixed-asset 3%-base / 2%-VAT rule in integer fen."""

    gross = _require_fen(gross_proceeds_fen, "gross_proceeds_fen", positive=True)
    taxable_rate = Decimal("0.03")
    vat_rate = Decimal("0.02")
    tax_sales_fen = _round_fen(Decimal(gross) / (Decimal("1") + taxable_rate))
    vat_fen = _round_fen(Decimal(tax_sales_fen) * vat_rate)
    return UsedFixedAssetVatResult(
        gross_proceeds_fen=gross,
        tax_sales_fen=tax_sales_fen,
        vat_fen=vat_fen,
        taxable_rate=taxable_rate,
        vat_rate=vat_rate,
    )


def fixed_asset_calculation_hash(
    *, command: str, request: Mapping[str, Any], calculation: Mapping[str, Any] | object
) -> str:
    """Hash command, caller facts, and calculated result with canonical JSON."""

    if not command:
        raise FixedAssetCalculationError("INVALID_FIXED_ASSET_COMMAND", "command is required")
    if hasattr(calculation, "model_dump"):
        normalized_calculation = calculation.model_dump(mode="json")
    elif hasattr(calculation, "__dataclass_fields__"):
        normalized_calculation = asdict(calculation)
    else:
        normalized_calculation = calculation
    payload = {"command": command, "request": request, "calculation": normalized_calculation}
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise FixedAssetCalculationError(
            "INVALID_FIXED_ASSET_CALCULATION_HASH_INPUT", "hash input is not canonical JSON"
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime, uuid.UUID)):
        return value.isoformat() if isinstance(value, (date, datetime)) else str(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")
