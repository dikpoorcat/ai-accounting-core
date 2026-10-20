from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.money.public import create_money, stable_occurrence_id

from .conftest import Context, objects, payable, payment


def evaluate(ids, fact=None, obligation=None, extra=()):
    fact = fact or payment(ids)
    context = Context(
        [fact, *extra], objects=objects(ids), obligations=[obligation or payable(ids)]
    )
    return create_money().evaluate_slot(fact["business_id"], "main", context)


def test_real_expense_payment_clears_debt_and_never_books_expense_twice(ids):
    plan = evaluate(ids)
    assert [
        (row["account_code"], row["debit_fen"], row["credit_fen"]) for row in plan["lines"]
    ] == [("2202", "100000", "0"), ("1002", "0", "100000")]
    assert plan["obligations"] == []
    assert plan["settlements"][0]["amount_fen"] == "100000"
    actual = plan["fund_movements"][0]
    assert actual["movement_id"] == stable_occurrence_id(ids["business"], "funds")
    assert actual["amount_fen"] == "100000"
    assert actual["cashflow_parts"] == [
        {"category": "other_operating_payments", "amount_fen": "100000"}
    ]
    assert plan["basis"]["source_results"][0]["business_id"] == ids["expense"]


def test_missing_cashflow_classification_does_not_block_real_payment(ids):
    plan = evaluate(ids, obligation=payable(ids, parts=False))
    assert "cashflow_parts" not in plan["fund_movements"][0]
    assert plan["settlements"][0]["amount_fen"] == "100000"


def test_expense_refund_is_real_inflow_against_a_receivable(ids):
    fact, ob = payment(ids), payable(ids)
    fact["data"]["direction"] = "inflow"
    ob["direction"], ob["account_code"] = "receivable", "1221"
    plan = evaluate(ids, fact, ob)
    assert [
        (row["account_code"], row["debit_fen"], row["credit_fen"]) for row in plan["lines"]
    ] == [("1221", "0", "100000"), ("1002", "100000", "0")]
    assert plan["fund_movements"][0]["direction"] == "inflow"


def test_cumulative_mixed_classification_includes_current_other_payments_across_carriers(ids):
    ob = payable(ids, "3")
    ob["cashflow_parts"] = [
        {"category": "employee_payments", "amount_fen": "1"},
        {"category": "asset_acquisition", "amount_fen": "1"},
        {"category": "other_operating_payments", "amount_fen": "1"},
    ]
    first = payment(ids, "1", day="2026-01-10")
    first["business_id"] = str(uuid4())
    second = payment(ids, "1", account="cash", day="2026-01-11")
    second_plan = evaluate(ids, second, ob, [first])
    assert second_plan["fund_movements"][0]["cashflow_parts"] == [
        {"category": "employee_payments", "amount_fen": "1"}
    ]
    third = deepcopy(second)
    third["business_id"], third["effective_on"] = str(uuid4()), "2026-01-12"
    third_plan = evaluate(ids, third, ob, [first, second])
    assert third_plan["fund_movements"][0]["cashflow_parts"] == [
        {"category": "other_operating_payments", "amount_fen": "1"}
    ]
    assert len(third_plan["basis"]["source_results"]) == 1


@pytest.mark.parametrize(
    "field,value", [("amount_fen", "99999"), ("direction", "inflow"), ("counterparty_id", None)]
)
def test_conflicting_payment_facts_are_not_silently_repaired(ids, field, value):
    fact = payment(ids)
    fact["data"][field] = str(uuid4()) if value is None else value
    with pytest.raises(ContractError):
        evaluate(ids, fact)


def test_company_account_cannot_be_replaced_by_a_person_or_project(ids):
    fact = payment(ids)
    fact["data"]["funds_account_id"] = ids["party"]
    with pytest.raises(ContractError) as error:
        evaluate(ids, fact)
    assert error.value.code == "invalid_input"
    assert error.value.fact_issues[0]["field"] == "/data/funds_account_id"


def test_date_must_be_actual_and_in_month(ids):
    fact = payment(ids)
    fact["period"] = "2026-02"
    with pytest.raises(ContractError):
        evaluate(ids, fact)
    del fact["effective_on"]
    with pytest.raises(ContractError):
        evaluate(ids, fact)


def test_physical_transfer_has_two_stable_sides_and_no_external_cashflow(ids):
    fact = {
        "business_id": ids["business"],
        "kind": "money.transfer",
        "period": "2026-01",
        "effective_on": "2026-01-12",
        "evidence_ids": [ids["source"]],
        "data": {
            "type": "physical",
            "from_account_id": ids["bank"],
            "to_account_id": ids["cash"],
            "amount_fen": "2345",
        },
    }
    plan = evaluate(ids, fact)
    assert [(x["account_code"], x["debit_fen"], x["credit_fen"]) for x in plan["lines"]] == [
        ("1001", "2345", "0"),
        ("1002", "0", "2345"),
    ]
    actual = plan["fund_movements"]
    assert {row["public_slot"] for row in actual} == {"from_funds", "to_funds"}
    assert len({row["transfer_group_id"] for row in actual}) == 1
    assert all(
        row["cashflow_parts"] == [{"category": "none", "amount_fen": "2345"}] for row in actual
    )
    assert plan["settlements"] == []


def test_managed_reserve_return_reverses_expense_without_inventing_another_balance(ids):
    fact = payment(ids)
    fact["data"] = {
        "type": "managed_reserve",
        "funds_account_id": ids["bank"],
        "direction": "inflow",
        "amount_fen": "40000",
    }
    plan = evaluate(ids, fact)
    assert [(x["account_code"], x["debit_fen"], x["credit_fen"]) for x in plan["lines"]] == [
        ("5602", "0", "40000"),
        ("1002", "40000", "0"),
    ]
    assert plan["obligations"] == []
    assert len(plan["fund_movements"]) == 1
