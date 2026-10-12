"""Both retained wage identities use the same source-confirmed accounting contract."""

import pytest
from test_payroll import (
    context_for,
    payroll,
    payroll_sources,
    tax_basis,
    version,
    with_payroll_plan,
)

from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.domains.payroll import Payroll, PayrollBounded, calculate_payroll


def bounded(**changes):
    return PayrollBounded(**payroll(**changes).model_dump())


def test_bounded_and_regular_have_identical_current_fields_and_require_explicit_tax():
    assert Payroll.model_fields.keys() == PayrollBounded.model_fields.keys()
    current = version(bounded(), "wage")
    sources = with_payroll_plan(current, payroll_sources())
    with pytest.raises(NeedsInformation):
        calculate_payroll(current, context_for(current, sources))
    result = calculate_payroll(
        current, context_for(current, [*sources, tax_basis(current, 90_000)])
    )
    assert result.values["tax_fen"] == 90_000
    assert result.values["net_fen"] == 830_000
    assert "tax_state_bounds" not in result.values


def test_unknown_deductions_are_not_current_accounting_inputs():
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        bounded(special_additional_deduction_fen=None)
