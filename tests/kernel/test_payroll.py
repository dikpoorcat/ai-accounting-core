from __future__ import annotations

import subprocess
import sys
from dataclasses import replace
from decimal import Inexact, localcontext

import pytest
from pydantic import ValidationError

from ai_accounting.kernel.contracts import (
    Calculation,
    Context,
    FactVersion,
    KernelError,
    NeedsInformation,
    Read,
    Registry,
)
from ai_accounting.kernel.domains.payroll import (
    AnnualBonus,
    AnnualBonusOpeningUsage,
    AnnualBonusPolicy,
    ContributionActualItem,
    ContributionRuleFact,
    LaborIncomeTaxPolicy,
    LaborRemuneration,
    Payroll,
    PayrollContributionActual,
    PayrollContributionPolicy,
    PayrollFirstWageTreatment,
    PayrollIncomeTaxPolicy,
    PayrollOpeningState,
    PayrollProfile,
    TaxBracketFact,
    calculate_annual_bonus,
    calculate_labor,
    calculate_payroll,
    employee_month,
    employee_year,
    register,
)
from ai_accounting.kernel.domains.transactions import Allocation, Payment, calculate_payment
from ai_accounting.kernel.types import MAX_FEN
from ai_accounting.payroll import AnnualBonusTaxPolicy, CumulativeIncomeTaxPolicy


def version(fact, subject="subject", *, revision=1, evidence=("evidence-sha256",)):
    return FactVersion(f"{subject}:v{revision}", subject, revision, fact, evidence)


def context_for(current, facts=(), calculations=()):
    """Build only the calculator's declared selections, including every empty scope."""
    facts = [item for item in facts if item.subject_id != current.subject_id] + [current]
    selections = {}
    for read in current.fact.reads():
        if read.source == "fact":
            selections[read] = tuple(
                item
                for item in facts
                if (read.kind == "*" or item.fact.kind == read.kind)
                and read.key in (f"@{item.subject_id}", *item.fact.scopes())
            )
        else:
            selections[read] = tuple(
                item
                for item in calculations
                if item.kind == read.kind
                and read.key
                in (
                    f"@{item.subject_id}",
                    employee_month(item.values.get("employee_id", ""), item.period),
                )
            )
    return Context(selections)


def as_calculation(current, result):
    return Calculation(
        f"calculation:{current.id}",
        current.subject_id,
        current.fact.kind,
        current.fact.period,
        result.values,
        current.id,
    )


def contribution_policy():
    return PayrollContributionPolicy(
        period="2026-01",
        version="test-contribution-2026",
        jurisdiction="test",
        effective_from="2026-01-01",
        effective_to="2026-12-31",
        primary_source_url="https://www.mof.gov.cn/",
        rules=(
            ContributionRuleFact(
                code="pension",
                base_kind="social_insurance",
                employee_rate="0.08",
                employer_rate="0.16",
                minimum_base_fen=0,
                maximum_base_fen=10_000_000,
                rounding="half_up",
                enabled=True,
            ),
        ),
    )


def income_tax_policy():
    source = CumulativeIncomeTaxPolicy.china_resident_wage_withholding()
    return PayrollIncomeTaxPolicy(
        period="2026-01",
        version=source.version,
        effective_from=source.effective_from.isoformat(),
        effective_to=None,
        primary_source_url=source.primary_source_url,
        legal_basis_source_url=source.legal_basis_source_url,
        monthly_standard_deduction_fen=source.monthly_standard_deduction_fen,
        brackets=tuple(
            TaxBracketFact(
                upper_bound_fen=x.upper_bound_fen,
                rate=str(x.rate),
                quick_deduction_fen=x.quick_deduction_fen,
            )
            for x in source.brackets
        ),
    )


def profile(**changes):
    return PayrollProfile(
        **{
            "period": "2026-01",
            "employee_id": "employee",
            "effective_from": "2026-01",
            "effective_to": None,
            "withholding_start_date": "2026-01-01",
            "social_insurance_base_fen": 1_000_000,
            "housing_fund_base_fen": None,
            "social_insurance_participating": True,
            "housing_fund_participating": False,
            "contribution_shortfall": "reject",
            **changes,
        }
    )


def opening(**changes):
    return PayrollOpeningState(
        **{
            "period": "2026-01",
            "employee_id": "employee",
            "through_period": None,
            "cumulative_income_fen": 0,
            "cumulative_tax_exempt_income_fen": 0,
            "cumulative_standard_deduction_fen": 0,
            "cumulative_employee_contributions_fen": 0,
            "cumulative_special_additional_deduction_fen": 0,
            "cumulative_other_legal_deduction_fen": 0,
            "cumulative_tax_relief_fen": 0,
            "cumulative_withheld_tax_fen": 0,
            **changes,
        }
    )


def payroll(**changes):
    return Payroll(
        **{
            "period": "2026-01",
            "employee_id": "employee",
            "profile_id": "profile",
            "contribution_policy_id": "contributions",
            "income_tax_policy_id": "income-tax",
            "accounting_gross_salary_fen": 1_000_000,
            "tax_reported_salary_fen": 1_000_000,
            "tax_exempt_income_fen": 0,
            "special_additional_deduction_fen": 0,
            "other_legal_deduction_fen": 0,
            "tax_relief_fen": 0,
            "expense_class": "management",
            "contribution_basis": "policy_until_actual",
            **changes,
        }
    )


def payroll_sources():
    return [
        version(profile(), "profile"),
        version(contribution_policy(), "contributions"),
        version(income_tax_policy(), "income-tax"),
        version(opening(), "opening"),
    ]


def actual(period="2026-01", employee=100_000, employer=200_000):
    return PayrollContributionActual(
        period=period,
        employee_id="employee",
        items=(
            ContributionActualItem(
                code="pension",
                base_kind="social_insurance",
                state="declared",
                employee_amount_fen=employee,
                employer_amount_fen=employer,
            ),
        ),
    )


def bonus_policy():
    source = AnnualBonusTaxPolicy.china_annual_bonus_2024_to_2027()
    return AnnualBonusPolicy(
        period="2026-01",
        version=source.version,
        effective_from=source.effective_from.isoformat(),
        effective_to=source.effective_to.isoformat(),
        primary_source_url=source.primary_source_url,
        brackets=tuple(
            TaxBracketFact(
                upper_bound_fen=x.upper_monthly_average_fen,
                rate=str(x.rate),
                quick_deduction_fen=x.quick_deduction_fen,
            )
            for x in source.brackets
        ),
    )


def bonus(**changes):
    return AnnualBonus(
        **{
            "period": "2026-01",
            "employee_id": "employee",
            "income_date": "2026-01-31",
            "bonus_fen": 3_000_000,
            "expense_class": "management",
            "bonus_policy_id": "bonus-policy",
            "income_tax_policy_id": "income-tax",
            "tax_method": "separate",
            **changes,
        }
    )


def bonus_sources():
    return [
        *payroll_sources(),
        version(bonus_policy(), "bonus-policy"),
        version(
            AnnualBonusOpeningUsage(
                period="2026-01",
                employee_id="employee",
                separate_method_already_used=False,
            ),
            "bonus-usage",
        ),
    ]


def labor_policy():
    return LaborIncomeTaxPolicy(
        period="2026-01",
        version="cn-resident-labor-2019.1",
        effective_from="2019-01-01",
        effective_to=None,
        primary_source_url="https://12366.chinatax.gov.cn/bzds/070/070-5-4.html",
        small_payment_threshold_fen=400_000,
        fixed_expense_deduction_fen=80_000,
        large_payment_expense_rate="0.20",
        brackets=(
            TaxBracketFact(upper_bound_fen=2_000_000, rate="0.20", quick_deduction_fen=0),
            TaxBracketFact(upper_bound_fen=5_000_000, rate="0.30", quick_deduction_fen=200_000),
            TaxBracketFact(upper_bound_fen=None, rate="0.40", quick_deduction_fen=700_000),
        ),
    )


def labor(**changes):
    return LaborRemuneration(
        **{
            "period": "2026-01",
            "person_id": "contractor",
            "income_date": "2026-01-31",
            "policy_id": "labor-policy",
            "expense_class": "service",
            "recipient_tax_status": "ordinary_resident",
            "remuneration_method": "fixed",
            "withholding_method": "net_after_withholding",
            "fixed_fee_fen": 1_000_000,
            **changes,
        }
    )


def test_regular_payroll_produces_balanced_obligations_and_records_empty_actual_scope():
    current = version(payroll(), "january")
    ctx = context_for(current, payroll_sources())
    result = calculate_payroll(current, ctx)
    assert result.values["tax_fen"] == 12_600
    assert result.values["net_fen"] == 907_400
    assert result.values["tax_state"]["cumulative_income_fen"] == 1_000_000
    assert sum(x.debit for x in result.lines) == sum(x.credit for x in result.lines) == 1_160_000
    assert (
        Read("fact", "payroll_contribution_actual", employee_month("employee", "2026-01"))
        in ctx.used
    )
    assert (
        Read("fact", "payroll_first_wage_treatment", employee_year("employee", "2026-01"))
        in ctx.used
    )
    assert result.values["rule_versions"] == ["contributions:v1", "income-tax:v1"]
    assert (
        next(x for x in result.values["obligations"] if x["name"] == "net")["key"]
        == "payroll:january:net"
    )
    assert not any(item.account == "1002" for item in result.lines)


def test_later_actual_source_recalculates_current_and_following_cumulative_tax():
    sources = payroll_sources()
    january = version(payroll(), "january")
    original = calculate_payroll(january, context_for(january, sources))
    february = version(payroll(period="2026-02"), "february")
    original_next = calculate_payroll(
        february,
        context_for(
            february,
            sources,
            [as_calculation(january, original)],
        ),
    )
    actual_source = version(actual(), "actual")
    corrected_ctx = context_for(january, [*sources, actual_source])
    corrected = calculate_payroll(january, corrected_ctx)
    corrected_next = calculate_payroll(
        february,
        context_for(
            february,
            [*sources, actual_source],
            [as_calculation(january, corrected)],
        ),
    )
    assert corrected.values["net_fen"] == 888_000
    assert corrected.values["tax_fen"] == 12_000
    assert corrected_next.values["tax_state"]["cumulative_withheld_tax_fen"] == 24_600
    assert original_next.values["tax_state"]["cumulative_withheld_tax_fen"] == 25_200
    assert "actual:v1" in corrected_ctx.versions
    assert original.values["net_fen"] == 907_400  # Previously produced results are not mutated.
    assert all(read.key != employee_month("employee", "2026-03") for read in february.fact.reads())


def test_corrected_payroll_never_changes_actual_payment_or_silently_absorbs_overpayment():
    sources = payroll_sources()
    january = version(payroll(), "january")
    original = calculate_payroll(january, context_for(january, sources))
    payment = version(
        Payment(
            period="2026-02",
            actual_date="2026-02-10",
            direction="outflow",
            bank_account_id="bank",
            counterparty_id="employee",
            amount_fen=907_400,
            allocations=(
                Allocation(
                    source_kind="payroll", source_id="january", obligation="net", amount_fen=907_400
                ),
            ),
        ),
        "payment",
    )
    before = payment.fact.model_dump(mode="json")
    paid = calculate_payment(payment, context_for(payment, (), [as_calculation(january, original)]))
    assert paid.values["amount_fen"] == 907_400
    corrected = calculate_payroll(
        january, context_for(january, [*sources, version(actual(), "actual")])
    )
    with pytest.raises(NeedsInformation) as error:
        calculate_payment(payment, context_for(payment, (), [as_calculation(january, corrected)]))
    assert error.value.response()["fact_issues"][0]["field"] == "overpayment"
    assert payment.fact.model_dump(mode="json") == before
    assert all(line.account != "1002" for line in corrected.lines)


def test_actual_amounts_remove_need_for_unknown_policy_estimation_base():
    current = version(payroll(contribution_basis="actual_required"), "january")
    sources = [x for x in payroll_sources() if x.subject_id != "profile"]
    sources += [
        version(profile(social_insurance_base_fen=None), "profile"),
        version(actual(), "actual"),
    ]
    result = calculate_payroll(current, context_for(current, sources))
    assert result.values["net_fen"] == 888_000
    assert not any(x["step"] == "contribution_line" for x in result.explanation)
    assert any(x["step"] == "contribution_actual" for x in result.explanation)


@pytest.mark.parametrize("missing", ["opening", "profile", "income-tax", "contributions"])
def test_missing_required_source_returns_structured_needs_information(missing):
    current = version(payroll(), "january")
    sources = [x for x in payroll_sources() if x.subject_id != missing]
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(current, context_for(current, sources))
    assert error.value.response()["status"] == "needs_information"
    assert error.value.response()["fact_issues"][0]["semantics"] == "accounting"


def test_missing_contribution_base_without_actual_is_not_zero():
    current = version(payroll(), "january")
    sources = [x for x in payroll_sources() if x.subject_id != "profile"]
    sources.append(version(profile(social_insurance_base_fen=None), "profile"))
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(current, context_for(current, sources))
    assert error.value.response()["fact_issues"][0]["field"] == "social_insurance_base_fen"


def test_actual_required_does_not_silently_fall_back_to_policy():
    current = version(payroll(contribution_basis="actual_required"), "january")
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(current, context_for(current, payroll_sources()))
    assert error.value.response()["fact_issues"][0]["field"] == "contribution_actual.items"


def test_contribution_actual_requires_evidence_and_unique_source():
    current = version(payroll(), "january")
    with pytest.raises(NeedsInformation):
        calculate_payroll(
            current,
            context_for(
                current,
                [
                    *payroll_sources(),
                    version(actual(), "actual", evidence=()),
                ],
            ),
        )
    with pytest.raises(KernelError) as error:
        calculate_payroll(
            current,
            context_for(
                current,
                [
                    *payroll_sources(),
                    version(actual(), "actual"),
                    version(actual(), "duplicate-actual"),
                ],
            ),
        )
    assert error.value.code == "ambiguous_source"


def test_first_wage_source_is_tracked_and_recalculates_deduction():
    current = version(payroll(period="2026-06"), "june")
    sources = [x for x in payroll_sources() if x.subject_id != "profile"]
    sources.append(version(profile(withholding_start_date="2026-06-01"), "profile"))
    before = calculate_payroll(current, context_for(current, sources))
    treatment = version(
        PayrollFirstWageTreatment(
            period="2026-06",
            employee_id="employee",
            standard_deduction_start_month=1,
        ),
        "first-wage",
    )
    ctx = context_for(current, [*sources, treatment])
    after = calculate_payroll(current, ctx)
    assert before.values["tax_fen"] == 12_600
    assert after.values["tax_fen"] == 0
    assert after.values["tax_state"]["cumulative_standard_deduction_fen"] == 3_000_000
    assert treatment.id in ctx.versions


def test_accounting_salary_is_not_replaced_by_reported_tax_salary():
    current = version(payroll(accounting_gross_salary_fen=1_200_000), "january")
    result = calculate_payroll(current, context_for(current, payroll_sources()))
    assert result.values["gross_fen"] == 1_200_000
    assert result.values["tax_state"]["cumulative_income_fen"] == 1_000_000
    assert result.values["net_fen"] == 1_107_400


def test_missing_month_in_cumulative_history_is_not_inferred_as_zero_income():
    january = version(payroll(), "january")
    first = calculate_payroll(january, context_for(january, payroll_sources()))
    march = version(payroll(period="2026-03"), "march")
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(
            march,
            context_for(
                march,
                payroll_sources(),
                [as_calculation(january, first)],
            ),
        )
    assert (
        error.value.response()["fact_issues"][0]["field"] == "payroll_opening_state.through_period"
    )


@pytest.mark.parametrize("value", [True, 1.5, MAX_FEN + 1, "10000"])
def test_money_rejects_boolean_float_overflow_and_string(value):
    with pytest.raises(ValidationError):
        payroll(accounting_gross_salary_fen=value)


def test_duplicate_employee_month_and_wrong_profile_are_rejected():
    current = version(payroll(), "january")
    with pytest.raises(KernelError) as duplicate:
        calculate_payroll(
            current, context_for(current, [*payroll_sources(), version(payroll(), "other")])
        )
    assert duplicate.value.code == "duplicate_remuneration"
    sources = [x for x in payroll_sources() if x.subject_id != "profile"]
    sources.append(version(profile(employee_id="someone-else"), "profile"))
    with pytest.raises(KernelError) as mismatch:
        calculate_payroll(current, context_for(current, sources))
    assert mismatch.value.code == "payroll_profile_mismatch"


def test_policy_effective_date_is_enforced():
    current = version(payroll(period="2027-01"), "later")
    with pytest.raises(KernelError) as error:
        calculate_payroll(current, context_for(current, payroll_sources()))
    assert error.value.code == "policy_not_effective"


def test_zero_salary_requires_explicit_employer_burden_treatment():
    current = version(payroll(accounting_gross_salary_fen=0, tax_reported_salary_fen=0), "january")
    with pytest.raises(KernelError) as error:
        calculate_payroll(current, context_for(current, payroll_sources()))
    assert error.value.code == "negative_net_pay"
    sources = [x for x in payroll_sources() if x.subject_id != "profile"]
    sources.append(version(profile(contribution_shortfall="employer_borne"), "profile"))
    result = calculate_payroll(current, context_for(current, sources))
    assert result.values["net_fen"] == result.values["employee_contributions_fen"] == 0
    assert result.values["employer_contributions_fen"] == 240_000


def test_bonus_requires_explicit_method_and_known_prior_usage():
    current = version(bonus(tax_method=None), "bonus")
    with pytest.raises(NeedsInformation) as choice:
        calculate_annual_bonus(current, context_for(current, bonus_sources()))
    assert choice.value.response()["fact_issues"][0]["field"] == "tax_method"
    current = version(bonus(), "bonus")
    with pytest.raises(NeedsInformation):
        calculate_annual_bonus(
            current,
            context_for(current, [x for x in bonus_sources() if x.subject_id != "bonus-usage"]),
        )


def test_separate_bonus_preserves_wage_cumulative_state_and_annual_usage():
    current = version(bonus(), "bonus")
    result = calculate_annual_bonus(current, context_for(current, bonus_sources()))
    assert result.values["tax_fen"] == 90_000
    assert result.values["net_fen"] == 2_910_000
    assert result.values["tax_state"] is None
    prior = version(bonus(period="2026-02", income_date="2026-02-20"), "other-bonus")
    with pytest.raises(KernelError) as error:
        calculate_annual_bonus(current, context_for(current, [*bonus_sources(), prior]))
    assert error.value.code == "annual_bonus_separate_method_already_used"


def test_combined_bonus_feeds_next_month_tax_state():
    january = version(payroll(), "january")
    wage_result = calculate_payroll(january, context_for(january, payroll_sources()))
    wage_calculation = as_calculation(january, wage_result)
    current = version(bonus(tax_method="combined", regular_payroll_id="january"), "bonus")
    result = calculate_annual_bonus(
        current, context_for(current, bonus_sources(), [wage_calculation])
    )
    assert result.values["tax_fen"] == 90_000
    assert result.values["tax_state"]["cumulative_income_fen"] == 4_000_000
    february = version(payroll(period="2026-02"), "february")
    next_result = calculate_payroll(
        february,
        context_for(
            february,
            payroll_sources(),
            [wage_calculation, as_calculation(current, result)],
        ),
    )
    assert next_result.values["tax_state"]["cumulative_income_fen"] == 5_000_000
    assert next_result.values["tax_fen"] == 29_400


def test_combined_bonus_cannot_use_another_employee_or_missing_payroll():
    current = version(bonus(tax_method="combined"), "bonus")
    with pytest.raises(NeedsInformation) as error:
        calculate_annual_bonus(current, context_for(current, bonus_sources()))
    assert error.value.response()["fact_issues"][0]["field"] == "regular_payroll_id"
    current = version(bonus(tax_method="combined", regular_payroll_id="january"), "bonus")
    january = version(payroll(), "january")
    wage_result = calculate_payroll(january, context_for(january, payroll_sources()))
    wrong = replace(
        as_calculation(january, wage_result), values=wage_result.values | {"employee_id": "other"}
    )
    with pytest.raises(KernelError) as error:
        calculate_annual_bonus(current, context_for(current, bonus_sources(), [wrong]))
    assert error.value.code == "bonus_regular_mismatch"


@pytest.mark.parametrize(
    ("gross", "expected_tax"),
    [
        (80_000, 0),
        (400_000, 64_000),
        (400_001, 64_000),
        (1_000_000, 160_000),
        (2_500_000, 400_000),
        (2_500_001, 400_000),
        (6_250_000, 1_300_000),
    ],
)
def test_labor_withholding_boundaries(gross, expected_tax):
    current = version(labor(fixed_fee_fen=gross), "labor")
    result = calculate_labor(
        current, context_for(current, [version(labor_policy(), "labor-policy")])
    )
    assert result.values["tax_fen"] == expected_tax
    assert result.values["net_fen"] == gross - expected_tax
    assert sum(x.debit for x in result.lines) == sum(x.credit for x in result.lines) == gross


def test_commission_rounds_integer_ratio_without_float():
    current = version(
        labor(
            remuneration_method="commission",
            fixed_fee_fen=None,
            commission_base_fen=1_000_001,
            commission_rate_ppm=500_000,
        ),
        "commission",
    )
    result = calculate_labor(
        current, context_for(current, [version(labor_policy(), "labor-policy")])
    )
    assert result.values["gross_fen"] == 500_001


def test_missing_labor_facts_need_information_and_conflicting_facts_are_invalid():
    current = version(labor(fixed_fee_fen=None), "labor")
    with pytest.raises(NeedsInformation) as error:
        calculate_labor(current, context_for(current, [version(labor_policy(), "labor-policy")]))
    assert error.value.response()["fact_issues"][0]["field"] == "fixed_fee_fen"
    with pytest.raises(ValidationError):
        labor(commission_base_fen=100, commission_rate_ppm=10)
    with pytest.raises(ValidationError):
        labor(income_date="2026-02-01")


def test_labor_withholding_treatment_is_explicit_and_gross_history_needs_actual_payment():
    for fields, missing in (
        ({"withholding_method": None}, "withholding_method"),
        ({"withholding_method": "gross_paid_without_withholding"}, "gross_payment_id"),
    ):
        current = version(labor(**fields), "labor")
        with pytest.raises(NeedsInformation) as failure:
            calculate_labor(
                current, context_for(current, [version(labor_policy(), "labor-policy")])
            )
        assert failure.value.issues[0]["field"] == missing


def test_labor_gross_paid_history_keeps_theoretical_tax_without_fictitious_withholding():
    current = version(
        labor(
            withholding_method="gross_paid_without_withholding",
            gross_payment_kind="payment",
            gross_payment_id="actual-payment",
        ),
        "labor",
    )
    paid = version(
        Payment(
            period="2026-01",
            actual_date="2026-01-31",
            direction="outflow",
            bank_account_id="bank",
            counterparty_id="contractor",
            amount_fen=1_000_000,
            allocations=(
                Allocation(
                    source_kind="labor", source_id="labor", obligation="net", amount_fen=1_000_000
                ),
            ),
        ),
        "actual-payment",
    )
    result = calculate_labor(
        current, context_for(current, [version(labor_policy(), "labor-policy"), paid])
    )
    assert result.values["tax_fen"] == 0
    assert result.values["theoretical_tax_fen"] == result.values["unwithheld_tax_fen"] == 160_000
    assert result.values["net_fen"] == 1_000_000
    assert [item["name"] for item in result.values["obligations"]] == ["net"]
    assert not any(line.account == "222103" for line in result.lines)


@pytest.mark.parametrize(
    "change,code",
    [
        ({"actual_date": "2026-01-30"}, "gross_labor_payment_conflict"),
        ({"counterparty_id": "other-person"}, "gross_labor_recipient_conflict"),
    ],
)
def test_labor_gross_history_checks_real_date_and_recipient(change, code):
    current = version(
        labor(
            withholding_method="gross_paid_without_withholding",
            gross_payment_kind="payment",
            gross_payment_id="actual-payment",
        ),
        "labor",
    )
    paid = version(
        Payment(
            **(
                {
                    "period": "2026-01",
                    "actual_date": "2026-01-31",
                    "direction": "outflow",
                    "bank_account_id": "bank",
                    "counterparty_id": "contractor",
                    "amount_fen": 1_000_000,
                    "allocations": (
                        Allocation(
                            source_kind="labor",
                            source_id="labor",
                            obligation="net",
                            amount_fen=1_000_000,
                        ),
                    ),
                }
                | change
            )
        ),
        "actual-payment",
    )
    with pytest.raises(KernelError) as failure:
        calculate_labor(
            current, context_for(current, [version(labor_policy(), "labor-policy"), paid])
        )
    assert failure.value.code == code


def test_registry_has_only_typed_fact_inputs_and_immutable_policy_versions():
    registry = Registry()
    register(registry)
    assert set(registry.evaluators) == {
        "payroll",
        "payroll_bounded",
        "annual_bonus",
        "labor",
        "labor_accrual",
    }
    assert all("account" not in model.model_fields for model in registry.models.values())
    assert PayrollIncomeTaxPolicy.immutable is True
    assert Payroll.identity_fields == ("employee_id", "period")
    assert len(registry.schemas()) == 15


def test_calculators_ignore_callers_decimal_precision_rounding_and_traps():
    current_payroll = version(payroll(), "january")
    current_bonus = version(bonus(bonus_fen=3_000_001), "bonus")
    current_labor = version(labor(fixed_fee_fen=1_000_001), "labor")
    calculations = (
        (calculate_payroll, current_payroll, payroll_sources()),
        (calculate_annual_bonus, current_bonus, bonus_sources()),
        (calculate_labor, current_labor, [version(labor_policy(), "labor-policy")]),
    )
    expected = [
        calculator(current, context_for(current, sources))
        for calculator, current, sources in calculations
    ]
    with localcontext() as math:
        math.prec = 2
        math.traps[Inexact] = True
        actual_results = [
            calculator(current, context_for(current, sources))
            for calculator, current, sources in calculations
        ]
    assert actual_results == expected


def test_payroll_import_does_not_load_any_database_or_legacy_service():
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; import ai_accounting.kernel.domains.payroll; "
            "assert not any(n.startswith(('sqlalchemy', 'psycopg')) for n in sys.modules); "
            "assert 'ai_accounting.service' not in sys.modules; "
            "assert 'ai_accounting.models' not in sys.modules",
        ],
        check=False,
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
