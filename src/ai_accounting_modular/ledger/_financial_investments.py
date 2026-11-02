"""Public lot quantities supply debt maturity; the GL remains the monetary authority."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import parse_fen, validate_contract

from ._balances import sum_amounts
from ._journal_amounts import journal_amount
from ._values import fail


def _key(reference):
    return reference["business_id"], reference["slot"]


def capture_bonds(selection, heads, journals):
    """Capture only touched formal lots; reporting later reads grouped immutable folds."""
    heads = tuple(heads)
    identities = [head["result_id"] for head in heads]
    selected = [
        head
        for head in heads
        if any(row["account_code"] == "1501" for row in journals[head["result_id"]])
    ]
    result = {identity: [] for identity in identities}
    if not selected:
        return result
    selected_ids = [head["result_id"] for head in selected]
    lots, uses = defaultdict(list), defaultdict(list)
    for row in selection.repo.children("cost_lot", selected_ids):
        if row["category"] == "investment":
            lots[row["result_id"]].append(row)
    for row in selection.repo.children("cost_consumption", selected_ids):
        uses[row["result_id"]].append(row)
    selection.load_bases(selected)
    for head in selected:
        identity = head["result_id"]
        quantities = {}
        for quantity in selection.basis(head)["adopted_quantities"]:
            if quantity["type"] != "investment_lot":
                continue
            validate_contract("business-facts.schema.json", "InvestmentLot", quantity)
            key = _key(quantity["source"])
            old = quantities.setdefault(key, quantity)
            if old != quantity:
                fail("storage_corrupt", "同一投资批次的正式量不一致。")
        values = defaultdict(int)
        for row in lots[identity]:
            key = head["business_id"], row["public_slot"]
            quantity = quantities.get(key)
            if quantity is None:
                fail("storage_corrupt", "正式投资成本缺少对应的公开批次数量。")
            if (
                quantity["product_id"] != row["object_id"]
                or parse_fen(quantity["carrying_fen"]) != row["amount_fen"]
            ):
                fail("storage_corrupt", "投资公开批次与正式成本的对象或金额不一致。")
            if quantity["account_code"] == "1501":
                maturity = quantity.get("matures_on")
                if maturity is None:
                    fail("storage_corrupt", "长期债券的正式批次缺少到期日。")
                dim = quantity["product_id"], maturity
                values[dim] = sum_amounts((values[dim], row["amount_fen"]))
        for row in uses[identity]:
            quantity = quantities.get((row["source_business_id"], row["source_public_slot"]))
            if quantity is not None and quantity["account_code"] == "1501":
                maturity = quantity.get("matures_on")
                if maturity is None:
                    fail("storage_corrupt", "采用的长期债券批次缺少到期日。")
                dim = quantity["product_id"], maturity
                values[dim] = sum_amounts((values[dim], -row["amount_fen"]))
        gl, classified = defaultdict(int), defaultdict(int)
        for row in journals[identity]:
            if row["account_code"] != "1501":
                continue
            gl[row.get("object_id")] = sum_amounts(
                (
                    gl[row.get("object_id")],
                    journal_amount(row["debit_fen"]),
                    -journal_amount(row["credit_fen"]),
                )
            )
        for (product, _), value in values.items():
            classified[product] = sum_amounts((classified[product], value))
        if {key: value for key, value in gl.items() if value} != {
            key: value for key, value in classified.items() if value
        }:
            fail("storage_corrupt", "长期债券的公开账面变动不能与同一正式总账逐产品对平。")
        result[identity] = [
            [product, maturity, value]
            for (product, maturity), value in sorted(values.items())
            if value
        ]
    return result


def opening_bonds(selection):
    if selection.opening is None:
        return []
    head, values, gl = selection.opening, defaultdict(int), defaultdict(int)
    for item in selection.basis(head)["adopted_quantities"]:
        if item["type"] == "investment_lot" and item["account_code"] == "1501":
            validate_contract("business-facts.schema.json", "InvestmentLot", item)
            if item["source"]["business_id"] != head["business_id"] or "matures_on" not in item:
                fail("storage_corrupt", "期初债券公开量的来源或到期日不完整。")
            dim = item["product_id"], item["matures_on"]
            values[dim] = sum_amounts((values[dim], parse_fen(item["carrying_fen"])))
        elif item["type"] == "opening_balance" and item["account_code"] == "1501":
            product = item.get("object_id")
            gl[product] = sum_amounts(
                (gl[product], parse_fen(item["debit_fen"]), -parse_fen(item["credit_fen"]))
            )
    proven = defaultdict(int)
    for (product, _), amount in values.items():
        proven[product] = sum_amounts((proven[product], amount))
    if {key: value for key, value in gl.items() if value} != {
        key: value for key, value in proven.items() if value
    }:
        fail("storage_corrupt", "期初债券公开账面量不能与同一正式期初逐产品对平。")
    return [
        [product, maturity, amount]
        for (product, maturity), amount in sorted(values.items())
        if amount
    ]
