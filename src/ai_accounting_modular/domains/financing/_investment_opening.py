"""Sourced investment cutover quantities, without replaying historical trades."""

from copy import deepcopy
from decimal import Decimal

from ai_accounting_modular.contracts.public import parse_fen, validate_contract

from ._common import checked, fail, format_fen, total
from ._investment_rules import account_code, validate_adopted_terms


def _terms(data, period, context):
    result = context.get_result(data["terms_id"], "main")
    if result is None or result.target["kind"] != "investment.terms":
        fail("期初投资须引用已经核验的投资条款正式结果。", "invalid_input")
    if result.basis["period"] > period:
        fail("期初不能采用建账边界之后成立的投资条款。", "invalid_input")
    return result, validate_adopted_terms(result, data["terms_id"])


def _balance(member, code, value, *, product=None, party=None):
    result = {
        "type": "opening_balance",
        "member_id": member["member_id"],
        "account_code": code,
        "debit_fen": format_fen(value),
        "credit_fen": "0",
    }
    if product is not None:
        result["object_id"] = product
    if party is not None:
        result["party_id"] = party
    return result


def _holding(fact, member, terms_result, terms):
    data = member["data"]
    start = fact["period"]
    quantity = Decimal(data["quantity"])
    if quantity <= 0:
        fail("期初持仓须有明确的正份额；只有未收收益时使用独立收益成员。", "invalid_input")
    if terms["quantity_unit"] == "nominal_fen":
        if quantity != quantity.to_integral_value():
            fail("期初票面本金数量须为整数分。", "invalid_input")
        checked(int(quantity))
    if terms["quantity_unit"] == "ownership_fraction" and quantity > 1:
        fail("期初持有的所有权比例不能超过完整权益。", "invalid_input")
    if data["acquired_period"] >= start or (
        "acquired_on" in data and data["acquired_on"][:7] != data["acquired_period"]
    ):
        fail("期初投资取得月份须早于建账月，已知实际日须属于该取得月。", "invalid_input")
    method, carrying = terms["measurement"], parse_fen(data["carrying_fen"])
    if method == "long_bond_cost":
        debt = data.get("debt")
        if debt is None:
            fail("期初长期债券须提供有据面值、未摊折溢价、应计利息和接续起点。", "invalid_input")
        if quantity != parse_fen(debt["face_fen"]):
            fail("期初债券的名义本金数量须等于同批次有据票面本金。", "invalid_input")
        components = total(
            parse_fen(debt[key])
            for key in ("face_fen", "unamortized_premium_fen", "accrued_interest_fen")
        )
        if components != carrying or debt["interest_start_on"] != start + "-01":
            fail("期初债券账面组成须等于账面值，计息接续起点须为明确建账边界。", "invalid_input")
        schedule = terms.get("coupon_schedule", ())
        if not schedule or schedule[-1]["payment_on"] < start + "-01":
            fail("期初债券缺少建账后仍适用的合同支付安排。", "invalid_input")
    elif "debt" in data:
        fail("非长期债券的期初持仓不能附加债券摊销组成。", "invalid_input")
    reference = {
        "business_id": fact["business_id"],
        "slot": "member:" + member["member_id"] + ":cost",
    }
    lot = {
        "type": "investment_lot",
        "source": reference,
        "terms_id": data["terms_id"],
        "product_id": terms["product_id"],
        "account_code": account_code(terms),
        "quantity": data["quantity"],
        "quantity_unit": terms["quantity_unit"],
        "right_type": terms["right_type"],
        **{key: data[key] for key in ("historical_cost_fen", "carrying_fen", "acquired_period")},
    }
    for key in (
        "acquired_on",
        "tax_buy_basis_fen",
        "eit_tax_basis_fen",
        "acquisition_sequence",
        "acquisition_origin",
        "eit_interest_realized_through",
        "eit_interest_source",
    ):
        if key in data:
            lot[key] = deepcopy(data[key])
    if ("eit_interest_realized_through" in data) != ("eit_interest_source" in data):
        fail("期初已实现税息截止日与其实际依据须一同提供。", "invalid_input")
    if data.get("eit_interest_realized_through", "0001-01-01") >= start + "-01":
        fail("期初已实现税息截止日须早于建账边界。", "invalid_input")
    if method == "long_bond_cost":
        lot.update(
            debt_face_fen=data["debt"]["face_fen"],
            opening_debt=deepcopy(data["debt"]),
            matures_on=terms["coupon_schedule"][-1]["payment_on"],
        )
    validate_contract("business-facts.schema.json", "InvestmentLot", lot)
    return {
        "member_id": member["member_id"],
        "balances": [_balance(member, lot["account_code"], carrying, product=lot["product_id"])]
        if carrying
        else [],
        "obligations": [],
        "cost_lots": [
            {
                "business_id": fact["business_id"],
                "slot": reference["slot"],
                "object_id": lot["product_id"],
                "category": "investment",
                "amount_fen": data["carrying_fen"],
                "business_period": start,
            }
        ],
        "adopted_quantities": [lot],
        "source_results": [deepcopy(terms_result.source)],
    }


def _receivable(fact, member, terms_result, terms):
    data = member["data"]
    right, interest = terms["right_type"], data["income_kind"] == "interest"
    if (interest and right not in {"financial_contract", "direct_bond"}) or (
        not interest and right == "direct_bond"
    ):
        fail("期初投资应收收益性质与已确认的法律权利不一致。", "invalid_input")
    code = "1132" if data["income_kind"] == "interest" else "1131"
    reference = {"business_id": fact["business_id"], "slot": "member:" + member["member_id"]}
    obligation = {
        **reference,
        "party_id": data["counterparty_id"],
        "direction": "receivable",
        "category": "investment",
        "amount_fen": data["amount_fen"],
        "account_code": code,
        "allowed_settlements": ["money", "offset", "transfer"],
        "business_period": fact["period"],
        "cashflow_weights": [{"category": "investment_income", "weight": "1"}],
    }
    return {
        "member_id": member["member_id"],
        "balances": [
            _balance(
                member,
                code,
                parse_fen(data["amount_fen"]),
                product=terms["product_id"],
                party=data["counterparty_id"],
            )
        ],
        "obligations": [obligation],
        "cost_lots": [],
        "adopted_quantities": [
            {
                "type": "opening_investment_receivable",
                "source": reference,
                "terms_id": data["terms_id"],
                "product_id": terms["product_id"],
                "income_kind": data["income_kind"],
            },
            {
                "type": "obligation",
                "source": reference,
                "account_code": code,
                "direction": "receivable",
                "amount_fen": data["amount_fen"],
                "party_id": data["counterparty_id"],
            },
        ],
        "source_results": [deepcopy(terms_result.source)],
    }


def opening_investment_members(fact, context):
    """The investment owner supplies complete professional member fragments."""
    from ._cooperation_opening import opening_member as cooperation_member

    result = []
    for member in sorted(fact["data"]["members"], key=lambda item: item["member_id"]):
        data = member["data"]
        if data["type"] == "cooperation":
            result.append(cooperation_member(fact, member, context))
        elif data["type"] in {"investment", "investment_receivable"}:
            adopted, terms = _terms(data, fact["period"], context)
            result.append(
                (_holding if data["type"] == "investment" else _receivable)(
                    fact, member, adopted, terms
                )
            )
    return tuple(result)
