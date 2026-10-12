"""Retired first-wage deductions remain history, never a current accounting dependency."""

from test_payroll import (
    context_for,
    payroll,
    payroll_sources,
    tax_basis,
    version,
    with_payroll_plan,
)

from ai_accounting.kernel.domains.payroll import calculate_payroll
from ai_accounting.kernel.payroll_history import PayrollFirstWageTreatment


def test_old_first_wage_treatment_does_not_influence_confirmed_tax():
    current = version(payroll(), "wage")
    old = version(
        PayrollFirstWageTreatment(
            period="2026-01", employee_id="employee", standard_deduction_start_month=1
        ),
        "old-first-wage",
    )
    sources = with_payroll_plan(current, [*payroll_sources(), old, tax_basis(current, 90_000)])
    context = context_for(current, sources)
    result = calculate_payroll(current, context)
    assert result.values["tax_fen"] == 90_000
    assert old.id not in result.values["source_versions"]
    assert "tax_state" not in result.values
