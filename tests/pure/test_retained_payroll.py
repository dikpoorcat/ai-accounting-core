"""Preserved pure regressions from tests/test_payroll_calculators.py; no legacy service fixtures."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest

from ai_accounting.payroll import (
    AnnualBonusScenarioInput,
    AnnualBonusTaxMethod,
    AnnualBonusTaxPolicy,
    AnnualBonusUsage,
    CalculationValidationError,
    ContributionBaseKind,
    ContributionBases,
    ContributionPolicy,
    ContributionRule,
    CumulativeIncomeTaxPolicy,
    CumulativeTaxPeriodInput,
    CumulativeTaxState,
    ExpiredPolicyError,
    NeedsInformationError,
    RoundingRule,
    YearMonth,
    calculate_annual_bonus_scenarios,
    calculate_contributions,
    calculate_cumulative_withholding,
    select_annual_bonus_tax_method,
)


def wage_tax_policy() -> CumulativeIncomeTaxPolicy:
    return CumulativeIncomeTaxPolicy.china_resident_wage_withholding()


def current_tax_input(
    income_fen: int,
    contributions_fen: int = 0,
    *,
    income_date: date = date(2026, 1, 31),
    withholding_start_date: date = date(2026, 1, 1),
) -> CumulativeTaxPeriodInput:
    return CumulativeTaxPeriodInput(
        income_date=income_date,
        withholding_start_date=withholding_start_date,
        income_fen=income_fen,
        tax_exempt_income_fen=0,
        employee_contributions_fen=contributions_fen,
        special_additional_deduction_fen=0,
        other_legal_deduction_fen=0,
    )


@pytest.mark.parametrize(
    ("taxable_income_fen", "rate"),
    [
        (0, Decimal("0.03")),
        (3_600_000, Decimal("0.03")),
        (3_600_001, Decimal("0.10")),
        (14_400_000, Decimal("0.10")),
        (14_400_001, Decimal("0.20")),
        (30_000_000, Decimal("0.20")),
        (30_000_001, Decimal("0.25")),
        (42_000_000, Decimal("0.25")),
        (42_000_001, Decimal("0.30")),
        (66_000_000, Decimal("0.30")),
        (66_000_001, Decimal("0.35")),
        (96_000_000, Decimal("0.35")),
        (96_000_001, Decimal("0.45")),
    ],
)
def test_cumulative_withholding_uses_correct_tax_bracket_boundary(
    taxable_income_fen: int, rate: Decimal
) -> None:
    result = calculate_cumulative_withholding(
        wage_tax_policy(),
        YearMonth(2026, 1),
        CumulativeTaxState.empty(2026),
        current_tax_input(taxable_income_fen + 500_000),
    )
    assert result.cumulative_taxable_income_fen == taxable_income_fen
    assert result.bracket_rate == rate


def test_midyear_new_hire_must_supply_an_explicit_known_zero_state() -> None:
    july = calculate_cumulative_withholding(
        wage_tax_policy(),
        YearMonth(2026, 7),
        CumulativeTaxState.empty(2026),
        current_tax_input(
            1_000_000,
            income_date=date(2026, 7, 31),
            withholding_start_date=date(2026, 7, 1),
        ),
    )
    assert july.new_state.cumulative_standard_deduction_fen == 500_000
    assert july.current_withholding_tax_fen == 15_000


def test_contributions_round_each_component_not_the_aggregate() -> None:
    policy = ContributionPolicy(
        version="rounding",
        jurisdiction="test",
        effective_from=date(2026, 1, 1),
        effective_to=None,
        primary_source_url="https://www.mof.gov.cn/",
        rules=(
            ContributionRule(
                code="first",
                base_kind=ContributionBaseKind.SOCIAL_INSURANCE,
                employee_rate=Decimal("0.015"),
                employer_rate=Decimal("0"),
                minimum_base_fen=0,
                maximum_base_fen=10_000,
                rounding_rule=RoundingRule.HALF_UP,
            ),
            ContributionRule(
                code="second",
                base_kind=ContributionBaseKind.SOCIAL_INSURANCE,
                employee_rate=Decimal("0.015"),
                employer_rate=Decimal("0"),
                minimum_base_fen=0,
                maximum_base_fen=10_000,
                rounding_rule=RoundingRule.HALF_UP,
            ),
        ),
    )
    result = calculate_contributions(policy, ContributionBases(101, None), date(2026, 1, 31))
    assert [line.employee_contribution_fen for line in result.lines] == [2, 2]
    assert result.employee_social_insurance_fen == 4


def bonus_request(
    *, used: bool = False, period: YearMonth | None = None
) -> AnnualBonusScenarioInput:
    actual_period = period or YearMonth(2026, 12)
    return AnnualBonusScenarioInput(
        period=actual_period,
        payment_date=date(actual_period.year, actual_period.month, 28),
        bonus_fen=12_000_000,
        prior_tax_state=CumulativeTaxState.empty(actual_period.year),
        regular_period_input=current_tax_input(
            1_000_000,
            income_date=date(actual_period.year, actual_period.month, 28),
            withholding_start_date=date(actual_period.year, 1, 1),
        ),
        usage=AnnualBonusUsage(actual_period.year, used),
    )


def test_annual_bonus_returns_separate_and_combined_scenarios_without_selecting() -> None:
    scenarios = calculate_annual_bonus_scenarios(
        AnnualBonusTaxPolicy.china_annual_bonus_2024_to_2027(),
        wage_tax_policy(),
        bonus_request(),
    )
    assert scenarios.separate.tax_fen == 948_000
    assert scenarios.separate.available is True
    assert scenarios.combined.available is True
    assert scenarios.based_on_current_known_cumulative_values is True
    with pytest.raises(NeedsInformationError) as selection_error:
        select_annual_bonus_tax_method(scenarios, None)
    assert selection_error.value.requirements[0].code == "annual_bonus_tax_method"
    assert (
        select_annual_bonus_tax_method(scenarios, AnnualBonusTaxMethod.SEPARATE)
        == scenarios.separate
    )


def test_annual_bonus_separate_method_is_limited_to_once_per_year() -> None:
    scenarios = calculate_annual_bonus_scenarios(
        AnnualBonusTaxPolicy.china_annual_bonus_2024_to_2027(),
        wage_tax_policy(),
        bonus_request(used=True),
    )
    assert scenarios.separate.available is False
    assert scenarios.separate.tax_fen is None
    assert scenarios.combined.available is True
    with pytest.raises(CalculationValidationError, match="unavailable"):
        select_annual_bonus_tax_method(scenarios, AnnualBonusTaxMethod.SEPARATE)


def test_expired_annual_bonus_policy_refuses_calculation() -> None:
    with pytest.raises(ExpiredPolicyError) as error:
        calculate_annual_bonus_scenarios(
            AnnualBonusTaxPolicy.china_annual_bonus_2024_to_2027(),
            wage_tax_policy(),
            bonus_request(period=YearMonth(2028, 1)),
        )
    assert error.value.code == "POLICY_NOT_EFFECTIVE"
