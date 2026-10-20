"""An integer/Fraction oracle is independent of the Decimal implementation."""

from datetime import date, timedelta
from decimal import Decimal
from fractions import Fraction

import pytest

from ai_accounting_modular.domains.financing._interest import segments


def half_up(value):
    return (value.numerator * 2 + value.denominator) // (value.denominator * 2)


@pytest.mark.parametrize("principal", [1, 79, 5000, 1_000_000, 2**63 - 1])
@pytest.mark.parametrize("denominator", [360, 365])
@pytest.mark.parametrize("rate", ["0.00000001", "0.036", "1"])
def test_segment_rounding_matches_independent_daily_principal_oracle(principal, denominator, rate):
    start, end = date(2026, 1, 1), date(2026, 2, 1)
    first, second = principal // 3, principal // 5
    events = {"2026-01-01": first, "2026-01-16": second, "2026-02-01": principal}
    opening, actual, _ = segments(principal, Decimal(rate), denominator, start, end, events)
    assert opening == principal - first
    daily = []
    for n in range(31):
        day = start + timedelta(days=n)
        outstanding = principal - sum(
            value for stamp, value in events.items() if stamp <= day.isoformat()
        )
        daily.append(Fraction(max(0, outstanding)) * Fraction(rate) / denominator)
    expected = half_up(sum(daily[:15])) + half_up(sum(daily[15:]))
    assert sum(part[3] for part in actual) == expected


def test_real_repayment_splits_one_month_and_end_day_does_not_change_it():
    opening, actual, exact = segments(
        1_000_000,
        Decimal("0.036"),
        360,
        date(2026, 1, 1),
        date(2026, 2, 1),
        {"2026-01-16": 400_000, "2026-02-01": 600_000},
    )
    assert opening == 1_000_000
    assert [(x[2], x[3]) for x in actual] == [(1_000_000, 1500), (600_000, 960)]
    assert exact == 2460


def test_zero_rounding_and_actual_overpayment_cannot_create_negative_interest():
    _, actual, _ = segments(
        1, Decimal("0.036"), 360, date(2026, 1, 1), date(2026, 2, 1), {"2026-01-16": 2}
    )
    assert [x[2] for x in actual] == [1, 0]
    assert [x[3] for x in actual] == [0, 0]
