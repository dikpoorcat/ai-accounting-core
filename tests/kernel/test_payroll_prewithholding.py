"""Missing tax evidence cannot be replaced by a hypothetical withholding start."""

import pytest
from pydantic import ValidationError
from test_payroll import (
    context_for,
    payroll,
    payroll_sources,
    profile,
    tax_basis,
    version,
    with_payroll_plan,
)

from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.domains.payroll import calculate_payroll


def test_profile_no_longer_accepts_a_tax_clock():
    with pytest.raises(ValidationError):
        profile(withholding_start_date="2026-06")


def test_small_gross_amount_requires_explicit_zero_source():
    current = version(payroll(accounting_gross_salary_fen=1), "wage")
    sources = payroll_sources()
    sources[0] = version(profile(social_insurance_participating=False), "profile")
    sources = with_payroll_plan(current, sources)
    with pytest.raises(NeedsInformation) as error:
        calculate_payroll(current, context_for(current, sources))
    assert error.value.issues[0]["field"] == "remuneration_tax_basis"
    result = calculate_payroll(current, context_for(current, [*sources, tax_basis(current, 0)]))
    assert result.values["net_fen"] == 1
    assert result.values["tax_fen"] == 0
    assert "withholding_start" not in result.values
