from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.money.public import create_money

from .conftest import Context, objects, payable, payment


def original(ids, *, kind="bank", value="100000"):
    data = {
        "rows": [
            {
                "row_id": str(uuid4()),
                "source": {"source_id": ids["source"], "location": "原件第2行"},
                "actual_on": "2026-01-20",
                "direction": "outflow",
                "amount_fen": value,
            }
        ]
    }
    if kind == "bank":
        data.update(funds_account_id=ids["bank"], opening_fen=value, closing_fen="0")
    else:
        data["platform_account_id"] = ids["platform"]
    return {
        "business_id": str(uuid4()),
        "kind": "money.bank_statement" if kind == "bank" else "money.platform_activity",
        "period": "2026-01",
        "evidence_ids": [],
        "data": data,
    }


def evaluate(ids, fact, *extra):
    context = Context([fact, *extra], objects=objects(ids), obligations=[payable(ids)])
    return create_money().evaluate_slot(fact["business_id"], "main", context)


def test_original_bank_rows_do_not_book_another_payment(ids):
    fact = original(ids)
    plan = evaluate(ids, fact)
    assert plan["lines"] == plan["fund_movements"] == plan["settlements"] == []
    assert plan["target"]["values"] == {
        "funds_account_id": ids["bank"],
        "opening_fen": "100000",
        "closing_fen": "0",
        "row_count": 1,
    }
    assert plan["basis"]["source_locations"] == [fact["data"]["rows"][0]["source"]]


def test_original_reference_is_unique_only_inside_the_same_statement(ids):
    first = original(ids)
    first["data"]["rows"][0]["reference"] = "原银行参考号"
    second = deepcopy(first)
    second["business_id"] = str(uuid4())
    evaluate(ids, first)
    evaluate(ids, second)
    duplicate = deepcopy(first["data"]["rows"][0])
    duplicate["row_id"] = str(uuid4())
    first["data"]["rows"].append(duplicate)
    first["data"]["opening_fen"] = "200000"
    with pytest.raises(ContractError):
        evaluate(ids, first)


@pytest.mark.parametrize("change", ["wrong_month", "wrong_closing", "duplicate_id"])
def test_original_row_conflicts_do_not_get_an_invented_adjustment(ids, change):
    fact = original(ids)
    if change == "wrong_month":
        fact["data"]["rows"][0]["actual_on"] = "2026-02-01"
    elif change == "wrong_closing":
        fact["data"]["closing_fen"] = "1"
    else:
        fact["data"]["rows"].append(deepcopy(fact["data"]["rows"][0]))
        fact["data"]["opening_fen"] = "200000"
    with pytest.raises(ContractError):
        evaluate(ids, fact)


def test_platform_actual_money_consumes_original_rows_once(ids):
    source = original(ids, kind="platform")
    fact = payment(ids, account="platform")
    fact["data"]["platform_row_ids"] = [source["data"]["rows"][0]["row_id"]]
    plan = evaluate(ids, fact, source)
    assert len(plan["fund_movements"]) == 1
    assert plan["fund_movements"][0]["amount_fen"] == "100000"
    assert plan["lines"][-1]["account_code"] == "1012"
    assert source["data"]["rows"][0]["source"] in plan["basis"]["source_locations"]
    copied = deepcopy(fact)
    copied["business_id"] = str(uuid4())
    with pytest.raises(ContractError):
        evaluate(ids, copied, source, fact)


@pytest.mark.parametrize("change", ["missing", "wrong_date", "wrong_direction", "wrong_amount"])
def test_platform_payment_requires_matching_actual_evidence(ids, change):
    source = original(ids, kind="platform")
    fact = payment(ids, account="platform")
    fact["data"]["platform_row_ids"] = [source["data"]["rows"][0]["row_id"]]
    if change == "missing":
        fact["data"]["platform_row_ids"] = []
    else:
        field, value = {
            "wrong_date": ("actual_on", "2026-01-19"),
            "wrong_direction": ("direction", "inflow"),
            "wrong_amount": ("amount_fen", "99999"),
        }[change]
        source["data"]["rows"][0][field] = value
    with pytest.raises(ContractError):
        evaluate(ids, fact, source)


def test_internal_reserve_platform_rows_have_no_company_cash_effect(ids):
    source = original(ids, kind="platform")
    fact = {
        "business_id": ids["business"],
        "kind": "money.reserve_internal",
        "period": "2026-01",
        "evidence_ids": [],
        "data": {
            "platform_account_id": ids["platform"],
            "platform_row_ids": [source["data"]["rows"][0]["row_id"]],
            "boundary_source": {"source_id": ids["source"], "location": "明确是代管资金内部移动"},
        },
    }
    plan = evaluate(ids, fact, source)
    assert plan["fund_movements"] == plan["lines"] == []
    assert plan["target"]["values"]["company_money_effect_fen"] == "0"
    moved = payment(ids, account="platform")
    moved["business_id"] = str(uuid4())
    moved["data"]["platform_row_ids"] = fact["data"]["platform_row_ids"]
    with pytest.raises(ContractError):
        evaluate(ids, moved, source, fact)
