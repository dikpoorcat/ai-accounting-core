"""Documented money-fund cost lots and independent redemption receivables."""

from copy import deepcopy

from ._common import (
    adopt,
    amount,
    base_plan,
    checked,
    definitions,
    fail,
    format_fen,
    line,
    obligation,
    total,
)


def purchase(fact, context):
    data = fact["data"]
    value = total((amount(data["purchase_price_fen"]), amount(data["acquisition_fees_fen"])))
    plan = base_plan(fact, context, {"fund_id": data["fund_id"], "cost_fen": format_fen(value)})
    line(plan, "1101", debit=value, object_id=data["fund_id"], summary="确认货币基金有据成本")
    line(plan, "2241", credit=value, party=data["counterparty_id"], summary="确认基金申购付款责任")
    obligation(
        plan,
        "primary",
        data["counterparty_id"],
        "payable",
        value,
        "2241",
        "investment",
        "investment_acquisition",
    )
    plan["cost_lots"].append(
        {
            "business_id": fact["business_id"],
            "slot": "cost",
            "object_id": data["fund_id"],
            "category": "money_fund",
            "amount_fen": format_fen(value),
            "business_period": fact["period"],
        }
    )
    return plan


def redemption(fact, context):
    data = fact["data"]
    costs = sorted(data["costs"], key=lambda x: (x["source"]["business_id"], x["source"]["slot"]))
    cost = total(amount(item["amount_fen"]) for item in costs)
    proceeds = amount(data["net_proceeds_fen"])
    gain = checked(proceeds - cost)
    plan = base_plan(
        fact,
        context,
        {
            "fund_id": data["fund_id"],
            "cost_fen": format_fen(cost),
            "net_proceeds_fen": data["net_proceeds_fen"],
            "gain_loss_fen": format_fen(gain),
        },
        accounting_input={**data, "costs": costs},
    )
    sources = {
        (item.reference["business_id"], item.reference["slot"]): item
        for item in definitions(context, [x["source"] for x in costs], "cost")
    }
    for item in costs:
        ref = item["source"]
        source = sources.get((ref["business_id"], ref["slot"]))
        if source is None:
            fail("赎回所需的公开成本来源未完整提供。")
        lot = source.value
        if lot["category"] != "money_fund" or lot["object_id"] != data["fund_id"]:
            fail("赎回只可消费同一具体货币基金的正式成本。")
        result = context.get_result(source.source["business_id"], source.source["result_slot"])
        if result is None or result.basis["period"] > fact["period"]:
            fail("赎回不能早于有据成本取得月份。")
        if (
            "effective_on" in fact
            and "effective_on" in result.basis
            and fact["effective_on"] < result.basis["effective_on"]
        ):
            fail("赎回不能早于已知的真实取得日。")
        adopt(plan, source.source)
        plan["basis"]["adopted_quantities"].append(
            {
                "type": "cost",
                "source": deepcopy(item["source"]),
                "object_id": lot["object_id"],
                "amount_fen": lot["amount_fen"],
            }
        )
        plan["cost_consumptions"].append(
            {
                "source": deepcopy(item["source"]),
                "consumer": {"business_id": fact["business_id"], "slot": "main"},
                "amount_fen": item["amount_fen"],
                "period": fact["period"],
            }
        )
    line(plan, "1101", credit=cost, object_id=data["fund_id"], summary="转出本次赎回账面成本")
    line(
        plan, "1221", debit=proceeds, party=data["counterparty_id"], summary="确认基金赎回净款应收"
    )
    if gain:
        line(
            plan,
            "5111",
            debit=-gain if gain < 0 else 0,
            credit=gain if gain > 0 else 0,
            summary="确认赎回投资损益",
        )
    # A zero obligation is not cash, but retains the public identity for later corrections.
    obligation(
        plan,
        "primary",
        data["counterparty_id"],
        "receivable",
        proceeds,
        "1221",
        "investment",
        "investment_recovery",
    )
    return plan
