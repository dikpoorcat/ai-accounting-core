"""Preserved pure regressions from tests/test_intangible_assets.py; no legacy service fixtures."""

from __future__ import annotations

import pytest

from ai_accounting.intangible_assets import (
    MAX_FEN,
    IntangibleAssetCalculationError,
    calculate_acquisition_cost,
    calculate_straight_line_amortization,
)


def test_acquisition_cost_is_strict_integer_fen() -> None:
    result = calculate_acquisition_cost(
        cost_fen=13_000,
        purchase_price_fen=12_000,
        noncreditable_tax_fen=360,
        directly_attributable_cost_fen=640,
    )
    assert result.cost_fen == 13_000

    for bad in (True, 1.0, "1", -1):
        with pytest.raises(IntangibleAssetCalculationError) as exc:
            calculate_acquisition_cost(
                cost_fen=1,
                purchase_price_fen=bad,  # type: ignore[arg-type]
                noncreditable_tax_fen=0,
                directly_attributable_cost_fen=0,
            )
        assert exc.value.code == "INVALID_FEN"

    with pytest.raises(IntangibleAssetCalculationError) as exc:
        calculate_acquisition_cost(cost_fen=MAX_FEN + 1)
    assert exc.value.code == "INTANGIBLE_ASSET_COST_OUT_OF_RANGE"


def test_amortization_remainder_closes_exactly_in_final_month() -> None:
    cost = 1_205
    life = 12
    results = [
        calculate_straight_line_amortization(
            cost_fen=cost,
            useful_life_months=life,
            completed_months=index,
            opening_accumulated_amortization_fen=(cost // life) * index,
        )
        for index in range(life)
    ]
    assert [item.amortization_fen for item in results[:-1]] == [100] * 11
    assert results[-1].amortization_fen == 105
    assert sum(item.amortization_fen for item in results) == cost
    assert results[-1].closing_accumulated_amortization_fen == cost
    assert results[-1].residual_value_fen == 0


def test_amortization_rejects_zero_month_policy_and_noncontinuous_opening() -> None:
    with pytest.raises(IntangibleAssetCalculationError) as exc:
        calculate_straight_line_amortization(
            cost_fen=11,
            useful_life_months=12,
            completed_months=0,
            opening_accumulated_amortization_fen=0,
        )
    assert exc.value.code == "INTANGIBLE_ASSET_INVALID_AMORTIZATION_POLICY"

    with pytest.raises(IntangibleAssetCalculationError) as exc:
        calculate_straight_line_amortization(
            cost_fen=120,
            useful_life_months=12,
            completed_months=2,
            opening_accumulated_amortization_fen=19,
        )
    assert exc.value.code == "INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE"
