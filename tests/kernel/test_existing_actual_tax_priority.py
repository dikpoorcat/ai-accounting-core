"""Actual source amounts remain authoritative across policy/reference changes."""

from dataclasses import replace

import pytest
from pydantic import ValidationError
from test_payroll import (
    bonus,
    context_for,
    labor,
    payroll,
    tax_basis,
    version,
    with_payroll_plan,
)
from test_tax_import import company as tax_company

from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.domains.payroll import (
    ContributionActualItem,
    ContributionRuleFact,
    PayrollBounded,
    PayrollContributionActual,
    PayrollContributionPolicy,
    PayrollProfile,
    calculate_annual_bonus,
    calculate_labor,
    calculate_payroll,
)
from ai_accounting.kernel.payroll_tax_declarations import PayrollTaxDeclarationActual


def contribution_sources(*, base_fen=1_000_000, rate="0.10"):
    return [
        version(
            PayrollProfile(
                period="2026-01",
                employee_id="employee",
                effective_from="2026-01",
                effective_to=None,
                social_insurance_base_fen=base_fen,
                housing_fund_base_fen=base_fen,
                social_insurance_participating=True,
                housing_fund_participating=True,
                contribution_shortfall="reject",
            ),
            "profile",
        ),
        version(
            PayrollContributionPolicy(
                period="2026-01",
                version="synthetic-contributions",
                jurisdiction="synthetic",
                effective_from="2026-01-01",
                effective_to=None,
                primary_source_url="https://www.mof.gov.cn/",
                rules=tuple(
                    ContributionRuleFact(
                        code=code,
                        base_kind=kind,
                        employee_rate=rate,
                        employer_rate=rate,
                        minimum_base_fen=0,
                        maximum_base_fen=10_000_000,
                        rounding="half_up",
                        enabled=True,
                    )
                    for code, kind in (
                        ("pension", "social_insurance"),
                        ("housing", "housing_fund"),
                    )
                ),
            ),
            "contributions",
        ),
    ]


def declared_contributions(*, employee_fen, employer_fen, state="declared"):
    return version(
        PayrollContributionActual(
            period="2026-01",
            employee_id="employee",
            items=tuple(
                ContributionActualItem(
                    code=code,
                    base_kind=kind,
                    state=state,
                    employee_amount_fen=employee_fen,
                    employer_amount_fen=employer_fen,
                )
                for code, kind in (
                    ("pension", "social_insurance"),
                    ("housing", "housing_fund"),
                )
            ),
        ),
        "actual-contributions",
    )


@pytest.mark.parametrize("bounded", [False, True])
@pytest.mark.parametrize("mode", ["policy_until_actual", "actual_required"])
@pytest.mark.parametrize("employee_fen,employer_fen", [(0, 0), (12_345, 54_321)])
def test_actual_social_and_housing_amounts_survive_policy_and_base_changes(
    bounded, mode, employee_fen, employer_fen
):
    wage_fact = payroll(contribution_basis=mode)
    if bounded:
        wage_fact = PayrollBounded(**wage_fact.model_dump())
    wage = version(wage_fact, "wage")
    actual = declared_contributions(employee_fen=employee_fen, employer_fen=employer_fen)
    zero_tax = tax_basis(wage, 0)
    outcomes = []
    for base, rate in ((1_000_000, "0.10"), (2_000_000, "0.20")):
        sources = with_payroll_plan(
            wage, [*contribution_sources(base_fen=base, rate=rate), actual, zero_tax]
        )
        outcomes.append(calculate_payroll(wage, context_for(wage, sources)))
    for outcome in outcomes:
        amounts = {item["name"]: item["amount_fen"] for item in outcome.values["obligations"]}
        assert amounts["employee_social"] == amounts["employee_housing"] == employee_fen
        assert amounts["employer_social"] == amounts["employer_housing"] == employer_fen
        assert outcome.values["net_fen"] == 1_000_000 - 2 * employee_fen
        assert outcome.values["tax_fen"] == 0
        assert actual.id in outcome.values["source_versions"]
        assert sum(line.debit for line in outcome.lines) == sum(
            line.credit for line in outcome.lines
        )
    assert outcomes[0].lines == outcomes[1].lines
    assert outcomes[0].balances == outcomes[1].balances


def test_explicit_not_declared_zero_does_not_fall_back_to_positive_policy_amount():
    wage = version(payroll(), "wage")
    actual = declared_contributions(employee_fen=0, employer_fen=0, state="not_declared")
    sources = with_payroll_plan(wage, [*contribution_sources(), actual, tax_basis(wage, 0)])
    outcome = calculate_payroll(wage, context_for(wage, sources))
    assert outcome.values["employee_contributions_fen"] == 0
    assert outcome.values["employer_contributions_fen"] == 0
    assert outcome.values["net_fen"] == 1_000_000


@pytest.mark.parametrize("tax_amount", [0, 9_999])
def test_actual_sources_without_evidence_block_instead_of_reverting_to_calculations(tax_amount):
    wage = version(payroll(), "wage")
    actual = declared_contributions(employee_fen=0, employer_fen=0)
    confirmed_tax = tax_basis(wage, tax_amount, basis_type="actual_withholding")
    for absent, expected_field in (
        ("contributions", "contribution_actual.evidence"),
        ("tax", "remuneration_tax_basis.evidence"),
    ):
        sources = with_payroll_plan(
            wage,
            [
                *contribution_sources(),
                replace(actual, evidence=()) if absent == "contributions" else actual,
                replace(confirmed_tax, evidence=()) if absent == "tax" else confirmed_tax,
            ],
        )
        with pytest.raises(NeedsInformation) as error:
            calculate_payroll(wage, context_for(wage, sources))
        assert error.value.issues[0]["field"] == expected_field


def test_missing_housing_actual_is_not_zero_when_complete_actuals_are_required():
    wage = version(payroll(contribution_basis="actual_required"), "wage")
    actual = declared_contributions(employee_fen=0, employer_fen=0)
    partial = replace(actual, fact=actual.fact.model_copy(update={"items": actual.fact.items[:1]}))
    sources = with_payroll_plan(wage, [*contribution_sources(), partial, tax_basis(wage, 0)])
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(wage, context_for(wage, sources))
    assert error.value.issues[0]["field"] == "contribution_actual.items"


@pytest.mark.parametrize("amount", [0, 12_345])
@pytest.mark.parametrize(
    "factory,calculator",
    [(payroll, calculate_payroll), (bonus, calculate_annual_bonus), (labor, calculate_labor)],
)
def test_remuneration_uses_confirmed_actual_tax_when_wage_declaration_differs(
    amount, factory, calculator
):
    remuneration = version(factory(), "remuneration")
    adopted = tax_basis(remuneration, amount, basis_type="actual_withholding")
    declaration = version(
        PayrollTaxDeclarationActual(
            period="2026-01",
            employee_id=(
                remuneration.fact.person_id
                if remuneration.fact.kind == "labor"
                else remuneration.fact.employee_id
            ),
            tax_period="2026-01",
            income_category="wages",
            declared_tax_fen=99_999,
            declaration_confirmed=True,
        ),
        "wage-declaration",
    )
    sources = [adopted, declaration]
    if remuneration.fact.kind == "payroll":
        sources = with_payroll_plan(remuneration, [*contribution_sources(), *sources])
    outcome = calculator(remuneration, context_for(remuneration, sources))
    assert outcome.values["tax_fen"] == amount
    assert outcome.values["net_fen"] == (
        outcome.values["gross_fen"] - outcome.values.get("employee_contributions_fen", 0) - amount
    )
    assert outcome.values["tax_basis"]["fact_id"] == adopted.id
    assert outcome.values["tax_basis"]["cash_payment_recorded"] is False
    assert declaration.id not in outcome.values["source_versions"]


@pytest.mark.parametrize("employee_fen,employer_fen", [(0, 0), (12_345, 54_321)])
def test_later_actual_contribution_replaces_estimate_through_normal_publication(
    tmp_path, employee_fen, employer_fen
):
    company = tax_company(tmp_path)
    original = company.current("january")
    assert original.values["employee_contributions_fen"] == 80_000
    count = company.count("voucher_version")
    actual = PayrollContributionActual(
        period="2026-01",
        employee_id="employee",
        items=(
            ContributionActualItem(
                code="pension",
                base_kind="social_insurance",
                state="declared",
                employee_amount_fen=employee_fen,
                employer_amount_fen=employer_fen,
            ),
        ),
    )
    saved = company.save(actual, "actual-contributions")
    assert "january" in company.pending()
    assert company.current("january") == original
    company.publish("january")
    adopted = company.current("january")
    assert adopted.values["employee_contributions_fen"] == employee_fen
    assert adopted.values["employer_contributions_fen"] == employer_fen
    assert adopted.values["tax_fen"] == original.values["tax_fen"]
    assert adopted.values["net_fen"] == 1_000_000 - employee_fen - 12_600
    assert saved["fact_id"] in adopted.values["source_versions"]
    assert adopted.fact_id == original.fact_id
    assert "january" not in company.pending()
    assert company.count("voucher_version") == count + 1


@pytest.mark.parametrize("category", ["annual_bonus", "labor"])
def test_wage_declaration_does_not_claim_unsupported_income_categories(category):
    with pytest.raises(ValidationError):
        PayrollTaxDeclarationActual(
            period="2026-01",
            employee_id="person",
            tax_period="2026-01",
            income_category=category,
            declared_tax_fen=0,
            declaration_confirmed=True,
        )
