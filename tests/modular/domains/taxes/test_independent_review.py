"""Independent accounting counterexamples through the public application."""

import pytest

from ._fixtures import confirmed_income, income_actual
from .test_application import assembled as assembled
from .test_application import main, netting, remaining, request, tax_cash


@pytest.mark.parametrize("paid", ["0", "9000", "10000"])
@pytest.mark.parametrize("received", ["0", "1000"])
def test_actual_cash_refund_requires_net_credit_after_prior_unpaid_tax(assembled, paid, received):
    app, company_id, _, _ = assembled
    first = confirmed_income("review-first", "2026-03", "10000")
    rows = [first]
    if paid != "0":
        rows.append(tax_cash("review-paid", "2026-03-31", "outflow", [(first, "tax", paid)]))
    result = app.accounting_apply(request(company_id, *rows))
    assert result["completion"] == "complete", result
    before = main(app, company_id, first)
    lower = confirmed_income("review-lower", "2026-06", "8000")
    declared = income_actual(lower, "8000", "10000", "-2000")
    rows = [lower, declared]
    if received != "0":
        rows.append(
            tax_cash(
                "review-received",
                "2026-06-30",
                "inflow",
                [(lower, "tax_refund", received)],
            )
        )
    result = app.accounting_apply(request(company_id, *rows))
    if paid == "10000":
        assert result["completion"] == "complete", result
        assert remaining(app, company_id, lower, "tax_refund") == (
            "2000" if received == "0" else "1000"
        )
        assert remaining(app, company_id, first) == "0"
        assert netting(app, company_id, lower)["target"]["values"]["matched_fen"] == "0"
    else:
        assert result["completion"] == "not_saved", result
        assert result["error"] == "needs_information", result
        assert main(app, company_id, first) == before


def test_actual_refund_can_use_partial_real_prepaid_without_requiring_all_prior_tax_paid(
    assembled,
):
    app, company_id, _, _ = assembled
    first = confirmed_income("review-partial-first", "2026-03", "10000")
    paid = tax_cash("review-partial-paid", "2026-03-31", "outflow", [(first, "tax", "9000")])
    lower = confirmed_income("review-partial-lower", "2026-06", "8000")
    declared = income_actual(lower, "8000", "9000", "-1000")
    result = app.accounting_apply(request(company_id, first, paid, lower, declared))
    assert result["completion"] == "complete", result
    assert remaining(app, company_id, first) == "0"
    assert remaining(app, company_id, lower) == "0"
    assert remaining(app, company_id, lower, "tax_refund") == "1000"
