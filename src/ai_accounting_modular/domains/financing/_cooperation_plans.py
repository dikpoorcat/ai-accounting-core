"""Cooperation contracts and actual contributions share the usual ledger plan."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import parse_rate

from ._common import ACCOUNTS, amount, fail, format_fen, line, require_object
from ._cooperation_common import adopt_result, cooperation_plan, terms


def contract(fact, context):
    data = fact["data"]
    accounting_input = {
        key: deepcopy(value) for key, value in data.items() if key != "contract_source"
    }
    accounting_input["company_revenue_expense_share"] = format(
        parse_rate(accounting_input["company_revenue_expense_share"]).normalize(), "f"
    )
    values = {key: value for key, value in accounting_input.items() if key != "profile"}
    plan = cooperation_plan(fact, context, values, accounting_input=accounting_input)
    locator = data["contract_source"]
    if locator["source_id"] not in fact["evidence_ids"]:
        fail("合作合同定位必须属于该事实已列明的来源。", "invalid_input")
    plan["basis"]["source_locations"] = [
        deepcopy(locator) if item["source_id"] == locator["source_id"] else item
        for item in plan["basis"]["source_locations"]
    ]
    return plan


def _actual_funds(plan, fact, context, contract_result, *, inflow):
    data, value = fact["data"], amount(fact["data"]["amount_fen"])
    account = require_object(
        fact, context, data["funds_account_id"], ACCOUNTS, "/data/funds_account_id"
    )
    operator, project = (
        contract_result.target["values"]["operator_id"],
        contract_result.target["values"]["project_id"],
    )
    plan["fund_movements"].append(
        {
            "movement_id": context.movement_id(fact["business_id"], "funds"),
            "public_slot": "funds",
            "funds_account_id": data["funds_account_id"],
            "actual_on": data["actual_on"],
            "direction": "inflow" if inflow else "outflow",
            "amount_fen": data["amount_fen"],
            "party_id": operator,
            "cashflow_parts": [
                {
                    "category": "other_operating_receipts"
                    if inflow
                    else "other_operating_payments",
                    "amount_fen": format_fen(value),
                }
            ],
        }
    )
    for source in context.fund_sources(fact, plan["fund_movements"]):
        if source not in plan["basis"]["source_locations"]:
            plan["basis"]["source_locations"].append(deepcopy(source))
    line(
        plan,
        ACCOUNTS[account["kind"]],
        debit=value if inflow else 0,
        credit=0 if inflow else value,
        object_id=data["funds_account_id"],
        summary="实际收回合作款项" if inflow else "实际拨入合作款项",
    )
    line(
        plan,
        "122106",
        debit=0 if inflow else value,
        credit=value if inflow else 0,
        party=operator,
        object_id=project,
        summary="减少合作代管资金" if inflow else "增加合作代管资金",
    )


def actual_event(fact, context):
    contract_result = terms(fact, context)
    plan = cooperation_plan(fact, context, deepcopy(fact["data"]))
    # Platform row selection locates the source; it is not another amount in the target.
    plan["target"]["values"].pop("platform_row_ids", None)
    adopt_result(plan, contract_result)
    if fact["kind"].endswith("funding") or fact["kind"].endswith("distribution"):
        _actual_funds(
            plan,
            fact,
            context,
            contract_result,
            inflow=fact["kind"].endswith("distribution"),
        )
    return plan
