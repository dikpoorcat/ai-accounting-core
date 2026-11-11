"""Exact original transaction quantities for the separate tax owner."""

from copy import deepcopy

from ._common import adopt, amount, fail, total
from ._investment_math import decimal_text, reference_key


def adopt_dividend_holdings(fact, context, plan):
    entitlements = fact["data"].get("entitlements")
    if entitlements is None:
        return
    if total(amount(row["amount_fen"]) for row in entitlements) != amount(
        fact["data"]["amount_fen"]
    ):
        fail("股息权益来源的金额合计必须等于本次有据股息。", "invalid_input")
    holdings, seen = [], set()
    for row in entitlements:
        key = reference_key(row["source"])
        if key in seen:
            fail("同次股息不能重复列同一权益来源。", "invalid_input")
        seen.add(key)
        (definition,) = context.resource_definitions([row["source"]], "cost")
        body = context.get_result(
            definition.source["business_id"], definition.source["result_slot"]
        )
        lots = [
            value
            for value in body.basis["adopted_quantities"]
            if value["type"] == "investment_lot" and value["source"] == row["source"]
        ]
        if len(lots) != 1 or lots[0]["terms_id"] != fact["data"]["terms_id"]:
            fail("股息权益须对应同一持仓的准确正式批次。", "invalid_input")
        if (
            lots[0]["acquired_period"] > fact["period"]
            or "acquired_on" in lots[0]
            and "effective_on" in fact
            and lots[0]["acquired_on"] > fact["effective_on"]
        ):
            fail("股息权益不能采用尚未实际取得的未来投资批次。", "invalid_input")
        value = {"acquired_period": lots[0]["acquired_period"], "amount_fen": row["amount_fen"]}
        if "acquired_on" in lots[0]:
            value["acquired_on"] = lots[0]["acquired_on"]
        holdings.append(value)
        adopt(plan, definition.source)
    plan["target"]["values"]["economic_event"]["dividend_holding"] = holdings


def economic_event(fact, term_values, event):
    data, suffix = fact["data"], fact["kind"].removeprefix("investment.")
    kind = (
        "acquisition"
        if suffix in {"acquire", "reinvest"}
        else "disposal"
        if suffix == "dispose"
        else suffix
    )
    if suffix in {"holding_change", "market_estimate"}:
        return None
    result = {
        "terms_id": data["terms_id"],
        "product_id": term_values["product_id"],
        "right_type": term_values["right_type"],
        "quantity_unit": term_values["quantity_unit"],
        "event_kind": kind,
        "quantity": decimal_text(event.quantity),
        "gross_price_fen": "0",
        "transaction_fees_fen": "0",
        "separate_declared_income_fen": "0",
        "accounting_cost_fen": str(event.cost),
        "accounting_income_fen": str(event.gain),
    }
    if suffix == "income":
        result.pop("quantity")
    if suffix == "acquire":
        result.update(
            gross_price_fen=data["purchase_price_fen"],
            transaction_fees_fen=data["acquisition_fees_fen"],
            separate_declared_income_fen=data["separate_declared_income_fen"],
        )
        if "acquired_coupon_dates" in data:
            result["acquired_coupon_dates"] = list(data["acquired_coupon_dates"])
        if event.separate_income_character is not None:
            result["separate_income_character"] = event.separate_income_character
    elif suffix == "reinvest":
        result.update(
            gross_price_fen=str(event.amount - amount(data["acquisition_fees_fen"])),
            transaction_fees_fen=data["acquisition_fees_fen"],
        )
    elif suffix == "dispose":
        result.update(
            gross_price_fen=data["gross_proceeds_fen"],
            transaction_fees_fen=data["disposal_fees_fen"],
            exit_mode=data["exit_mode"],
        )
        if "maturity_income" in data:
            result["maturity_income"] = deepcopy(data["maturity_income"])
    elif suffix in {"income", "debt_interest", "capital_return", "loss"}:
        result["gross_price_fen"] = str(event.amount)
        if suffix != "loss":
            result["income_character"] = (
                "interest"
                if suffix == "debt_interest"
                else data.get("income_character", "capital_return")
            )
    result["lot_allocations"] = [
        {"source": deepcopy(use["source"]), "quantity": use["quantity"]} for use in event.uses
    ]
    if "transaction_sequence" in data:
        result["transaction_sequence"] = data["transaction_sequence"]
    return result
