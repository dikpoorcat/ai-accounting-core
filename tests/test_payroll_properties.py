from __future__ import annotations

from datetime import date
from decimal import Decimal

from hypothesis import given, settings
from hypothesis import strategies as st

from ai_accounting.payroll import (
    ContributionBaseKind,
    ContributionBases,
    ContributionPolicy,
    ContributionRule,
    CumulativeIncomeTaxPolicy,
    CumulativeTaxPeriodInput,
    CumulativeTaxState,
    RegularPayrollInput,
    RoundingRule,
    YearMonth,
    calculate_contributions,
    calculate_cumulative_withholding,
    calculate_regular_payroll,
)

ZERO_RATE_POLICY = ContributionPolicy(
    version="zero-rate-property-policy",
    jurisdiction="test",
    effective_from=date(2026, 1, 1),
    effective_to=None,
    primary_source_url="https://www.mof.gov.cn/",
    rules=(
        ContributionRule(
            code="zero-social",
            base_kind=ContributionBaseKind.SOCIAL_INSURANCE,
            employee_rate=Decimal("0"),
            employer_rate=Decimal("0"),
            minimum_base_fen=0,
            maximum_base_fen=1_000_000_000,
            rounding_rule=RoundingRule.HALF_UP,
        ),
    ),
)


@given(
    tax_reported_salary_fen=st.integers(min_value=0, max_value=10_000_000),
)
@settings(max_examples=80, deadline=None)
def test_regular_payroll_property_preserves_gross_to_net_conservation(
    tax_reported_salary_fen: int,
) -> None:
    payroll_input = RegularPayrollInput(
        tax_reported_salary_fen=tax_reported_salary_fen,
        special_additional_deduction_fen=0,
        other_legal_deduction_fen=0,
    )
    contributions = calculate_contributions(
        ZERO_RATE_POLICY,
        ContributionBases(social_insurance_base_fen=0, housing_fund_base_fen=None),
        date(2026, 1, 31),
    )
    tax = calculate_cumulative_withholding(
        CumulativeIncomeTaxPolicy.china_resident_wage_withholding(),
        YearMonth(2026, 1),
        CumulativeTaxState.empty(2026),
        CumulativeTaxPeriodInput(
            income_date=date(2026, 1, 31),
            withholding_start_date=date(2026, 1, 1),
            income_fen=payroll_input.gross_salary_fen,
            tax_exempt_income_fen=0,
            employee_contributions_fen=0,
            special_additional_deduction_fen=0,
            other_legal_deduction_fen=0,
        ),
    )
    result = calculate_regular_payroll(payroll_input, contributions, tax)
    assert result.net_pay_fen >= 0
    assert result.gross_salary_fen == result.net_pay_fen + result.employee_deductions_fen
    assert result.individual_income_tax_fen >= 0
