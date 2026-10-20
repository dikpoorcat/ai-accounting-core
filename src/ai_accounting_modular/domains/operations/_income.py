"""A bank interest receipt or company reward is one actual incoming movement."""

from ._common import amount, base_plan, line, reject

_ACCOUNT_CODES = {"bank_account": "1002", "cash_account": "1001", "platform_account": "1012"}


def other_income(fact, context):
    data, total = fact["data"], amount(fact["data"])
    if fact["effective_on"][:7] != fact["period"]:
        reject("银行利息或公司奖励的真实收款日必须属于核算月。", "invalid_input")
    account = context.get_object(data["funds_account_id"])
    plan = base_plan(
        fact,
        context,
        {"amount_fen": data["amount_fen"], "expense_or_income_class": data["income_kind"]},
        adopt_effective_on=True,
    )
    line(plan, _ACCOUNT_CODES[account["kind"]], debit=total, summary="收到属于公司的款项")
    plan["lines"][-1]["object_id"] = data["funds_account_id"]
    line(
        plan,
        "5603" if data["income_kind"] == "bank_interest" else "6301",
        credit=total,
        summary="银行利息冲财务费用" if data["income_kind"] == "bank_interest" else "确认公司奖励",
    )
    movement = {
        "movement_id": context.movement_id(fact["business_id"], "funds"),
        "public_slot": "funds",
        "funds_account_id": data["funds_account_id"],
        "actual_on": fact["effective_on"],
        "direction": "inflow",
        "amount_fen": data["amount_fen"],
        "cashflow_parts": [
            {"category": "other_operating_receipts", "amount_fen": data["amount_fen"]}
        ],
    }
    if "counterparty_id" in data:
        movement["party_id"] = data["counterparty_id"]
    plan["fund_movements"].append(movement)
    for location in context.fund_sources(fact, plan["fund_movements"]):
        if location not in plan["basis"]["source_locations"]:
            plan["basis"]["source_locations"].append(location)
    return plan
