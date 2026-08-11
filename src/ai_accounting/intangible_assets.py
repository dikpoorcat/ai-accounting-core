"""Pure deterministic calculations for purchased intangible assets."""

from __future__ import annotations

import hashlib
import json
import uuid
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

SMALL_ENTERPRISE_INTANGIBLE_ASSET_RULE_VERSION = (
    "small_enterprise_intangible_assets_2013.1"
)
MAX_FEN = 2**63 - 1


class IntangibleAssetCalculationError(ValueError):
    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class AcquisitionCostResult:
    purchase_price_fen: int
    noncreditable_tax_fen: int
    directly_attributable_cost_fen: int
    cost_fen: int


@dataclass(frozen=True)
class StraightLineAmortizationResult:
    cost_fen: int
    residual_value_fen: int
    useful_life_months: int
    completed_months: int
    opening_accumulated_amortization_fen: int
    base_monthly_fen: int
    amortization_fen: int
    closing_accumulated_amortization_fen: int
    is_final_month: bool


def _require_fen(value: object, field: str, *, positive: bool = False) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise IntangibleAssetCalculationError(
            "INVALID_FEN", f"{field} must be an integer fen amount"
        )
    if value < 0 or (positive and value == 0):
        qualifier = "positive" if positive else "non-negative"
        raise IntangibleAssetCalculationError(
            "INVALID_FEN", f"{field} must be {qualifier}"
        )
    if value > MAX_FEN:
        raise IntangibleAssetCalculationError(
            "INTANGIBLE_ASSET_COST_OUT_OF_RANGE",
            f"{field} exceeds signed 64-bit integer fen",
        )
    return value


def calculate_acquisition_cost(
    *,
    purchase_price_fen: int,
    noncreditable_tax_fen: int,
    directly_attributable_cost_fen: int,
) -> AcquisitionCostResult:
    components = {
        "purchase_price_fen": _require_fen(purchase_price_fen, "purchase_price_fen"),
        "noncreditable_tax_fen": _require_fen(
            noncreditable_tax_fen, "noncreditable_tax_fen"
        ),
        "directly_attributable_cost_fen": _require_fen(
            directly_attributable_cost_fen, "directly_attributable_cost_fen"
        ),
    }
    cost_fen = sum(components.values())
    if cost_fen > MAX_FEN:
        raise IntangibleAssetCalculationError(
            "INTANGIBLE_ASSET_COST_OUT_OF_RANGE",
            "acquisition cost exceeds signed 64-bit integer fen",
        )
    if cost_fen <= 0:
        raise IntangibleAssetCalculationError(
            "INTANGIBLE_ASSET_COST_MUST_BE_POSITIVE", "cost_fen must be positive"
        )
    return AcquisitionCostResult(**components, cost_fen=cost_fen)


def calculate_straight_line_amortization(
    *,
    cost_fen: int,
    useful_life_months: int,
    completed_months: int,
    opening_accumulated_amortization_fen: int,
) -> StraightLineAmortizationResult:
    """Calculate one continuous full calendar month in integer fen.

    The residual value is a closed policy constant of zero.  All remainder is
    posted in the final month so the immutable event chain closes exactly.
    """

    cost = _require_fen(cost_fen, "cost_fen", positive=True)
    if isinstance(useful_life_months, bool) or not isinstance(useful_life_months, int):
        raise IntangibleAssetCalculationError(
            "INTANGIBLE_ASSET_INVALID_AMORTIZATION_POLICY",
            "useful_life_months must be an integer",
        )
    if useful_life_months <= 0 or cost < useful_life_months:
        raise IntangibleAssetCalculationError(
            "INTANGIBLE_ASSET_INVALID_AMORTIZATION_POLICY",
            "cost must provide at least one fen in every useful-life month",
        )
    if isinstance(completed_months, bool) or not isinstance(completed_months, int):
        raise IntangibleAssetCalculationError(
            "INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE",
            "completed_months must be an integer",
        )
    if completed_months < 0 or completed_months >= useful_life_months:
        raise IntangibleAssetCalculationError(
            "INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE",
            "completed_months must identify an unposted amortization month",
        )
    opening = _require_fen(
        opening_accumulated_amortization_fen,
        "opening_accumulated_amortization_fen",
    )
    base_monthly_fen = cost // useful_life_months
    expected_opening = base_monthly_fen * completed_months
    if opening != expected_opening:
        raise IntangibleAssetCalculationError(
            "INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE",
            "opening accumulated amortization does not match continuous history",
        )
    is_final_month = completed_months == useful_life_months - 1
    amortization_fen = cost - opening if is_final_month else base_monthly_fen
    closing = opening + amortization_fen
    return StraightLineAmortizationResult(
        cost_fen=cost,
        residual_value_fen=0,
        useful_life_months=useful_life_months,
        completed_months=completed_months,
        opening_accumulated_amortization_fen=opening,
        base_monthly_fen=base_monthly_fen,
        amortization_fen=amortization_fen,
        closing_accumulated_amortization_fen=closing,
        is_final_month=is_final_month,
    )


def intangible_asset_calculation_hash(
    *, command: str, request: Mapping[str, Any], calculation: Mapping[str, Any] | object
) -> str:
    if not command:
        raise IntangibleAssetCalculationError(
            "INVALID_INTANGIBLE_ASSET_COMMAND", "command is required"
        )
    if hasattr(calculation, "model_dump"):
        normalized_calculation = calculation.model_dump(mode="json")
    elif hasattr(calculation, "__dataclass_fields__"):
        normalized_calculation = asdict(calculation)
    else:
        normalized_calculation = calculation
    payload = {
        "command": command,
        "request": request,
        "calculation": normalized_calculation,
    }
    try:
        canonical = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=_json_default,
        )
    except (TypeError, ValueError) as exc:
        raise IntangibleAssetCalculationError(
            "INVALID_INTANGIBLE_ASSET_CALCULATION_HASH_INPUT",
            "hash input is not canonical JSON",
        ) from exc
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _json_default(value: object) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, uuid.UUID):
        return str(value)
    raise TypeError(f"unsupported canonical JSON value: {type(value).__name__}")
