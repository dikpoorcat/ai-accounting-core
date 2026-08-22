"""Focused regressions for payroll acceptance-remediation PAY-003 through PAY-021."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest
from pydantic import ValidationError

from ai_accounting.payroll import (
    AnnualBonusScenarioInput,
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
    RoundingRule,
    YearMonth,
    calculate_annual_bonus_scenarios,
    calculate_contributions,
    calculate_cumulative_withholding,
)
from ai_accounting.payroll.annual_bonus import AnnualBonusBracket
from ai_accounting.payroll.income_tax import TaxBracket
from ai_accounting.schemas import PreviewPayrollRequest, RegisterPayrollPolicyVersionRequest
from ai_accounting.service import FinanceService


def tax_policy(*, effective_to: date | None = None) -> CumulativeIncomeTaxPolicy:
    return CumulativeIncomeTaxPolicy(
        version="withholding-test-v1",
        effective_from=date(2026, 1, 1),
        effective_to=effective_to,
        primary_source_url="https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
        legal_basis_source_url="https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
        monthly_standard_deduction_fen=500_000,
        brackets=(TaxBracket(None, Decimal("0.03"), 0),),
    )


def tax_input(
    income_date: date,
    *,
    withholding_start_date: date = date(2026, 1, 1),
    income_fen: int = 2_000_000,
) -> CumulativeTaxPeriodInput:
    return CumulativeTaxPeriodInput(
        income_date=income_date,
        withholding_start_date=withholding_start_date,
        income_fen=income_fen,
        tax_exempt_income_fen=0,
        employee_contributions_fen=0,
        special_additional_deduction_fen=0,
        other_legal_deduction_fen=0,
    )


def policy_parameters() -> dict[str, object]:
    return {
        "contribution_rules": [
            {
                "code": "pension",
                "base_kind": "social_insurance",
                "employee_rate": "0.08",
                "employer_rate": "0.16",
                "minimum_base_fen": 0,
                "maximum_base_fen": 10_000_000,
                "rounding_rule": "half_up",
            }
        ],
        "income_tax": {
            "version": "income-v1",
            "primary_source_url": "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
            "legal_basis_source_url": "https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "monthly_standard_deduction_fen": 500_000,
            "brackets": [{"upper_bound_fen": None, "rate": "0.03", "quick_deduction_fen": 0}],
        },
        "annual_bonus": {
            "version": "bonus-v1",
            "primary_source_url": "https://m.mof.gov.cn/czxw/202308/t20230828_3904328.htm",
            "effective_from": "2023-01-01",
            "effective_to": "2027-12-31",
            "brackets": [
                {
                    "upper_monthly_average_fen": None,
                    "rate": "0.03",
                    "quick_deduction_fen": 0,
                }
            ],
        },
        "payment_targets": {
            "social_insurance": {"agency_code": "SOCIAL", "agency_name": "Social"},
            "housing_fund": {"agency_code": "HOUSING", "agency_name": "Housing"},
            "individual_income_tax": {"agency_code": "TAX", "agency_name": "Tax"},
        },
    }


def test_pay003_tax_year_resets_on_actual_january_payment_date() -> None:
    december = calculate_cumulative_withholding(
        tax_policy(),
        YearMonth(2026, 12),
        CumulativeTaxState.empty(2026),
        tax_input(date(2026, 12, 31)),
    )
    january = calculate_cumulative_withholding(
        tax_policy(),
        YearMonth(2027, 1),
        CumulativeTaxState.empty(2027),
        tax_input(date(2027, 1, 5)),
    )

    assert december.new_state.tax_year == 2026
    assert january.new_state.tax_year == 2027
    assert january.new_state.cumulative_income_fen == 2_000_000
    assert january.new_state.cumulative_standard_deduction_fen == 500_000


def test_pay004_standard_deduction_counts_zero_wage_months_and_midyear_hire() -> None:
    january = calculate_cumulative_withholding(
        tax_policy(),
        YearMonth(2026, 1),
        CumulativeTaxState.empty(2026),
        tax_input(date(2026, 1, 31)),
    )
    # February has no payroll.  March still accrues all three employment months.
    march = calculate_cumulative_withholding(
        tax_policy(),
        YearMonth(2026, 3),
        january.new_state,
        tax_input(date(2026, 3, 31)),
    )
    hired_in_march = calculate_cumulative_withholding(
        tax_policy(),
        YearMonth(2026, 3),
        CumulativeTaxState.empty(2026),
        tax_input(date(2026, 3, 31), withholding_start_date=date(2026, 3, 15)),
    )

    assert march.new_state.cumulative_standard_deduction_fen == 1_500_000
    assert hired_in_march.new_state.cumulative_standard_deduction_fen == 500_000


def test_pay005_combined_bonus_requires_batch_reference_and_rejects_free_wage_input() -> None:
    base = {
        "org_id": "00000000-0000-0000-0000-000000000001",
        "idempotency_key": "bonus-combined",
        "batch_kind": "annual_bonus",
        "payroll_period": "2026-12",
        "posting_date": "2026-12-31",
        "payment_date": "2026-12-31",
        "tax_method": "combined",
        "employee_items": [
            {
                "employee_id": "00000000-0000-0000-0000-000000000002",
                "annual_bonus_fen": 1_000_000,
            }
        ],
    }
    with pytest.raises(ValidationError, match="regular_payroll_batch_id"):
        PreviewPayrollRequest.model_validate(base)

    free_input = {
        **base,
        "tax_method": "separate",
        "employee_items": [
            {
                **base["employee_items"][0],
                "regular_tax_input": {"income_fen": 1_000_000},
            }
        ],
    }
    with pytest.raises(ValidationError, match="regular_tax_input"):
        PreviewPayrollRequest.model_validate(free_input)


def test_pay006_actual_income_date_controls_expiry_while_contributions_use_wage_period() -> None:
    contribution_policy = ContributionPolicy(
        version="contribution-2027",
        jurisdiction="test",
        effective_from=date(2027, 1, 1),
        effective_to=date(2027, 12, 31),
        primary_source_url="https://www.mof.gov.cn/",
        rules=(
            ContributionRule(
                code="pension",
                base_kind=ContributionBaseKind.SOCIAL_INSURANCE,
                employee_rate=Decimal("0.08"),
                employer_rate=Decimal("0.16"),
                minimum_base_fen=0,
                maximum_base_fen=10_000_000,
                rounding_rule=RoundingRule.HALF_UP,
            ),
        ),
    )
    assert (
        calculate_contributions(
            contribution_policy, ContributionBases(1_000_000, None), date(2027, 12, 31)
        ).policy_version
        == "contribution-2027"
    )
    with pytest.raises(ExpiredPolicyError):
        calculate_cumulative_withholding(
            tax_policy(effective_to=date(2027, 12, 31)),
            YearMonth(2028, 1),
            CumulativeTaxState.empty(2028),
            tax_input(date(2028, 1, 1)),
        )


def test_pay021_each_tax_calculator_requires_its_own_versioned_absolute_source() -> None:
    with pytest.raises(CalculationValidationError, match="source"):
        CumulativeIncomeTaxPolicy(
            version="v1",
            effective_from=date(2026, 1, 1),
            effective_to=None,
            primary_source_url="not-an-url",
            legal_basis_source_url="https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
            monthly_standard_deduction_fen=500_000,
            brackets=(TaxBracket(None, Decimal("0.03"), 0),),
        )
    with pytest.raises(CalculationValidationError, match="source"):
        AnnualBonusTaxPolicy(
            version="bonus-v1",
            effective_from=date(2026, 1, 1),
            effective_to=None,
            primary_source_url="",
            brackets=(AnnualBonusBracket(None, Decimal("0.03"), 0),),
        )


def test_pay005_uses_immutable_posted_regular_withholding_amount_for_combined_delta() -> None:
    scenarios = calculate_annual_bonus_scenarios(
        AnnualBonusTaxPolicy(
            version="bonus-v1",
            effective_from=date(2026, 1, 1),
            effective_to=None,
            primary_source_url="https://m.mof.gov.cn/czxw/202308/t20230828_3904328.htm",
            brackets=(AnnualBonusBracket(None, Decimal("0.03"), 0),),
        ),
        tax_policy(),
        AnnualBonusScenarioInput(
            period=YearMonth(2026, 12),
            payment_date=date(2026, 12, 31),
            bonus_fen=1_000_000,
            prior_tax_state=CumulativeTaxState.empty(2026),
            regular_period_input=tax_input(date(2026, 12, 15)),
            usage=AnnualBonusUsage(2026, False),
            regular_current_withholding_fen=9_999,
        ),
    )
    assert scenarios.combined.tax_fen is not None
    assert scenarios.trace[-1].values["regular_posted_tax_fen"] == 9_999


def test_pay011_effective_version_ranges_overlap_at_closed_and_open_boundaries() -> None:
    assert FinanceService._effective_date_ranges_overlap(
        date(2026, 1, 1), date(2026, 6, 30), date(2026, 6, 30), date(2026, 12, 31)
    )
    assert not FinanceService._effective_date_ranges_overlap(
        date(2026, 1, 1), date(2026, 6, 30), date(2026, 7, 1), None
    )
    with pytest.raises(CalculationValidationError, match="maximum_base"):
        ContributionRule(
            code="invalid-range",
            base_kind=ContributionBaseKind.SOCIAL_INSURANCE,
            employee_rate=Decimal("0.08"),
            employer_rate=Decimal("0.16"),
            minimum_base_fen=1_000,
            maximum_base_fen=999,
            rounding_rule=RoundingRule.HALF_UP,
        )


def test_pay011_and_pay021_registration_reject_overlaps_and_incomplete_sources(
    session, organization
) -> None:
    service = FinanceService(session)
    request = RegisterPayrollPolicyVersionRequest(
        org_id=organization.id,
        region="CN-test",
        effective_from=date(2026, 1, 1),
        effective_to=date(2026, 12, 31),
        version="v1",
        source_url="https://www.chinatax.gov.cn/chinatax/n810341/n810765/n3359382/201812/c4182700/content.html",
        parameters=policy_parameters(),
    )
    assert service.register_payroll_policy_version(request)["status"] == "registered"
    overlapping = request.model_copy(update={"version": "v2"})
    assert service.register_payroll_policy_version(overlapping)["errors"] == [
        "OVERLAPPING_PAYROLL_POLICY_VERSION"
    ]
    incomplete = request.model_copy(
        update={
            "region": "CN-other",
            "version": "v3",
            "parameters": {
                **policy_parameters(),
                "income_tax": {
                    **policy_parameters()["income_tax"],
                    "primary_source_url": "",
                },
            },
        }
    )
    assert service.register_payroll_policy_version(incomplete)["status"] == "rejected"
