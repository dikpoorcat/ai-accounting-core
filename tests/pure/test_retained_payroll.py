"""Contribution regressions and the absence of runtime PIT calculators."""
from datetime import date
from decimal import Decimal

from ai_accounting.payroll import (
    ContributionBaseKind,
    ContributionBases,
    ContributionPolicy,
    ContributionRule,
    RoundingRule,
    calculate_contributions,
)


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

def test_runtime_package_has_no_income_tax_calculators():
    import ai_accounting.payroll as payroll
    from ai_accounting.payroll import annual_bonus, income_tax
    for module, names in (
        (payroll, (
            'calculate_cumulative_withholding', 'calculate_annual_bonus_scenarios',
            'select_annual_bonus_tax_method',
        )),
        (income_tax, ('calculate_cumulative_withholding',)),
        (annual_bonus, ('calculate_annual_bonus_scenarios', 'select_annual_bonus_tax_method')),
    ):
        assert all(not hasattr(module, name) for name in names)
