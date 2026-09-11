"""Actual withholding governs payable amounts while the policy estimate survives."""

from dataclasses import replace

import pytest
from pydantic import ValidationError
from test_exports import setup as export_fixture
from test_payroll import (
    as_calculation,
    context_for,
    contribution_policy,
    income_tax_policy,
    opening,
    payroll,
    profile,
    version,
)
from test_payroll_tax_declarations import adopt, declare, pay, preview

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.domains.payroll import (
    PayrollBounded,
    PayrollWithholdingActual,
    calculate_payroll,
)

setup = export_fixture


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
    wage = version(
        payroll(
            period="2026-08", accounting_gross_salary_fen=4000000, tax_reported_salary_fen=4000000
        ),
        "august",
    )
    sources = [
        version(
            profile(
                withholding_start_date="2026-03",
                social_insurance_participating=False,
                social_insurance_base_fen=None,
            ),
            "profile",
        ),
        version(contribution_policy(), "contributions"),
        version(income_tax_policy(), "income-tax"),
        version(
            opening(
                period="2026-08",
                through_period="2026-07",
                cumulative_standard_deduction_fen=2500000,
            ),
            "opening",
        ),
    ]
    return wage, sources


def test_actual_900_replaces_300_without_changing_relationship_or_policy():
    wage, sources = august_case()
    computed = calculate_payroll(wage, context_for(wage, sources))
    assert computed.values["tax_fen"] == 30000
    observed = version(actual(), "actual")
    adopted = calculate_payroll(wage, context_for(wage, [*sources, observed]))
    assert adopted.values["tax_fen"] == 90000
    assert adopted.values["net_fen"] == 3910000
    assert adopted.values["calculated_tax_fen"] == 30000
    assert adopted.values["calculated_tax_state"] == computed.values["tax_state"]
    assert adopted.values["tax_state"]["cumulative_standard_deduction_fen"] == 3000000
    assert adopted.values["tax_state_basis"] == "policy_deductions_with_actual_withholding"
    assert (
        adopted.values["actual_withholding"]["reported_cumulative_standard_deduction_fen"]
        == 1000000
    )
    assert adopted.values["tax_state"]["cumulative_withheld_tax_fen"] == 90000
    assert adopted.values["tax_input"]["withholding_start_date"] == "2026-03"
    assert adopted.values["rule_versions"] == computed.values["rule_versions"]
    assert sum(line.debit for line in adopted.lines) == sum(line.credit for line in adopted.lines)
    assert not any(line.account.startswith("100") for line in adopted.lines)
    assert adopted.values["actual_withholding"]["difference_from_calculation_fen"] == 60000
    september = version(
        payroll(period="2026-09", accounting_gross_salary_fen=0, tax_reported_salary_fen=0),
        "september",
    )
    next_result = calculate_payroll(
        september, context_for(september, sources, [as_calculation(wage, adopted)])
    )
    assert next_result.values["prior_tax_state"]["cumulative_withheld_tax_fen"] == 90000
    assert next_result.values["tax_fen"] == 0
    assert next_result.values["tax_state"]["cumulative_withheld_tax_fen"] == 90000


def test_actual_requires_evidence_and_unique_employee_month():
    wage, sources = august_case()
    with pytest.raises(NeedsInformation, match="需要依据"):
        calculate_payroll(wage, context_for(wage, [*sources, version(actual(), evidence=())]))
    with pytest.raises(KernelError, match="多个"):
        calculate_payroll(
            wage, context_for(wage, [*sources, version(actual(), "one"), version(actual(), "two")])
        )
    unrelated = version(actual(employee_id="other"), "other")
    assert (
        calculate_payroll(wage, context_for(wage, [*sources, unrelated])).values["tax_fen"] == 30000
    )


@pytest.mark.parametrize("amount", [True, 900.0, -1, 2**63])
def test_actual_amount_rejects_non_integer_or_overflow(amount):
    with pytest.raises(ValidationError):
        actual(amount=amount)


def test_actual_above_available_salary_never_invents_a_negative_payable():
    wage, sources = august_case()
    with pytest.raises(KernelError):
        calculate_payroll(wage, context_for(wage, [*sources, version(actual(amount=4000001))]))


def test_actual_tax_keeps_unknown_deductions_unknown():
    wage, sources = august_case()
    data = wage.fact.model_dump() | {"special_additional_deduction_fen": None}
    wage = replace(wage, fact=PayrollBounded(**data))
    with pytest.raises(NeedsInformation):
        calculate_payroll(wage, context_for(wage, sources))
    result = calculate_payroll(wage, context_for(wage, [*sources, version(actual())]))
    assert result.values["tax_fen"] == 90000
    assert result.values["tax_state"] is None
    assert result.values["tax_input"]["special_additional_deduction_fen"] is None
    assert result.values["tax_state_bounds"]["lower_bound"]["cumulative_withheld_tax_fen"] == 90000
    assert "calculated_tax_fen" not in result.values
    assert result.values["calculated_tax_upper_bound_fen"] == 30000


def test_actual_backfill_preserves_bank_payment_and_clears_wrong_hold(setup):
    company, export, template = setup
    old = company.current("january")
    _, declaration = declare(company)
    assert not company.pending(), "a declaration alone must not invalidate the wage"
    adopt(company, declaration)
    target = old.values["net_fen"] - 60000
    pay(company, target)
    payment = company.current("payment", "payment")
    original_number = company.engine.ledger("2026-01")[0]["number"]
    company.save(
        actual(
            period="2026-01",
            amount=old.values["tax_fen"] + 60000,
            reported_cumulative_standard_deduction_fen=None,
        ),
        "actual-withholding",
    )
    assert {"january", "basis", "payment"} <= company.pending()
    company.publish("january", "basis", "payment")
    wage = company.current("january")
    assert wage.values["net_fen"] == target
    assert company.engine.ledger("2026-01")[0]["number"] == original_number
    paid = company.current("payment", "payment")
    assert paid.fact_id == payment.fact_id
    assert {k: v for k, v in paid.values.items() if k != "settlements"} == {
        k: v for k, v in payment.values.items() if k != "settlements"
    }
    assert paid.values["settlements"] == tuple(
        dict(item) | {"source_calculation": wage.id} for item in payment.values["settlements"]
    )
    basis = company.current("basis", "payroll_disbursement_basis")
    assert basis.values["held_fen"] == 0 and basis.values["withholding_recorded"] is True
    with company.engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT coalesce(sum(amount),0) FROM balance "
                "WHERE balance_key='payroll:january:net'"
            ).fetchone()[0]
            == 0
        )
    assert not company.pending()
    with pytest.raises(KernelError, match="没有可代发"):
        preview(export, template)


def test_actual_without_bank_payment_changes_net_export_once(setup):
    company, export, template = setup
    old = company.current("january")
    _, declaration = declare(company)
    adopt(company, declaration)
    company.save(
        actual(
            period="2026-01",
            amount=old.values["tax_fen"] + 60000,
            reported_cumulative_standard_deduction_fen=None,
        ),
        "withholding",
    )
    company.publish("january", "basis")
    result = preview(export, template)
    assert result["total_fen"] == old.values["net_fen"] - 60000
    assert result["payroll_disbursements"][0]["held_fen"] == 0
    assert result["payroll_disbursements"][0]["withholding_recorded"] is True
