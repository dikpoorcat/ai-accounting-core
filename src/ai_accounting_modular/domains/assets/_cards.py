"""Discover stable asset ownership through declared object relations."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import parse_fen

from ._common import (
    CAPITAL,
    PENDING,
    add_months,
    assets_in,
    base_plan,
    consume,
    cost_definition,
    line,
    missing,
    reject,
    result_for,
    source,
)


def related(context, asset_id, kind):
    return context.assets_related(asset_id, kind)


def one_related(fact, context, asset_id, kind, *, required=True):
    matches = related(context, asset_id, kind)
    if len(matches) > 1:
        reject("同一资产不能有多个有效的取得、启用或处置事实。")
    if not matches:
        if required:
            missing(fact, "/data", "资产缺少已登记的公司取得或启用事实。")
        return None
    return matches[0]


def acquisition_card(fact, context, asset_id):
    acquired = one_related(fact, context, asset_id, "assets.acquisition")
    card = context.asset_card(acquired, asset_id)
    return acquired, deepcopy(card)


def activated_cards(fact, context):
    return context.activation_cards(fact, lambda: _activated_cards(fact, context))


def _activated_cards(fact, context):
    cards = []
    for item in assets_in(fact):
        asset_id = item["asset_id"]
        acquired, card = acquisition_card(fact, context, asset_id)
        if acquired["period"] > fact["period"]:
            reject("资产不能在公司取得之前启用。")
        disposals = related(context, asset_id, "assets.disposal")
        if any(item["period"] < fact["period"] for item in disposals):
            reject("已经处置的资产不能再启用。")
        in_use = item.get("in_use_on")
        if in_use is not None:
            if in_use[:7] != fact["period"]:
                reject("实际启用日必须属于明确的启用月。", "invalid_input")
            if acquired.get("effective_on") and acquired["effective_on"] > in_use:
                reject("资产实际启用日不能早于公司实际取得日。")
        cost, residual, life = (
            parse_fen(card["cost_fen"]),
            parse_fen(item["residual_fen"]),
            item["useful_life_months"],
        )
        if residual >= cost or cost - residual < life:
            reject("可摊金额必须小于或等于原价，且足够每月分到至少一分。", "invalid_input")
        if card["asset_type"] == "intangible" and (
            residual != 0 or item["rounding"] != "floor_final_remainder"
        ):
            reject("无形资产残值须为零，采用整分均摊、末月收余数。", "invalid_input")
        monthly = (
            (2 * (cost - residual) + life) // (2 * life)
            if item["rounding"] == "round_half_up_card"
            else (cost - residual) // life
        )
        if monthly * (life - 1) >= cost - residual:
            reject("所选卡片舍入会在寿命末月之前耗尽可摊金额。", "invalid_input")
        cards.append(
            (
                acquired,
                {
                    "type": "asset_card",
                    **card,
                    "accumulated_fen": "0",
                    "completed_months": 0,
                    "consumption_start": add_months(fact["period"], card["asset_type"] == "fixed"),
                    "useful_life_months": life,
                    "residual_fen": item["residual_fen"],
                    "benefit_area": item["benefit_area"],
                    "rounding": item["rounding"],
                },
            )
        )
    return cards


def activation(fact, context):
    cards = activated_cards(fact, context)
    context.load_costs(
        {"business_id": acquired["business_id"], "slot": "cost:" + card["asset_id"]}
        for acquired, card in cards
    )
    plan = base_plan(
        fact,
        context,
        {
            "assets": [
                {
                    key: card[key]
                    for key in (
                        "asset_id",
                        "asset_type",
                        "cost_fen",
                        "consumption_start",
                        "rounding",
                    )
                }
                for _, card in cards
            ]
        },
    )
    for acquired, card in cards:
        cost, asset_id, asset_type = (
            parse_fen(card["cost_fen"]),
            card["asset_id"],
            card["asset_type"],
        )
        original = result_for(context, acquired["business_id"])
        source(plan, original)
        definition = cost_definition(
            fact,
            context,
            {
                "business_id": acquired["business_id"],
                "slot": "cost:" + asset_id,
            },
            object_id=asset_id,
            category="asset",
        )
        if parse_fen(definition.value["amount_fen"]) != cost:
            reject("资产原价与其公开待启用成本不一致。")
        consume(plan, definition, cost, adopt_quantity=False)
        plan["basis"]["adopted_quantities"].append(card)
        line(plan, CAPITAL[asset_type], cost, debit=True)
        line(plan, PENDING[asset_type], cost)
    return plan


def consumption_end(card, context):
    end = add_months(card["consumption_start"], card["useful_life_months"] - 1)
    disposals = related(context, card["asset_id"], "assets.disposal")
    if len(disposals) > 1:
        reject("同一资产不能重复处置。")
    return min(end, disposals[0]["period"]) if disposals else end
