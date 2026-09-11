"""Preserved pure depreciation regressions; no legacy service fixtures."""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ai_accounting.fixed_assets import (
    DepreciationGroupMember,
    FixedAssetCalculationError,
    calculate_grouped_straight_line_depreciation,
    calculate_straight_line_depreciation,
)


def test_grouped_straight_line_rounds_book_card_half_up_before_allocation() -> None:
    members = (
        DepreciationGroupMember("sign", 400_000, 0),
        DepreciationGroupMember("renovation", 3_200_000, 0),
        DepreciationGroupMember("glass", 140_000, 0),
        DepreciationGroupMember("adjustment", 260_000, 0),
    )
    results = [
        calculate_grouped_straight_line_depreciation(
            members=members,
            member_key=member.member_key,
            useful_life_months=60,
            completed_months=0,
            opening_accumulated_depreciation_fen=0,
        )
        for member in members
    ]

    assert {result.group_base_monthly_fen for result in results} == {66_667}
    assert sum(result.member_result.depreciation_fen for result in results) == 66_667
    assert sum(result.member_receives_rounding_fen for result in results) == 2


def test_grouped_straight_line_final_month_closes_every_member_exactly() -> None:
    members = (
        DepreciationGroupMember("a", 1_001, 1),
        DepreciationGroupMember("b", 1_002, 2),
    )
    first = {
        member.member_key: calculate_grouped_straight_line_depreciation(
            members=members,
            member_key=member.member_key,
            useful_life_months=3,
            completed_months=0,
            opening_accumulated_depreciation_fen=0,
        )
        for member in members
    }
    final = {
        member.member_key: calculate_grouped_straight_line_depreciation(
            members=members,
            member_key=member.member_key,
            useful_life_months=3,
            completed_months=2,
            opening_accumulated_depreciation_fen=(
                first[member.member_key].member_result.depreciation_fen * 2
            ),
        )
        for member in members
    }

    assert all(result.member_result.is_final_month for result in final.values())
    assert {
        member.member_key: (
            first[member.member_key].member_result.depreciation_fen * 2
            + final[member.member_key].member_result.depreciation_fen
        )
        for member in members
    } == {"a": 1_000, "b": 1_000}


@given(
    useful_life_months=st.integers(min_value=1, max_value=120),
    residual_value_fen=st.integers(min_value=0, max_value=10_000),
    depreciable_fen=st.integers(min_value=120, max_value=1_000_000),
)
@settings(max_examples=100, deadline=None)
def test_straight_line_property_closes_exactly_to_depreciable_cost(
    useful_life_months: int, residual_value_fen: int, depreciable_fen: int
) -> None:
    cost_fen = residual_value_fen + depreciable_fen
    opening = 0
    calculated = []

    for completed_months in range(useful_life_months):
        result = calculate_straight_line_depreciation(
            cost_fen=cost_fen,
            residual_value_fen=residual_value_fen,
            useful_life_months=useful_life_months,
            completed_months=completed_months,
            opening_accumulated_depreciation_fen=opening,
        )
        calculated.append(result.depreciation_fen)
        opening = result.closing_accumulated_depreciation_fen

    assert all(amount >= 0 for amount in calculated)
    assert sum(calculated) == depreciable_fen
    assert opening == depreciable_fen


def test_straight_line_rejects_noncontinuous_history_and_completed_life() -> None:
    with pytest.raises(FixedAssetCalculationError) as history_error:
        calculate_straight_line_depreciation(
            cost_fen=10_000,
            residual_value_fen=0,
            useful_life_months=4,
            completed_months=1,
            opening_accumulated_depreciation_fen=1,
        )
    assert history_error.value.code == "FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE"

    with pytest.raises(FixedAssetCalculationError) as life_error:
        calculate_straight_line_depreciation(
            cost_fen=10_000,
            residual_value_fen=0,
            useful_life_months=4,
            completed_months=4,
            opening_accumulated_depreciation_fen=10_000,
        )
    assert life_error.value.code == "FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE"
