"""A company label alone never supplies zero historical comparison figures."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger.opening.public import is_zero_origin

from ._support import (
    assemble,
    bank_and_equity,
    loan_opening,
    opening,
    request,
    result,
)


@pytest.mark.parametrize(
    "origin,amount,empty,expected",
    [
        ("newly_established", "0", True, True),
        ("newly_established", "0", False, True),
        ("newly_established", "100", False, False),
        ("existing_ledger", "0", True, False),
    ],
)
def test_sourced_origin_uses_the_adopted_zero_amounts_not_just_the_company_label(
    tmp_path, origin, amount, empty, expected
):
    app, company = assemble(tmp_path)
    item = opening() if empty else bank_and_equity(amount)
    item["data"]["basis"] = origin
    response = app.accounting_apply(request(company, item))
    assert response.get("completion") == "complete", response
    outcome = result(app, company, item)
    assert is_zero_origin(outcome["target"], outcome["basis"]) is expected


@pytest.mark.parametrize("change", ["target_slot", "basis_slot", "debit", "credit", "other_kind"])
def test_both_typed_opening_main_parts_and_both_zero_sides_are_required(tmp_path, change):
    app, company = assemble(tmp_path)
    item = opening(basis="newly_established")
    contract, _, _ = loan_opening()
    assert app.accounting_apply(request(company, item, contract))["completion"] == "complete"
    outcome = result(app, company, item)
    target, basis = deepcopy(outcome["target"]), deepcopy(outcome["basis"])
    if change == "target_slot":
        target["result_slot"] = "another"
    elif change == "basis_slot":
        basis["result_slot"] = "another"
    elif change == "other_kind":
        other = result(app, company, contract)
        assert not is_zero_origin(other["target"], basis)
        basis = other["basis"]
    else:
        target["values"][change + "_fen"] = "1"
    assert not is_zero_origin(target, basis)


@pytest.mark.parametrize("amount", [0, "00", "+0", "-0"])
def test_origin_rejects_invalid_types_and_noncanonical_zero_instead_of_coercing(tmp_path, amount):
    app, company = assemble(tmp_path)
    item = opening(basis="newly_established")
    assert app.accounting_apply(request(company, item))["completion"] == "complete"
    outcome = result(app, company, item)
    outcome["target"]["values"]["debit_fen"] = amount
    with pytest.raises(ContractError) as error:
        is_zero_origin(outcome["target"], outcome["basis"])
    assert error.value.code == "invalid_input"
