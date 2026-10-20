from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, EvaluatedSlot, ResourceDefinition
from ai_accounting_modular.money.public import create_money, stable_occurrence_id

from .conftest import Context, objects


class ReconciliationContext(Context):
    def __init__(self, ids, facts, funds=()):
        cards = objects(ids)
        cards[0]["data"] = {"opened_period": "2026-01", "zero_at_opening": True}
        super().__init__(facts, objects=cards)
        self.funds, self.results = tuple(funds), {}

    def get_result(self, business_id, result_slot):
        if business_id not in self.results:
            plan = create_money().evaluate_slot(business_id, result_slot, self)
            self.results[business_id] = EvaluatedSlot(
                {
                    "business_id": business_id,
                    "result_id": business_id,
                    "result_slot": result_slot,
                    "result_generation": 1,
                    "content_digest": "0" * 64,
                },
                plan["target"],
                plan["basis"],
            )
        return self.results[business_id]

    def fund_movements(self, account_id, period_from, period_to):
        return tuple(
            item
            for item in self.funds
            if item.value["funds_account_id"] == account_id
            and period_from <= item.value["actual_on"][:7] <= period_to
        )


def funds(ids, value, *, day="2026-01-20", direction="inflow"):
    business = str(uuid4())
    return ResourceDefinition(
        "funds",
        {"business_id": business, "slot": "funds"},
        {
            "business_id": business,
            "result_id": business,
            "result_slot": "main",
            "result_generation": 1,
            "content_digest": "0" * 64,
        },
        {
            "movement_id": stable_occurrence_id(business, "funds"),
            "public_slot": "funds",
            "funds_account_id": ids["bank"],
            "actual_on": day,
            "direction": direction,
            "amount_fen": str(value),
        },
    )


def pair(ids, *, values=(300,), opening="0", day="2026-01-20"):
    statement = {
        "business_id": str(uuid4()),
        "kind": "money.bank_statement",
        "period": day[:7],
        "evidence_ids": [],
        "data": {
            "funds_account_id": ids["bank"],
            "opening_fen": opening,
            "closing_fen": str(int(opening) + sum(values)),
            "rows": [
                {
                    "row_id": str(uuid4()),
                    "source": {"source_id": ids["source"]},
                    "actual_on": day,
                    "direction": "inflow",
                    "amount_fen": str(value),
                }
                for value in values
            ],
        },
    }
    check = {
        "business_id": str(uuid4()),
        "kind": "money.reconciliation",
        "period": day[:7],
        "evidence_ids": [],
        "data": {"statement_id": statement["business_id"], "matches": []},
    }
    return statement, check


def validate(context, check):
    money = create_money()
    plan = money.evaluate_slot(check["business_id"], "main", context)
    change = {
        "domain": "money",
        "businesses": [
            {
                "business_id": check["business_id"],
                "expected_seq": 0,
                "op": "create",
                "natural_key": None,
                "fact": check,
                "complete_result_slots": ["main"],
            }
        ],
        "dependencies": [],
        "publications": [],
        "issues": [],
        "retained_results": [],
        "plans": [plan],
    }
    before = deepcopy(change)
    money.validate_candidate(change, context)
    assert change == before
    return plan


@pytest.mark.parametrize("row_values,fund_values", [((300,), (100, 200)), ((100, 200), (300,))])
def test_complete_group_can_match_one_to_many_or_many_to_one_without_splitting_money(
    ids, row_values, fund_values
):
    statement, check = pair(ids, values=row_values)
    actual = [funds(ids, value) for value in fund_values]
    check["data"]["matches"] = [
        {
            "statement_row_ids": [row["row_id"] for row in statement["data"]["rows"]],
            "fund_movement_ids": [item.value["movement_id"] for item in actual],
        }
    ]
    plan = validate(ReconciliationContext(ids, [statement, check], actual), check)
    assert plan["lines"] == plan["fund_movements"] == plan["settlements"] == []
    assert plan["target"]["values"]["difference_fen"] == "0"
    assert plan["target"]["values"]["closing_fen"] == "300"


def test_extra_unmatched_actual_money_cannot_be_hidden_by_matching_only_requested_ids(ids):
    statement, check = pair(ids)
    actual = funds(ids, 300)
    check["data"]["matches"] = [
        {
            "statement_row_ids": [statement["data"]["rows"][0]["row_id"]],
            "fund_movement_ids": [actual.value["movement_id"]],
        }
    ]
    with pytest.raises(ContractError):
        validate(ReconciliationContext(ids, [statement, check], [actual, funds(ids, 1)]), check)


@pytest.mark.parametrize("change", ["date", "direction", "amount", "duplicate", "foreign_id"])
def test_mismatch_groups_never_invent_a_balancing_fee(ids, change):
    statement, check = pair(ids)
    actual = funds(ids, 300)
    check["data"]["matches"] = [
        {
            "statement_row_ids": [statement["data"]["rows"][0]["row_id"]],
            "fund_movement_ids": [actual.value["movement_id"]],
        }
    ]
    if change == "date":
        actual.value["actual_on"] = "2026-01-21"
    elif change == "direction":
        actual.value["direction"] = "outflow"
    elif change == "amount":
        actual.value["amount_fen"] = "299"
    elif change == "duplicate":
        check["data"]["matches"].append(deepcopy(check["data"]["matches"][0]))
    else:
        check["data"]["matches"][0]["fund_movement_ids"] = [str(uuid4())]
    with pytest.raises(ContractError):
        validate(ReconciliationContext(ids, [statement, check], [actual]), check)


def test_empty_following_month_continues_completed_opening_and_missing_month_blocks_only_that_chain(
    ids,
):
    january, first = pair(ids, values=())
    february, second = pair(ids, values=(), day="2026-02-20")
    context = ReconciliationContext(ids, [january, first, february, second])
    validate(context, first)
    second_plan = validate(context, second)
    assert second_plan["target"]["values"]["opening_fen"] == "0"
    assert len(second_plan["basis"]["source_results"]) == 2
    march, third = pair(ids, values=(), day="2026-03-20")
    missing = ReconciliationContext(ids, [january, first, march, third])
    with pytest.raises(ContractError) as error:
        validate(missing, third)
    assert error.value.code == "needs_information"


def test_bank_statement_opening_is_not_an_accounting_opening(ids):
    statement, check = pair(ids, values=(), opening="999")
    with pytest.raises(ContractError):
        validate(ReconciliationContext(ids, [statement, check]), check)
