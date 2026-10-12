"""Historical actual withholding is adopted only through explicit source confirmation."""

import pytest
from test_payroll import (
    context_for,
    contribution_policy,
    payroll,
    profile,
    tax_basis,
    version,
    with_payroll_plan,
)

from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.domains.payroll import calculate_payroll
from ai_accounting.kernel.payroll_history import PayrollWithholdingActual
from ai_accounting.kernel.remuneration_tax_basis import HistoricalWithholdingOrigin


def actual(*, period="2026-08", amount=90000, **changes):
    return PayrollWithholdingActual(
        **{
            "period": period,
            "employee_id": "employee",
            "withheld_tax_fen": amount,
            "withholding_confirmed": True,
            "reported_cumulative_standard_deduction_fen": 1000000,
            **changes,
        }
    )


def august_case():
    wage = version(payroll(period="2026-08", accounting_gross_salary_fen=4000000), "august")
    return wage, [
        version(
            profile(social_insurance_participating=False, social_insurance_base_fen=None), "profile"
        ),
        version(contribution_policy(), "contributions"),
    ]


def test_old_actual_is_not_automatically_adopted_or_recomputed():
    wage, sources = august_case()
    observed = version(actual(), "actual")
    sources = with_payroll_plan(wage, [*sources, observed])
    with pytest.raises(NeedsInformation):
        calculate_payroll(wage, context_for(wage, sources))
    basis = tax_basis(
        wage,
        90000,
        basis_type="actual_withholding",
        origin_references=(
            HistoricalWithholdingOrigin(
                kind=observed.fact.kind, subject_id=observed.subject_id, fact_id=observed.id
            ),
        ),
    )
    result = calculate_payroll(wage, context_for(wage, [*sources, basis]))
    assert result.values["tax_fen"] == 90000
    assert result.values["net_fen"] == 3910000
    assert result.values["tax_basis"]["basis_type"] == "actual_withholding"
    assert "difference_from_calculation_fen" not in result.values


def test_final_statement_is_distinct_from_actual_withholding_and_cash_payment():
    wage, sources = august_case()
    basis = tax_basis(wage, 90000)
    result = calculate_payroll(wage, context_for(wage, [*with_payroll_plan(wage, sources), basis]))
    assert result.values["tax_basis"]["basis_type"] == "final_statement"
    assert result.values["tax_basis"]["withholding_date"] is None
    assert result.values["tax_basis"]["cash_payment_recorded"] is False
