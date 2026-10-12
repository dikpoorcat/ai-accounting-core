from __future__ import annotations

import pytest
from pydantic import ValidationError

from ai_accounting.kernel.contracts import (
    Calculation,
    Context,
    FactVersion,
    NeedsInformation,
    Registry,
)
from ai_accounting.kernel.domains.payroll import (
    AnnualBonus,
    AnnualBonusPolicy,
    ContributionActualItem,
    ContributionRuleFact,
    LaborIncomeTaxPolicy,
    LaborRemuneration,
    Payroll,
    PayrollContributionActual,
    PayrollContributionPolicy,
    PayrollIncomeTaxPolicy,
    PayrollOpeningState,
    PayrollProfile,
    TaxBracketFact,
    calculate_annual_bonus,
    calculate_labor,
    calculate_payroll,
    employee_month,
    register,
)
from ai_accounting.kernel.payroll_confirmation import FactRevisionReference
from ai_accounting.kernel.payroll_preparation import PayrollPlan
from ai_accounting.kernel.remuneration_tax_basis import RemunerationTaxBasis
from ai_accounting.kernel.types import MAX_FEN
from ai_accounting.payroll import AnnualBonusTaxPolicy, CumulativeIncomeTaxPolicy


def version(fact, subject="subject", *, revision=1, evidence=("evidence-sha256",)):
    return FactVersion(f"{subject}:v{revision}", subject, revision, fact, evidence)


def context_for(current, facts=(), calculations=()):
    """Build only the calculator's declared selections, including every empty scope."""
    facts = [item for item in facts if item.subject_id != current.subject_id] + [current]
    selections = {}
    for read in current.fact.reads_for(current.subject_id):
        if read.source == "fact":
            selections[read] = tuple(
                item
                for item in facts
                if (read.kind == "*" or item.fact.kind == read.kind)
                and (read.before_period is None or item.fact.period < read.before_period)
                and (
                    read.key == "*"
                    or read.key == f"#{item.id}"
                    or read.key
                    in (str(item.fact.period), f"@{item.subject_id}", *item.fact.scopes())
                )
            )
        else:
            selections[read] = tuple(
                item
                for item in calculations
                if item.kind == read.kind
                and (
                    read.key == "*"
                    or read.key == f"#{item.id}"
                    or read.key
                    in (
                        f"@{item.subject_id}",
                        str(item.period),
                        employee_month(item.values.get("employee_id", ""), item.period),
                    )
                )
            )
    first_context = Context(selections)
    for read in current.fact.reads_after(current.subject_id, first_context):
        if read.source == "fact":
            selections[read] = tuple(
                item
                for item in facts
                if item.fact.kind == read.kind
                and (
                    read.key == "#" + item.id
                    or read.key == "@" + item.subject_id
                    or read.key in item.fact.scopes()
                )
            )
    return Context(selections)


def with_payroll_plan(current, facts):
    """Add the explicit synthetic owner confirmation required by this wage scenario."""
    wage = current.fact

    plan = PayrollPlan(
        period=wage.period,
        payroll_revision=FactRevisionReference(
            subject_id=current.subject_id, revision=current.revision
        ),
        change_notice_revisions=(),
    )
    return [*facts, version(plan, f"confirmation:{wage.employee_id}:{wage.period}")]


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
    # Explicit synthetic source identities become exact references at the factory boundary.
    for old, field in (
        ("profile_id", "profile_revision"),
        ("contribution_policy_id", "contribution_policy_revision"),
    ):
        if old in changes:
            changes[field] = {"subject_id": changes.pop(old), "revision": 1}
    return Payroll(
        **{
            "period": "2026-01",
            "employee_id": "employee",
            "profile_revision": {"subject_id": "profile", "revision": 1},
            "contribution_policy_revision": {"subject_id": "contributions", "revision": 1},
            "accounting_gross_salary_fen": 1_000_000,
            "expense_class": "management",
            "contribution_basis": "policy_until_actual",
            **changes,
        }
    )


def payroll_sources():
    return [version(profile(), "profile"), version(contribution_policy(), "contributions")]


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
            **changes,
        }
    )


def bonus_sources():
    # Current bonus accounting has no tax-rule or prior-usage sources.
    return []


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
            "expense_class": "service",
            "remuneration_method": "fixed",
            "fixed_fee_fen": 1_000_000,
            **changes,
        }
    )


def tax_basis(
    current, amount, *, subject=None, revision=1, basis_type="final_statement", **changes
):
    """A fixture requires the actual expected source amount; it never assesses PIT."""
    return version(
        RemunerationTaxBasis(
            **{
                "period": current.fact.period,
                "source_kind": current.fact.kind,
                "source_id": current.subject_id,
                "source_fact_id": current.id,
                "tax_period": current.fact.period,
                "tax_amount_fen": amount,
                "basis_type": basis_type,
                "confirmed": True,
                **changes,
            }
        ),
        subject or "tax:" + current.subject_id,
        revision=revision,
    )


def test_regular_payroll_uses_explicit_source_tax_and_balanced_obligations():
    current = version(payroll(), "wage")
    sources = with_payroll_plan(current, payroll_sources()) + [tax_basis(current, 90_000)]
    result = calculate_payroll(current, context_for(current, sources))
    assert result.values["gross_fen"] == 1_000_000
    assert result.values["employee_contributions_fen"] == 80_000
    assert result.values["tax_fen"] == 90_000
    assert result.values["net_fen"] == 830_000
    assert sum(item.debit for item in result.lines) == sum(item.credit for item in result.lines)
    assert result.values["rule_versions"] == ["contributions:v1"]


def test_regular_payroll_missing_tax_even_for_small_wages_requires_information():
    current = version(
        payroll(
            accounting_gross_salary_fen=1, profile_revision={"subject_id": "profile", "revision": 1}
        ),
        "wage",
    )
    sources = payroll_sources()
    sources[0] = version(profile(social_insurance_participating=False), "profile")
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(current, context_for(current, with_payroll_plan(current, sources)))
    assert error.value.issues[0]["field"] == "remuneration_tax_basis"


@pytest.mark.parametrize("value", [True, 1.0, "100", MAX_FEN + 1])
def test_money_rejects_boolean_float_overflow_and_string(value):
    with pytest.raises(ValidationError):
        payroll(accounting_gross_salary_fen=value)


def test_missing_contribution_base_remains_information_even_with_confirmed_tax():
    current = version(payroll(), "wage")
    sources = payroll_sources()
    sources[0] = version(profile(social_insurance_base_fen=None), "profile")
    with pytest.raises(NeedsInformation):
        calculate_payroll(
            current,
            context_for(current, with_payroll_plan(current, sources) + [tax_basis(current, 0)]),
        )


def test_actual_contribution_is_adopted_without_needing_estimation_base():
    current = version(payroll(contribution_basis="actual_required"), "wage")
    sources = payroll_sources()
    sources[0] = version(profile(social_insurance_base_fen=None), "profile")
    sources = with_payroll_plan(
        current, [*sources, version(actual(), "actual"), tax_basis(current, 50_000)]
    )
    result = calculate_payroll(current, context_for(current, sources))
    assert result.values["employee_contributions_fen"] == 100_000
    assert result.values["net_fen"] == 850_000


def test_bonus_and_commission_use_confirmed_tax_and_integer_gross_amount():
    annual = version(bonus(), "bonus")
    result = calculate_annual_bonus(annual, context_for(annual, [tax_basis(annual, 120_000)]))
    assert result.values["net_fen"] == 2_880_000
    commission = version(
        labor(
            remuneration_method="commission",
            fixed_fee_fen=None,
            commission_base_fen=100_001,
            commission_rate_ppm=333_333,
        ),
        "labor",
    )
    result = calculate_labor(commission, context_for(commission, [tax_basis(commission, 7_000)]))
    assert result.values["gross_fen"] == 33_334
    assert result.values["net_fen"] == 26_334
    assert "theoretical_tax_fen" not in result.values


def test_retired_tax_grammars_are_read_only_registry_models():
    registry = Registry()
    register(registry)
    assert "payroll_income_tax_policy" in registry.models
    assert "payroll_income_tax_policy" not in registry.input_models
    assert registry.evaluators["payroll"] is registry.evaluators["payroll_bounded"]
    assert "payroll_withholding_actual" not in registry.input_models
