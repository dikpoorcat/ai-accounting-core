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
    base_salary_fen=st.integers(min_value=1, max_value=10_000_000),
    performance_pay_fen=st.integers(min_value=0, max_value=3_000_000),
    taxable_allowance_fen=st.integers(min_value=0, max_value=1_000_000),
    tax_exempt_income_fen=st.integers(min_value=0, max_value=1_000_000),
    attendance_deduction_fen=st.integers(min_value=0, max_value=1_000_000),
)
@settings(max_examples=80, deadline=None)
def test_regular_payroll_property_preserves_gross_to_net_conservation(
    base_salary_fen: int,
    performance_pay_fen: int,
    taxable_allowance_fen: int,
    tax_exempt_income_fen: int,
    attendance_deduction_fen: int,
) -> None:
    taxable_before_deduction = base_salary_fen + performance_pay_fen + taxable_allowance_fen
    # The calculator correctly rejects a zero gross salary; this conservation
    # property covers only valid regular-payroll facts.
    bounded_attendance_deduction = min(
        attendance_deduction_fen,
        taxable_before_deduction,
        max(0, taxable_before_deduction + tax_exempt_income_fen - 1),
    )
    payroll_input = RegularPayrollInput(
        base_salary_fen=base_salary_fen,
        performance_pay_fen=performance_pay_fen,
        taxable_allowance_fen=taxable_allowance_fen,
        tax_exempt_income_fen=tax_exempt_income_fen,
        attendance_deduction_fen=bounded_attendance_deduction,
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
            employment_start_date=date(2026, 1, 1),
            income_fen=payroll_input.gross_salary_fen,
            tax_exempt_income_fen=payroll_input.tax_exempt_income_fen,
            employee_contributions_fen=0,
            special_additional_deduction_fen=0,
            other_legal_deduction_fen=0,
        ),
    )
    result = calculate_regular_payroll(payroll_input, contributions, tax)
    assert result.net_pay_fen >= 0
    assert result.gross_salary_fen == result.net_pay_fen + result.employee_deductions_fen
    assert result.individual_income_tax_fen >= 0
