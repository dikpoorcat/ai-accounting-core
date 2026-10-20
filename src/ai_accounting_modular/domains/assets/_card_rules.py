"""One card rule for newly activated assets and sourced opening cards."""

from ai_accounting_modular.contracts.public import parse_fen, validate_contract

from ._common import reject


def monthly_base(card):
    amount = parse_fen(card["cost_fen"]) - parse_fen(card["residual_fen"])
    life = card["useful_life_months"]
    return (
        (2 * amount + life) // (2 * life)
        if card["rounding"] == "round_half_up_card"
        else amount // life
    )


def validate_card(card):
    validate_contract("business-facts.schema.json", "AdoptedAssetQuantity", card)
    cost, residual = parse_fen(card["cost_fen"]), parse_fen(card["residual_fen"])
    amount, life = cost - residual, card["useful_life_months"]
    if residual >= cost or amount < life:
        reject("资产残值须小于原价，可摊金额须足够各月至少一分。", "invalid_input")
    if card["asset_type"] == "intangible" and (
        residual != 0 or card["rounding"] != "floor_final_remainder"
    ):
        reject("无形资产残值须为零，采用整分均摊、末月收余数。", "invalid_input")
    monthly = monthly_base(card)
    if monthly * (life - 1) >= amount:
        reject("卡片舍入会在寿命末月之前耗尽可摊金额。", "invalid_input")
    completed = card["completed_months"]
    if completed > life:
        reject("资产已摊月数不能超过明确使用寿命。", "invalid_input")
    expected = amount if completed == life else monthly * completed
    if parse_fen(card["accumulated_fen"]) != expected:
        reject("累计折旧或摊销与已摊月数及明确卡片舍入规则不一致。")
