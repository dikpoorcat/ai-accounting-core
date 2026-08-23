from __future__ import annotations

from dataclasses import replace
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
    EmployeeContributionShortfallTreatment,
    ExpiredPolicyError,
    NeedsInformationError,
    RegularPayrollInput,
    RoundingRule,
    YearMonth,
    allocate_contribution_burden,
    calculate_annual_bonus_scenarios,
    calculate_contributions,
    calculate_cumulative_withholding,
    calculate_regular_payroll,
    select_annual_bonus_tax_method,
)


def contribution_policy() -> ContributionPolicy:
    return ContributionPolicy(
        version="test-contributions-2026",
        jurisdiction="test-jurisdiction",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
        primary_source_url="https://www.mof.gov.cn/",
        rules=(
            ContributionRule(
                code="pension",
                base_kind=ContributionBaseKind.SOCIAL_INSURANCE,
                employee_rate=Decimal("0.08"),
                employer_rate=Decimal("0.16"),
                minimum_base_fen=300_000,
                maximum_base_fen=3_000_000,
                rounding_rule=RoundingRule.HALF_UP,
            ),
            ContributionRule(
                code="medical",
                base_kind=ContributionBaseKind.SOCIAL_INSURANCE,
                employee_rate=Decimal("0.015"),
                employer_rate=Decimal("0.025"),
                minimum_base_fen=300_000,
                maximum_base_fen=3_000_000,
                rounding_rule=RoundingRule.HALF_UP,
            ),
            ContributionRule(
                code="housing_fund",
                base_kind=ContributionBaseKind.HOUSING_FUND,
                employee_rate=Decimal("0.07"),
                employer_rate=Decimal("0.07"),
                minimum_base_fen=300_000,
                maximum_base_fen=3_000_000,
                rounding_rule=RoundingRule.HALF_UP,
            ),
        ),
    )


def wage_tax_policy() -> CumulativeIncomeTaxPolicy:
    return CumulativeIncomeTaxPolicy.china_resident_wage_withholding()


def hangzhou_social_policy() -> ContributionPolicy:
    rates = (
        ("pension", "0.08", "0.16"),
        ("medical", "0.02", "0.095"),
        ("unemployment", "0.005", "0.005"),
        ("work_injury", "0", "0.004"),
    )
    return ContributionPolicy(
        version="hangzhou-2026-test",
        jurisdiction="杭州",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
        primary_source_url="https://www.hangzhou.gov.cn/",
        rules=tuple(
            ContributionRule(
                code=code,
                base_kind=ContributionBaseKind.SOCIAL_INSURANCE,
                employee_rate=Decimal(employee_rate),
                employer_rate=Decimal(employer_rate),
                minimum_base_fen=0,
                maximum_base_fen=10_000_000,
                rounding_rule=RoundingRule.HALF_UP,
            )
            for code, employee_rate, employer_rate in rates
        ),
    )


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


def test_cumulative_withholding_is_zero_below_deduction_and_never_refunds() -> None:
    zero = calculate_cumulative_withholding(
        wage_tax_policy(),
        YearMonth(2026, 1),
        CumulativeTaxState.empty(2026),
        current_tax_input(500_000),
    )
    assert zero.current_withholding_tax_fen == 0

    over_withheld = replace(
        zero.new_state,
        cumulative_withheld_tax_fen=100_000,
    )
    no_refund = calculate_cumulative_withholding(
        wage_tax_policy(),
        YearMonth(2026, 2),
        over_withheld,
        current_tax_input(500_000, income_date=date(2026, 2, 28)),
    )
    assert no_refund.current_withholding_tax_fen == 0


def test_cumulative_withholding_carries_prior_month_state() -> None:
    january = calculate_cumulative_withholding(
        wage_tax_policy(),
        YearMonth(2026, 1),
        CumulativeTaxState.empty(2026),
        current_tax_input(1_000_000),
    )
    february = calculate_cumulative_withholding(
        wage_tax_policy(),
        YearMonth(2026, 2),
        january.new_state,
        current_tax_input(
            1_000_000,
            income_date=date(2026, 2, 28),
        ),
    )
    assert january.current_withholding_tax_fen == 15_000
    assert february.current_withholding_tax_fen == 15_000
    assert february.new_state.cumulative_income_fen == 2_000_000
    assert february.new_state.cumulative_withheld_tax_fen == 30_000


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


def test_missing_cumulative_state_and_contribution_bases_are_structured() -> None:
    with pytest.raises(NeedsInformationError) as state_error:
        calculate_cumulative_withholding(
            wage_tax_policy(),
            YearMonth(2026, 7),
            None,
            current_tax_input(1_000_000, income_date=date(2026, 7, 31)),
        )
    assert state_error.value.as_response() == {
        "status": "needs_information",
        "missing_information": [
            {
                "code": "cumulative_tax_state",
                "message": "a known zero state or payroll opening state is required",
                "fields": ["payroll_opening_state"],
            }
        ],
    }

    with pytest.raises(NeedsInformationError) as base_error:
        calculate_contributions(contribution_policy(), None, date(2026, 7, 31))
    assert base_error.value.requirements[0].code == "contribution_bases"


@pytest.mark.parametrize(
    ("profile_base_fen", "expected_capped_base_fen"),
    [(100_000, 300_000), (300_000, 300_000), (4_000_000, 3_000_000)],
)
def test_contributions_apply_base_bounds_per_component(
    profile_base_fen: int, expected_capped_base_fen: int
) -> None:
    result = calculate_contributions(
        contribution_policy(),
        ContributionBases(profile_base_fen, profile_base_fen),
        date(2026, 7, 31),
    )
    assert {line.capped_base_fen for line in result.lines} == {expected_capped_base_fen}
    assert result.employee_total_fen == sum(line.employee_contribution_fen for line in result.lines)
    assert result.employer_total_fen == sum(line.employer_contribution_fen for line in result.lines)


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


def test_nonparticipating_employee_does_not_invent_minimum_base_contributions() -> None:
    result = calculate_contributions(
        contribution_policy(),
        ContributionBases(
            social_insurance_base_fen=0,
            housing_fund_base_fen=0,
            social_insurance_participating=False,
            housing_fund_participating=False,
        ),
        date(2026, 7, 31),
    )

    assert {line.capped_base_fen for line in result.lines} == {0}
    assert result.employee_total_fen == 0
    assert result.employer_total_fen == 0
    assert all(
        line_trace.values["employee_participating"] is False
        for line_trace in result.trace
    )


def test_zero_tax_reported_salary_can_shift_employee_social_share_to_company() -> None:
    statutory = calculate_contributions(
        hangzhou_social_policy(),
        ContributionBases(social_insurance_base_fen=500_000, housing_fund_base_fen=0),
        date(2026, 3, 31),
    )
    burden = allocate_contribution_burden(
        statutory,
        0,
        EmployeeContributionShortfallTreatment.EMPLOYER_BORNE,
    )

    assert statutory.employee_social_insurance_fen == 52_500
    assert statutory.employer_social_insurance_fen == 132_000
    assert burden.employee_social_insurance_fen == 0
    assert burden.employer_social_insurance_fen == 184_500
    assert burden.employer_borne_employee_contributions_fen == 52_500


def test_tax_reported_wages_from_march_reconcile_to_filed_may_withholding() -> None:
    policy = wage_tax_policy()

    def filed_tax(wages: tuple[int, int, int]) -> int:
        state = CumulativeTaxState.empty(2026)
        result = None
        for month, income_fen, employee_social_fen in (
            (3, wages[0], 0),
            (4, wages[1], 52_500),
            (5, wages[2], 52_500),
        ):
            result = calculate_cumulative_withholding(
                policy,
                YearMonth(2026, month),
                state,
                CumulativeTaxPeriodInput(
                    income_date=date(2026, month, 28),
                    withholding_start_date=date(2026, 3, 1),
                    income_fen=income_fen,
                    tax_exempt_income_fen=0,
                    employee_contributions_fen=employee_social_fen,
                    special_additional_deduction_fen=0,
                    other_legal_deduction_fen=0,
                ),
            )
            state = result.new_state
        assert result is not None
        return result.current_withholding_tax_fen

    luo_tax = filed_tax((0, 52_500, 2_557_633))
    jiang_tax = filed_tax((0, 357_000, 2_658_783))
    assert luo_tax == 30_154
    assert jiang_tax == 42_323
    assert luo_tax + jiang_tax == 72_477


def test_regular_payroll_reconciles_gross_deductions_tax_and_net_pay() -> None:
    payroll_input = RegularPayrollInput(
        tax_reported_salary_fen=2_350_000,
        special_additional_deduction_fen=20_000,
        other_legal_deduction_fen=10_000,
    )
    contributions = calculate_contributions(
        contribution_policy(), ContributionBases(1_000_000, 1_000_000), date(2026, 1, 31)
    )
    income_tax = calculate_cumulative_withholding(
        wage_tax_policy(),
        YearMonth(2026, 1),
        CumulativeTaxState.empty(2026),
        CumulativeTaxPeriodInput(
            income_date=date(2026, 1, 31),
            withholding_start_date=date(2026, 1, 1),
            income_fen=payroll_input.gross_salary_fen,
            tax_exempt_income_fen=0,
            employee_contributions_fen=contributions.employee_total_fen,
            special_additional_deduction_fen=payroll_input.special_additional_deduction_fen,
            other_legal_deduction_fen=payroll_input.other_legal_deduction_fen,
        ),
    )
    result = calculate_regular_payroll(payroll_input, contributions, income_tax)
    assert result.gross_salary_fen == 2_350_000
    assert result.gross_salary_fen == result.net_pay_fen + result.employee_deductions_fen
    assert result.employee_deductions_fen == (
        result.employee_social_insurance_fen
        + result.employee_housing_fund_fen
        + result.individual_income_tax_fen
    )
    assert result.trace[0].step == "net_pay_reconciliation"


def test_regular_payroll_rejects_a_negative_tax_reported_salary() -> None:
    with pytest.raises(CalculationValidationError, match="tax_reported_salary_fen"):
        RegularPayrollInput(
            tax_reported_salary_fen=-1,
            special_additional_deduction_fen=0,
            other_legal_deduction_fen=0,
        )


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


def test_builtin_tax_policies_include_primary_official_sources() -> None:
    wage_policy = wage_tax_policy()
    bonus_policy = AnnualBonusTaxPolicy.china_annual_bonus_2024_to_2027()
    assert "chinatax.gov.cn" in wage_policy.primary_source_url
    assert "chinatax.gov.cn" in wage_policy.legal_basis_source_url
    assert "mof.gov.cn" in bonus_policy.primary_source_url
