"""Detached accounting meaning of already verified current or historical terms."""

from copy import deepcopy

from ._common import fail

_RIGHTS = {
    "fund_units",
    "financial_contract",
    "company_equity",
    "partnership_interest",
    "direct_bond",
}


def _economic_values(values):
    result = deepcopy(values)
    legacy = result.pop("legal_form", None)
    if legacy is not None:
        right = "fund_units" if legacy in {"securities_fund", "contractual_fund"} else legacy
        if "right_type" in result and result["right_type"] != right:
            fail("正式投资条款的历史权利与当前会计权利矛盾。", "storage_corrupt")
        result["right_type"] = right
    if "right_type" in result and result["right_type"] not in _RIGHTS:
        fail("正式投资来源含有未知的会计权利。", "storage_corrupt")
    result.pop("tax_facts", None)
    result.pop("financial_instrument_reference", None)
    return result


def investment_accounting_terms(values):
    """Keep the original formal body and its digest intact; never adapt submitted facts."""
    result = _economic_values(values)
    if "right_type" not in result:
        fail("正式投资条款缺少已知的会计权利。", "storage_corrupt")
    return result


def _simple_fund_event(values, kind, accounting_input):
    if accounting_input is None:
        fail("读取简单基金的原始经济量须同时提供该正式结果的输入。", "invalid_input")
    if accounting_input["fund_id"] != values["fund_id"]:
        fail("简单基金的正式输入与结果产品身份不一致。", "storage_corrupt")
    event = {
        "product_id": values["fund_id"],
        "accounting_cost_fen": values["cost_fen"],
    }
    if kind == "investment.money_fund_purchase":
        event.update(
            event_kind="simple_fund_acquisition",
            purchase_price_fen=accounting_input["purchase_price_fen"],
            acquisition_fees_fen=accounting_input["acquisition_fees_fen"],
        )
    else:
        event.update(
            event_kind="simple_fund_redemption",
            net_proceeds_fen=values["net_proceeds_fen"],
            cost_allocations=[
                {"source": deepcopy(row["source"]), "amount_fen": row["amount_fen"]}
                for row in accounting_input["costs"]
            ],
        )
    return event


def investment_economic_events(values, *, kind=None, input=None):
    """Read retained events without promoting their old tax claims to current economics."""
    names = ("economic_event", "economic_events", "tax_event", "tax_events")
    selected = [name for name in names if name in values]
    if not selected:
        if kind in {"investment.money_fund_purchase", "investment.money_fund_redemption"}:
            return (_simple_fund_event(values, kind, input),)
        return ()
    if len(selected) != 1:
        fail("正式投资结果同时包含多组交易正文，无法确定原事件位置。", "storage_corrupt")
    name = selected[0]
    events = values[name] if name.endswith("events") else (values[name],)
    return tuple(_economic_values(event) for event in events)
