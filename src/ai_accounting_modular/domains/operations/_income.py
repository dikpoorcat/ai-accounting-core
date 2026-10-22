"""A typed receipt belonging to the company is one actual incoming movement."""

from ._common import amount, base_plan, line, reject

_ACCOUNT_CODES = {"bank_account": "1002", "cash_account": "1001", "platform_account": "1012"}
_INCOME_LINES = {
    "bank_interest": ("5603", "银行利息冲财务费用"),
    "company_reward": ("6301", "确认公司奖励"),
    "retained_verification": ("6301", "确认可保留的账户验证收入"),
}


def other_income(fact, context):
    data, total = fact["data"], amount(fact["data"])
    if fact["effective_on"][:7] != fact["period"]:
        reject("其他收入的真实收款日必须属于核算月。", "invalid_input")
    account = context.get_object(data["funds_account_id"])
    plan = base_plan(
        fact,
        context,
        {"amount_fen": data["amount_fen"], "expense_or_income_class": data["income_kind"]},
        adopt_effective_on=True,
    )
    line(plan, _ACCOUNT_CODES[account["kind"]], debit=total, summary="收到属于公司的款项")
    plan["lines"][-1]["object_id"] = data["funds_account_id"]
    income_account, summary = _INCOME_LINES[data["income_kind"]]
    line(plan, income_account, credit=total, summary=summary)
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
