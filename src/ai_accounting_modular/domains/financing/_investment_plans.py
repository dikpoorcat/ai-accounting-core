"""Prepare complete typed investment results before controlled posting."""

from copy import deepcopy

from ._common import adopt, amount, base_plan, fail
from ._investment_candidate import REVISION, inventory
from ._investment_events import terms_result
from ._investment_math import Event, decimal_text, reference_key
from ._investment_position import position_plan as position_plan
from ._investment_postings import post
from ._investment_rules import (
    decorate_lot,
    resolved_terms,
    validate_income_character,
    validate_terms,
)
from ._investment_tax import adopt_dividend_holdings, tax_event


def terms(fact, context, *, authorization=None):
    validate_terms(fact, context)
    normalized = resolved_terms(fact["data"])
    values = deepcopy(normalized)
    if authorization is not None:
        found = [
            row
            for row in values.get("settlement_authorizations", ())
            if row["entry_key"] == authorization
        ]
        if len(found) != 1:
            fail("请求的投资结算授权条目不存在。", "invalid_input")
        values = {
            "authorization": {
                "terms_id": fact["business_id"],
                "product_id": values["product_id"],
                **found[0],
            }
        }
    plan = base_plan(fact, context, values, accounting_input=normalized)
    plan["calculation_revision"] = plan["rule_refs"][0]["revision"] = REVISION
    if authorization is not None:
        slot = "authorization:" + authorization
        plan["result_slot"] = plan["target"]["result_slot"] = plan["basis"]["result_slot"] = slot
    return plan


def _adopt_sources(plan, event, context):
    seen = set()
    for use in event.uses:
        reference = use["source"]
        if reference_key(reference) in seen:
            continue
        seen.add(reference_key(reference))
        (definition,) = context.resource_definitions([reference], "cost")
        adopt(plan, definition.source)
        source_result = context.get_result(
            definition.source["business_id"], definition.source["result_slot"]
        )
        lots = [
            item
            for item in source_result.basis["adopted_quantities"]
            if item["type"] == "investment_lot" and item["source"] == reference
        ]
        if len(lots) != 1:
            fail("投资成本来源必须公开唯一的完整批次数量和账面成本。")
        plan["basis"]["adopted_quantities"].append(deepcopy(lots[0]))
        if plan["target"]["kind"] in {"investment.debt_interest", "investment.dispose"}:
            tax = plan["target"]["values"].get("tax_event")
            if tax is not None and tax.get("legal_form") == "direct_bond":
                lot = lots[0]
                entitlement = {
                    "source": deepcopy(reference),
                    "nominal_fen": use["quantity"],
                    "acquired_period": lot["acquired_period"],
                    "origin_source": deepcopy(lot.get("origin_source", reference)),
                }
                for key in (
                    "acquired_on",
                    "acquisition_origin",
                    "eit_interest_realized_through",
                    "eit_interest_source",
                ):
                    if key in lot:
                        entitlement[key] = deepcopy(lot[key])
                tax.setdefault("bond_entitlements", []).append(entitlement)
        value = amount(use["cost_fen"])
        if value:
            plan["cost_consumptions"].append(
                {
                    "source": deepcopy(reference),
                    "consumer": {"business_id": plan["business_id"], "slot": "main"},
                    "amount_fen": str(value),
                    "period": plan["accounting_period"],
                }
            )
        plan["basis"]["adopted_quantities"].append(deepcopy(use))


def event_plan(fact, context):
    adopted = terms_result(fact, context)
    data, term_values = fact["data"], adopted.target["values"]
    if fact["kind"] == "investment.income":
        validate_income_character(term_values, data["income_character"])
        event = Event(
            fact["business_id"], amount=amount(data["amount_fen"]), gain=amount(data["amount_fen"])
        )
        prefix, arrangement = None, term_values["holding_arrangement"]
    else:
        candidate = inventory(context.get_fact(data["terms_id"]), adopted, context)
        event, prefix = candidate.event(fact["business_id"])
        arrangement = prefix.arrangement
    for lot in event.created:
        decorate_lot(lot, term_values, arrangement)
    values = {
        "terms_id": data["terms_id"],
        "product_id": term_values["product_id"],
        "cost_fen": str(event.cost),
        "carrying_change_fen": str(event.book_change),
        "amount_fen": str(event.amount),
        "gain_loss_fen": str(event.gain),
        "allocations": deepcopy(event.uses),
        "created_lots": deepcopy(event.created),
    }
    if prefix is not None:
        values.update(
            quantity=decimal_text(event.quantity), remaining_quantity=decimal_text(prefix.quantity)
        )
    transaction_tax = tax_event(fact, term_values, event)
    if transaction_tax is not None:
        values["tax_event"] = transaction_tax
        if term_values["legal_form"] == "direct_bond" and fact["kind"] in {
            "investment.debt_interest",
            "investment.dispose",
        }:
            transaction_tax["bond_coupon_dates"] = [
                row["payment_on"] for row in term_values["coupon_schedule"]
            ]
    if fact["kind"] == "investment.market_estimate":
        values.update(market_value_fen=data["estimated_value_fen"], market_as_of=data["as_of_on"])
    plan = base_plan(fact, context, values)
    plan["calculation_revision"] = plan["rule_refs"][0]["revision"] = REVISION
    if fact["kind"] == "investment.market_estimate":
        plan["basis"]["effective_on"] = data["as_of_on"]
    elif fact["kind"] == "investment.holding_change":
        plan["basis"]["effective_on"] = data["effective_from"]
    elif fact["kind"] == "investment.debt_interest":
        plan["basis"]["effective_on"] = data["payment_on"]
    adopt(plan, adopted.source)
    _adopt_sources(plan, event, context)
    recognized = data.get("maturity_income", {}).get("recognized_income_source")
    if recognized is not None:
        (definition,) = context.resource_definitions([recognized], "obligation")
        original = context.get_result(
            definition.source["business_id"], definition.source["result_slot"]
        )
        maturity = data["maturity_income"]
        if (
            original.target["kind"] != "investment.income"
            or original.target["values"]["terms_id"] != data["terms_id"]
            or original.basis["input"]["income_character"] != maturity["character"]
            or definition.value.get("party_id") != data["counterparty_id"]
            or definition.value["direction"] != "receivable"
            or definition.value["amount_fen"] != maturity["amount_fen"]
            or recognized["slot"] != "income"
            or original.basis["period"] > fact["period"]
            or (
                "effective_on" in original.basis
                and "effective_on" in fact
                and original.basis["effective_on"] > fact["effective_on"]
            )
        ):
            fail(
                "到期复用的收益须是同持仓、同对象、同性质和同金额的原正式收益应收。",
                "invalid_input",
            )
        adopt(plan, definition.source)
    if fact["kind"] == "investment.income":
        adopt_dividend_holdings(fact, context, plan)
    for lot in event.created:
        plan["cost_lots"].append(
            {
                "business_id": fact["business_id"],
                "slot": lot["source"]["slot"],
                "object_id": term_values["product_id"],
                "category": "investment",
                "amount_fen": lot["carrying_fen"],
                "business_period": fact["period"],
            }
        )
        plan["basis"]["adopted_quantities"].append(deepcopy(lot))
    return post(fact, context, plan, event, term_values, arrangement)
