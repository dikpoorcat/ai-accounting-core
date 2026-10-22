"""Money's finite input variants retain their exact installed field contracts."""

import json

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.ledger import _read_origins


def fragment(data, *, quantities=(), checked=None):
    kind = "money.movement"
    plan = _read_origins._SELECTIONS[kind]
    tokens = [json.dumps(kind), json.dumps(kind), json.dumps(list(quantities))]
    tokens.extend(json.dumps(data[field]) if field in data else None for field in plan.fields)
    return _read_origins._fragment(
        {"projection_json": json.dumps(tokens)}, plan, {} if checked is None else checked
    )


@pytest.mark.parametrize(
    "data",
    [
        {"type": "settlement"},
        {"type": "managed_reserve"},
        {"type": "payroll_reserve", "reserve_expense_fen": "1"},
        {"type": "payroll_reserve", "reserve_expense_fen": "9223372036854775807"},
    ],
)
def test_money_variants_use_named_object_contracts_and_detached_quantities(data):
    quantity = {
        "type": "obligation",
        "source": {"business_id": "10000000-0000-4000-8000-000000000001", "slot": "primary"},
        "account_code": "2202",
        "direction": "payable",
        "amount_fen": "0",
    }
    checked = {}
    target, basis = fragment(data, quantities=(quantity,), checked=checked)
    assert target == {"kind": "money.movement"}
    assert basis == {"kind": "money.movement", "input": data, "adopted_quantities": [quantity]}
    basis["input"].clear()
    basis["adopted_quantities"][0]["source"]["slot"] = "changed"
    assert fragment(data, quantities=(quantity,), checked=checked)[1] == {
        "kind": "money.movement",
        "input": data,
        "adopted_quantities": [quantity],
    }


@pytest.mark.parametrize(
    "data",
    [
        {},
        {"type": None},
        {"type": []},
        {"type": "future_variant"},
        {"type": "settlement", "reserve_expense_fen": "1"},
        {"type": "managed_reserve", "reserve_expense_fen": "1"},
        {"type": "payroll_reserve"},
        *(
            {"type": "payroll_reserve", "reserve_expense_fen": value}
            for value in (None, 1, True, "0", "-1", "01", "9223372036854775808")
        ),
    ],
)
def test_money_fragment_rejects_unknown_variants_and_selected_illegal_fields(data):
    with pytest.raises(ContractError) as caught:
        fragment(data)
    assert caught.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "quantity", [{}, {"type": "unknown"}, {"type": "obligation", "amount_fen": "1"}]
)
def test_money_fragment_still_validates_the_complete_adopted_quantity_union(quantity):
    with pytest.raises(ContractError) as caught:
        fragment({"type": "settlement"}, quantities=(quantity,))
    assert caught.value.code == "storage_corrupt"
