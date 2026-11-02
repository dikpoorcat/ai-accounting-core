"""Formal month boundaries reuse the candidate's one inventory calculation."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import YearMonth

from ._common import adopt, base_plan, fail
from ._investment_candidate import REVISION, inventory
from ._investment_math import decimal_text
from ._investment_rules import validate_adopted_terms


def position_plan(fact, result_slot, context):
    period = result_slot.removeprefix("position:")
    adopted = context.get_result(fact["business_id"], "main")
    if adopted is None:
        fail("投资月末位置缺少已声明的条款来源。")
    validate_adopted_terms(adopted, fact["business_id"])
    terms = adopted.target["values"]
    candidate = inventory(fact, adopted, context)
    opening, closing = candidate.month(period)
    values = {
        "terms_id": fact["business_id"],
        "product_id": terms["product_id"],
        "as_of_period": period,
        "product_type": terms["product_type"],
        "measurement": terms["measurement"],
        "quantity_unit": terms["quantity_unit"],
        "holding_arrangement": closing.prefix.arrangement,
        "quantity": decimal_text(closing.prefix.quantity),
        "opening_quantity": decimal_text(opening.quantity),
        "opening_cost_fen": str(opening.cost),
        "closing_cost_fen": str(closing.prefix.cost),
        "lots": list(closing.lots),
        "inventory_revision": REVISION,
    }
    if closing.market_value_fen is not None:
        values.update(market_value_fen=closing.market_value_fen, market_as_of=closing.market_as_of)
    position_fact = {
        **fact,
        "kind": "investment.position",
        "period": period,
        "data": {"terms_id": fact["business_id"], "as_of_period": period},
    }
    position_fact.pop("effective_on", None)
    plan = base_plan(position_fact, context, values)
    plan["result_slot"] = plan["target"]["result_slot"] = plan["basis"]["result_slot"] = result_slot
    plan["calculation_revision"] = plan["rule_refs"][0]["revision"] = REVISION
    refs = list(candidate.required_refs)
    previous = str(YearMonth.from_ordinal(YearMonth(period).ordinal - 1))
    first = max(fact["period"], context.read_context.start_period)
    if previous >= first:
        refs.append({"business_id": fact["business_id"], "slot": "position:" + previous})
    refs.extend(candidate.month_refs(period))
    for source in context.require_result_sources(refs):
        adopt(plan, source)
    plan["basis"]["adopted_quantities"].extend(deepcopy(closing.lots))
    return plan
