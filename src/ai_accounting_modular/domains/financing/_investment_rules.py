"""Supported investment accounting profiles are code, never free journal settings."""

import calendar
from copy import deepcopy
from datetime import date

from ._common import amount, fail, missing, require_object
from ._investment_accounting import investment_accounting_terms


def validate_term_compatibility(fact):
    """Management names never establish accounting rights or holding intent."""
    data = fact["data"]
    model, right = data["measurement"], data["right_type"]
    intent = data.get("initial_holding_intent")
    allowed = (
        {"short_term_cost", "long_bond_cost"}
        if right == "direct_bond"
        else {"short_term_cost", "long_equity_cost"}
        if right in {"company_equity", "partnership_interest"}
        else {"short_term_cost", "other_financial_cost"}
    )
    if model not in allowed:
        fail("成本核算方法与已确认的法律权利不相容，不能另选模型跳过核算。", "invalid_input")
    if model == "short_term_cost" and (
        data["holding_arrangement"] != "current" or intent == "long_term"
    ):
        fail("短期投资方法须对应实际短期持有安排，不能以报告分类改写长期意图。", "invalid_input")
    if right == "direct_bond" and data["quantity_unit"] != "nominal_fen":
        fail("直接债券的数量须使用有据整数分票面本金。", "invalid_input")
    if data["quantity_unit"] == "ownership_fraction" and right not in {
        "company_equity",
        "partnership_interest",
    }:
        fail("所有权比例只适用于实际股权或合伙权益，不能当作合同本金或基金份数。", "invalid_input")
    schedule = data.get("coupon_schedule", ())
    if right != "direct_bond" and schedule:
        fail("合同付息及折溢价计划只适用于已确认的直接债券权利。", "invalid_input")
    days = [row["payment_on"] for row in schedule]
    if days != sorted(set(days)) or right == "direct_bond" and not schedule:
        fail("直接债券须保存日期不重复、顺序明确的实际付息计划。", "invalid_input")

    if right in {"direct_bond", "company_equity", "partnership_interest"}:
        if intent is None:
            missing(
                fact,
                "/data/initial_holding_intent",
                "直接债券或所有权投资的核算取决于有据原始持有意图，"
                "不能从报告日流动性或已选核算方法倒推。",
                precision=("source",),
            )
        expected = (
            "short_term_cost"
            if intent == "short_term"
            else "long_bond_cost"
            if right == "direct_bond"
            else "long_equity_cost"
        )
        if model != expected:
            fail(
                "直接投资的成本方法与有据原始持有意图不一致，不能另选方法绕过核算。",
                "invalid_input",
            )


def resolved_terms(data):
    result = deepcopy(data)
    result.setdefault("cost_method", "fifo")
    if result["measurement"] == "long_bond_cost":
        result["debt_amortization_method"] = "remaining_contract_periods_straight_line"
    return result


def validate_adopted_terms(result, terms_id):
    data = investment_accounting_terms(result.target["values"])
    validate_term_compatibility(
        {
            "business_id": terms_id,
            "kind": "investment.terms",
            "period": result.basis["period"],
            "evidence_ids": [data["terms_source"]["source_id"]],
            "data": data,
        }
    )
    return data


def _authorization_boundary(value, *, end=False):
    if len(value) == 10:
        return date.fromisoformat(value)
    year, month = map(int, value.split("-"))
    return date(year, month, calendar.monthrange(year, month)[1] if end else 1)


def account_code(terms, arrangement=None):
    method = terms["measurement"]
    if method == "short_term_cost":
        return "1101"
    if method == "long_bond_cost":
        return "1501"
    if method == "long_equity_cost":
        return "1511"
    return "149901" if (arrangement or terms["holding_arrangement"]) == "current" else "189902"


def validate_income_character(terms, character):
    allowed = {
        "company_equity": {"dividend"},
        "financial_contract": {"interest", "market_distribution"},
        "fund_units": {"fund_distribution", "market_distribution"},
        "partnership_interest": {"market_distribution"},
        "direct_bond": set(),
    }[terms["right_type"]]
    if terms["right_type"] == "partnership_interest" and terms["product_type"] == "other_fund":
        allowed = allowed | {"fund_distribution"}
    if character not in allowed:
        fail(
            "投资收益性质与已确认的法律权利不一致，不能通过更换标签改变核算或税务处理。",
            "invalid_input",
        )


def separate_income_character(fact, terms):
    """Resolve an acquired existing right only as far as the known law requires."""
    data = fact["data"]
    if not amount(data.get("separate_declared_income_fen", "0")):
        return None
    supplied = data.get("separate_income_character")
    if terms["right_type"] == "direct_bond":
        if supplied not in {None, "interest"}:
            fail(
                "直接债券取得前旧息由已到息合同权利确定，不能更换为其他收益标签。", "invalid_input"
            )
        return "interest"
    if supplied is None and terms["right_type"] == "financial_contract":
        missing(
            fact,
            "/data/separate_income_character",
            "取得价款已单列既有收益权利，金融合同可能包含利息或非利息分配，"
            "须从实际结算资料确认性质以承接正确的应收权利。",
            precision=("source",),
        )
    if supplied is not None:
        validate_income_character(terms, supplied)
    return supplied


def validate_terms(fact, context):
    data = fact["data"]
    product = require_object(
        fact, context, data["product_id"], {"investment_product"}, "/data/product_id"
    )
    if product["data"]["category"] != data["product_type"]:
        fail("投资条款的产品类别须与实际登记产品一致。", "invalid_input")
    if data["product_type"] == "cooperation":
        fail("合作经营须使用既有合作合同规则，不能套用金融投资计量模型。", "unsupported_business")
    validate_term_compatibility(fact)
    seen = set()
    for authorization in data.get("settlement_authorizations", ()):
        if authorization["entry_key"] in seen:
            fail("同一持仓不能重复使用结算授权条目键。", "invalid_input")
        seen.add(authorization["entry_key"])
        for field in ("principal_party_id", "agent_id"):
            require_object(
                fact,
                context,
                authorization[field],
                {"person", "organization"},
                "/data/settlement_authorizations/" + field,
            )
        if authorization["principal_party_id"] == authorization["agent_id"]:
            fail("投资代理结算授权须区分原义务方和真实代理方。", "invalid_input")
        if "valid_through" in authorization and _authorization_boundary(
            authorization["valid_through"], end=True
        ) < _authorization_boundary(authorization["valid_from"]):
            fail("投资结算授权的结束时间不能早于生效时间。", "invalid_input")


def decorate_lot(lot, terms, arrangement=None):
    lot["account_code"] = account_code(terms, arrangement)
    lot["right_type"] = terms["right_type"]
    if terms["measurement"] == "long_bond_cost":
        lot["matures_on"] = terms["coupon_schedule"][-1]["payment_on"]
    return lot
