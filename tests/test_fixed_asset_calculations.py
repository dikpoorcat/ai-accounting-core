from __future__ import annotations

import uuid
from datetime import date
from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ai_accounting.fixed_assets import (
    DepreciationGroupMember,
    FixedAssetCalculationError,
    calculate_acquisition_cost,
    calculate_grouped_straight_line_depreciation,
    calculate_straight_line_depreciation,
    calculate_used_fixed_asset_vat,
    fixed_asset_calculation_hash,
)


def test_acquisition_cost_is_exact_sum_of_the_only_supported_components() -> None:
    result = calculate_acquisition_cost(
        purchase_price_fen=100_000,
        noncreditable_tax_fen=3_000,
        transport_and_handling_fen=500,
        installation_and_direct_cost_fen=1_500,
    )

    assert result.cost_fen == 105_000
    assert result.purchase_price_fen + result.noncreditable_tax_fen == 103_000


@pytest.mark.parametrize("invalid", [True, 1.0, "1", -1])
def test_acquisition_cost_rejects_non_integer_or_negative_fen(invalid: object) -> None:
    with pytest.raises(FixedAssetCalculationError) as error:
        calculate_acquisition_cost(
            purchase_price_fen=100,
            noncreditable_tax_fen=0,
            transport_and_handling_fen=0,
            installation_and_direct_cost_fen=invalid,  # type: ignore[arg-type]
        )

    assert error.value.code == "INVALID_FEN"


def test_straight_line_depreciation_puts_rounding_remainder_in_last_month() -> None:
    first = calculate_straight_line_depreciation(
        cost_fen=1_001,
        residual_value_fen=1,
        useful_life_months=3,
        completed_months=0,
        opening_accumulated_depreciation_fen=0,
    )
    second = calculate_straight_line_depreciation(
        cost_fen=1_001,
        residual_value_fen=1,
        useful_life_months=3,
        completed_months=1,
        opening_accumulated_depreciation_fen=first.closing_accumulated_depreciation_fen,
    )
    final = calculate_straight_line_depreciation(
        cost_fen=1_001,
        residual_value_fen=1,
        useful_life_months=3,
        completed_months=2,
        opening_accumulated_depreciation_fen=second.closing_accumulated_depreciation_fen,
    )

    assert [first.depreciation_fen, second.depreciation_fen, final.depreciation_fen] == [
        333,
        333,
        334,
    ]
    assert final.is_final_month is True
    assert final.closing_accumulated_depreciation_fen == 1_000


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


def test_straight_line_rejects_a_policy_that_would_create_zero_fen_months() -> None:
    with pytest.raises(FixedAssetCalculationError) as error:
        calculate_straight_line_depreciation(
            cost_fen=100,
            residual_value_fen=0,
            useful_life_months=101,
            completed_months=0,
            opening_accumulated_depreciation_fen=0,
        )

    assert error.value.code == "FIXED_ASSET_INVALID_DEPRECIATION_POLICY"


def test_2026_used_fixed_asset_vat_uses_three_percent_base_and_two_percent_vat() -> None:
    result = calculate_used_fixed_asset_vat(gross_proceeds_fen=103)

    assert result.tax_sales_fen == 100
    assert result.vat_fen == 2
    assert result.taxable_rate == Decimal("0.03")
    assert result.vat_rate == Decimal("0.02")


def test_calculation_hash_is_canonical_and_covers_the_derived_result() -> None:
    calculation = calculate_used_fixed_asset_vat(gross_proceeds_fen=10_300)
    first = fixed_asset_calculation_hash(
        command="finance_preview_fixed_asset_depreciation",
        request={
            "asset_id": uuid.UUID("12345678-1234-5678-1234-567812345678"),
            "posting_date": date(2026, 2, 28),
            "period": "2026-02",
        },
        calculation=calculation,
    )
    replay = fixed_asset_calculation_hash(
        command="finance_preview_fixed_asset_depreciation",
        request={
            "period": "2026-02",
            "posting_date": "2026-02-28",
            "asset_id": "12345678-1234-5678-1234-567812345678",
        },
        calculation=calculation,
    )
    changed = fixed_asset_calculation_hash(
        command="finance_preview_fixed_asset_depreciation",
        request={"asset_id": "asset", "posting_date": "2026-02-28", "period": "2026-02"},
        calculation={"vat_fen": 201},
    )

    assert len(first) == 64
    assert first == replay
    assert first != changed
