"""Shared pretax plus explicit accumulated tax, through real deterministic owners."""

from copy import deepcopy

import pytest

from ._support import (
    SOURCE,
    assemble,
    continuation,
    expense,
    fact,
    main,
    opening,
    request,
    withdraw,
)
from .test_application import known, profit_value


def income_inputs():
    policy = fact(
        "taxes.income_tax_policy",
        {
            "policy": {
                "version": "synthetic-carry-1",
                "official_url": "https://shanghai.chinatax.gov.cn/zzzb/zcwj/202602/t479283.html",
                "effective_from": "2023-01-01",
                "effective_to": "2027-12-31",
            },
            "standard_rate": "0.25",
            "small_profit_rate": "0.20",
            "small_profit_taxable_fraction": "0.25",
            "small_profit_limit_fen": "300000000",
        },
    )
    basis = fact(
        "taxes.prepayment_basis",
        {
            "tax_year": 2026,
            "quarter_end": "2026-09",
            "collection_method": "actual_profit",
            "treatment": "small_low_profit",
            "basis_source": {"source_id": SOURCE},
            "eligibility_source": {"source_id": SOURCE},
            "loss_deduction_fen": "0",
            "special_treatments": [],
        },
        period="2026-09",
    )
    assessed = fact(
        "taxes.assessment",
        {
            "type": "income_tax",
            "method": "computed_provision",
            "tax_year": 2026,
            "quarter_end": "2026-09",
            "policy_id": policy["business_id"],
            "prepayment_basis_id": basis["business_id"],
        },
        period="2026-09",
    )
    return assessed, basis, policy


@pytest.mark.parametrize("opening_basis", ["existing_ledger", "newly_established"])
def test_computed_income_tax_uses_formal_pretax_and_independent_carry_then_recalculates(
    tmp_path, opening_basis
):
    app, company = assemble(tmp_path)
    initial = opening(basis=opening_basis)
    current = continuation(initial, carry="7000")
    profit_value(current, "2000000")
    business = expense()
    assessed, basis, policy = income_inputs()
    response = app.accounting_apply(
        request(company, assessed, business, current, basis, initial, policy)
    )
    assert response.get("completion") == "complete", response
    first = main(app, company, assessed)
    assert known(app, company).fields["profit_before_tax"]["amount_fen"] == "1980000"
    values = first["target"]["values"]
    assert values["cumulative_tax_fen"] == "99000"
    assert values["previous_cumulative_fen"] == "7000"
    assert values["change_fen"] == "92000"
    assert first["vouchers"][0]["lines"][0]["debit_fen"] == "92000"
    assert {r["business_id"] for r in first["basis"]["source_results"]} >= {
        current["business_id"],
        business["business_id"],
    }
    # A new continuation target drives the existing EIT consumer; no EIT mutation is submitted.
    profit_value(current, "1000000")
    current["data"]["income_tax_carry"]["cumulative_tax_fen"] = "8000"
    response = app.accounting_apply(request(company, current, op="replace", seq=1, seed=False))
    assert response.get("completion") == "complete", response
    second = main(app, company, assessed)
    assert second["target"]["values"]["cumulative_tax_fen"] == "49000"
    assert second["target"]["values"]["previous_cumulative_fen"] == "8000"
    assert second["target"]["values"]["change_fen"] == "41000"
    assert second["vouchers"][0]["voucher_id"] == first["vouchers"][0]["voucher_id"]
    # Removing the only known prior facts would make already-used EIT unknown: atomic rejection.
    rejected = app.accounting_apply(withdraw(company, current, seq=2))
    assert rejected.get("completion") == "not_saved", rejected
    assert rejected["error"] == "dependency_conflict"
    assert all(issue["field"] != "/data/profit_before_tax" for issue in rejected["fact_issues"])
    assert main(app, company, assessed) == second
    assert known(app, company).fields["profit_before_tax"]["amount_fen"] == "980000"


def test_explicit_zero_carry_differs_from_absent_carry_and_never_uses_pl31(tmp_path):
    app, company = assemble(tmp_path)
    initial = opening()
    current = continuation(initial)
    assert app.accounting_apply(request(company, initial, current))["completion"] == "complete"
    assessed, basis, policy = income_inputs()
    response = app.accounting_apply(request(company, assessed, basis, policy, seed=False))
    assert response.get("completion") == "not_saved", response
    assert response["error"] == "needs_information"
    assert "/data/income_tax_carry" in {issue["field"] for issue in response["fact_issues"]}
    current["data"]["income_tax_carry"] = {
        "tax_year": 2026,
        "period_to": "2026-06",
        "cumulative_tax_fen": "0",
        "source": {"source_id": SOURCE},
    }
    submitted = request(company, assessed, basis, policy, seed=False)
    submitted["mutations"].append({"op": "replace", "expected_seq": 1, "fact": deepcopy(current)})
    accepted = app.accounting_apply(submitted)
    assert accepted.get("completion") == "complete", accepted
    values = main(app, company, assessed)["target"]["values"]
    assert values["previous_cumulative_fen"] == "0"
    assert values["cumulative_tax_fen"] == "5000"
    assert values["change_fen"] == "5000"


@pytest.mark.parametrize("basis", ["newly_established", "existing_ledger"])
@pytest.mark.parametrize("with_continuation", [False, True])
def test_sourced_zero_origin_with_or_without_reports_does_not_guess_existing_company_carry(
    tmp_path, basis, with_continuation
):
    app, company = assemble(tmp_path)
    initial = opening(value="0", basis=basis)
    assessed, inputs, policy = income_inputs()
    rows = [initial, assessed, inputs, policy]
    if with_continuation:
        current = continuation(initial)
        for key in ("year_beginning_balance", "prior_profit", "prior_cash"):
            for row in current["data"][key]:
                for column in row.keys() - {"line"}:
                    row[column] = "0"
        rows.append(current)
    response = app.accounting_apply(request(company, *rows))
    if basis == "newly_established":
        assert response.get("completion") == "complete", response
        values = main(app, company, assessed)["target"]["values"]
        assert values["cumulative_tax_fen"] == "0"
        assert values["previous_cumulative_fen"] == "0"
        assert main(app, company, assessed)["vouchers"] == []
    else:
        assert response.get("completion") == "not_saved", response
        if with_continuation:
            assert response["error"] == "needs_information"
            assert response["fact_issues"][0]["field"] == "/data/income_tax_carry"
        else:
            assert response["error"] == "dependency_conflict"
            assert all(
                issue["field"] != "/data/profit_before_tax" for issue in response["fact_issues"]
            )
