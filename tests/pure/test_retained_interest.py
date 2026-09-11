"""Preserved pure regressions from tests/test_borrowings.py; no legacy service fixtures."""

from __future__ import annotations

from datetime import date, timedelta
from decimal import ROUND_HALF_UP, Decimal, Inexact, getcontext, setcontext

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ai_accounting.borrowings import (
    BorrowingCalculationError,
    calculate_simple_interest,
)


def test_simple_interest_uses_actual_days_and_round_half_up_independently() -> None:
    first = calculate_simple_interest(
        principal_fen=10_000,
        annual_rate_percent=Decimal("3.65"),
        period_start=date(2026, 1, 1),
        period_end=date(2026, 2, 1),
        day_count_basis="actual_365",
    )
    second = calculate_simple_interest(
        principal_fen=10_000,
        annual_rate_percent=Decimal("3.65"),
        period_start=date(2026, 2, 1),
        period_end=date(2026, 3, 1),
        day_count_basis="actual_365",
    )

    assert first.actual_days == 31
    assert second.actual_days == 28
    assert first.interest_fen == 31
    assert second.interest_fen == 28
    assert first.day_count_denominator == 365


def test_simple_interest_rejects_zero_fen_and_non_decimal_rate() -> None:
    with pytest.raises(BorrowingCalculationError) as zero:
        calculate_simple_interest(
            principal_fen=1,
            annual_rate_percent=Decimal("0.01"),
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 2),
            day_count_basis="actual_360",
        )
    assert zero.value.code == "BORROWING_INTEREST_AMOUNT_MUST_BE_POSITIVE"

    with pytest.raises(BorrowingCalculationError) as rate:
        calculate_simple_interest(
            principal_fen=100,
            annual_rate_percent=3.65,  # type: ignore[arg-type]
            period_start=date(2026, 1, 1),
            period_end=date(2026, 1, 2),
            day_count_basis="actual_360",
        )
    assert rate.value.code == "BORROWING_INVALID_RATE"


@given(
    principal_fen=st.integers(min_value=100_000, max_value=1_000_000_000),
    annual_rate_basis_points=st.integers(min_value=100, max_value=10_000),
    actual_days=st.integers(min_value=1, max_value=366),
    day_count_basis=st.sampled_from(["actual_360", "actual_365"]),
)
@settings(max_examples=100, deadline=None)
def test_simple_interest_property_matches_exact_decimal_formula(
    principal_fen: int,
    annual_rate_basis_points: int,
    actual_days: int,
    day_count_basis: str,
) -> None:
    start = date(2024, 1, 1)
    end = start + timedelta(days=actual_days)
    rate = Decimal(annual_rate_basis_points) / Decimal("100")

    result = calculate_simple_interest(
        principal_fen=principal_fen,
        annual_rate_percent=rate,
        period_start=start,
        period_end=end,
        day_count_basis=day_count_basis,
    )

    denominator = 360 if day_count_basis == "actual_360" else 365
    expected = (
        Decimal(principal_fen) * rate / Decimal("100") * Decimal(actual_days) / Decimal(denominator)
    )
    assert result.actual_days == actual_days
    assert result.interest_fen == int(expected.quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def test_simple_interest_is_independent_of_global_decimal_precision_and_traps() -> None:
    original_context = getcontext().copy()
    outcomes: list[tuple[Decimal, int]] = []
    try:
        for precision in (6, 16, 28):
            getcontext().prec = precision
            getcontext().traps[Inexact] = True
            result = calculate_simple_interest(
                principal_fen=9_223_372_036_854_775_807,
                annual_rate_percent=Decimal("99.123456"),
                period_start=date(2024, 1, 1),
                period_end=date(2024, 12, 31),
                day_count_basis="actual_365",
            )
            outcomes.append((result.unrounded_interest_fen, result.interest_fen))
    finally:
        setcontext(original_context)

    assert outcomes[0] == outcomes[1] == outcomes[2]
