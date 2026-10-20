"""One complete monthly responsibility, with exact per-card closing quantities."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import parse_fen

from ._cards import consumption_end
from ._common import (
    ACCUMULATED,
    CONSUMPTION,
    add_months,
    base_plan,
    checked,
    line,
    month_number,
    reject,
    result_for,
    total,
)


def slot_for(month):
    return "depreciation:" + month


def cards_from_activation(result):
    return [
        deepcopy(item)
        for item in result.basis["adopted_quantities"]
        if item["type"] == "asset_card"
    ]


def monthly_amount(card, opening, completed):
    cost, residual = parse_fen(card["cost_fen"]), parse_fen(card["residual_fen"])
    amount, life = cost - residual, card["useful_life_months"]
    base = (
        (2 * amount + life) // (2 * life)
        if card["rounding"] == "round_half_up_card"
        else amount // life
    )
    if completed < 0 or completed >= life or opening != base * completed:
        reject("摊折必须接续真实采用的上月累计，不能覆盖冻结卡片或补造期初。")
    return amount - opening if completed == life - 1 else base


def consumption(fact, month, context):
    main = result_for(context, fact["business_id"])
    cards = [
        card
        for card in cards_from_activation(main)
        if card["consumption_start"] <= month <= consumption_end(card, context)
    ]
    if not cards:
        reject("本月没有仍需摊折的有效资产卡片。", "invalid_input")
    plan = base_plan(fact, context, {})
    slot = slot_for(month)
    plan.update(result_slot=slot, accounting_period=month, calculation_key="assets.consumption")
    plan["rule_refs"][0]["key"] = "assets.consumption"
    sources = [deepcopy(main.source)]
    adopted, items = [], []
    previous = None
    previous_items = {}
    for card in cards:
        completed = month_number(month) - month_number(card["consumption_start"])
        opening = 0
        if completed:
            if previous is None:
                previous = result_for(context, fact["business_id"], slot_for(add_months(month, -1)))
                sources.append(deepcopy(previous.source))
                previous_items = {
                    item["asset_id"]: item for item in previous.target["values"]["items"]
                }
            row = previous_items.get(card["asset_id"])
            if row is None or row["completed_months"] != completed:
                reject("前月责任结果缺少本卡片的连续摊折数量。")
            opening = parse_fen(row["closing_accumulated_fen"])
        adopted.append({**card, "accumulated_fen": str(opening), "completed_months": completed})
        amount = monthly_amount(card, opening, completed)
        closing = checked(opening + amount)
        items.append(
            {
                "asset_id": card["asset_id"],
                "amount_fen": str(amount),
                "completed_months": completed + 1,
                "closing_accumulated_fen": str(closing),
                "carrying_fen": str(parse_fen(card["cost_fen"]) - closing),
            }
        )
        line(
            plan,
            CONSUMPTION[card["asset_type"]][card["benefit_area"]],
            amount,
            debit=True,
        )
        line(plan, ACCUMULATED[card["asset_type"]], amount)
    plan["target"] = {
        "kind": "assets.consumption",
        "result_slot": slot,
        "values": {
            "total_amount_fen": str(total(parse_fen(item["amount_fen"]) for item in items)),
            "items": items,
        },
    }
    plan["basis"] = {
        "kind": "assets.consumption",
        "result_slot": slot,
        "period": month,
        "source_results": sources,
        "adopted_cards": adopted,
        "source_locations": context.resolve_sources(fact["evidence_ids"]),
    }
    return plan
