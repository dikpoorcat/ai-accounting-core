"""Dispose once, keep actual cash separate, and adopt the precise final card."""

import calendar
from copy import deepcopy
from decimal import ROUND_HALF_UP, Decimal, localcontext

from ai_accounting_modular.contracts.public import parse_fen, parse_rate

from ._cards import acquisition_card, consumption_end, one_related
from ._common import (
    ACCUMULATED,
    CAPITAL,
    PENDING,
    base_plan,
    checked,
    consume,
    cost_definition,
    line,
    missing,
    month_number,
    obligation,
    reject,
    result_for,
    source,
)
from ._consumption import cards_from_activation, slot_for


def disposal_state(fact, context):
    asset_id = fact["data"]["asset_id"]
    acquired, acquired_card = acquisition_card(fact, context, asset_id)
    if acquired["period"] > fact["period"] or (
        acquired.get("effective_on") and acquired["effective_on"] > fact["effective_on"]
    ):
        reject("资产处置不能早于公司的取得。")
    acquisition_result = result_for(context, acquired["business_id"])
    enabled = one_related(fact, context, asset_id, "assets.activation", required=False)
    if enabled is None:
        return acquired_card, 0, PENDING[acquired_card["asset_type"]], [acquisition_result], None
    if enabled["period"] > fact["period"]:
        reject("处置后的资产不能仍保留未来启用。")
    main = result_for(context, enabled["business_id"])
    card = next(item for item in cards_from_activation(main) if item["asset_id"] == asset_id)
    day = next(item for item in enabled["data"]["items"] if item["asset_id"] == asset_id).get(
        "in_use_on"
    )
    if day and day > fact["effective_on"]:
        reject("资产处置实际日不能早于其实际启用日。")
    end = consumption_end(card, context)
    accumulated, results = 0, [main]
    if card["consumption_start"] <= end:
        final = result_for(context, enabled["business_id"], slot_for(end))
        row = next(
            (item for item in final.target["values"]["items"] if item["asset_id"] == asset_id), None
        )
        if row is None:
            reject("处置所需的连续摊折结果缺少本资产。")
        accumulated = parse_fen(row["closing_accumulated_fen"])
        expected = min(
            card["useful_life_months"],
            month_number(end) - month_number(card["consumption_start"]) + 1,
        )
        if row["completed_months"] != expected:
            reject("处置累计摊折未连续到应计月份。")
        results.append(final)
        card = {**card, "accumulated_fen": str(accumulated), "completed_months": expected}
    return card, accumulated, CAPITAL[card["asset_type"]], results, card


def _vat(fact, context, plan):
    data = fact["data"]
    policy = context.get_fact(data["used_asset_vat_policy_id"])
    if policy is None:
        missing(fact, "/data/used_asset_vat_policy_id", "需要资产处置采用的旧资产VAT规则。")
    if policy["kind"] != "taxes.used_asset_vat_policy":
        reject("资产处置须引用旧资产VAT政策。", "invalid_input")
    params, day = policy["data"], fact["effective_on"]
    identity = params["policy"]
    if day < identity["effective_from"] or (
        identity.get("effective_to") and day > identity["effective_to"]
    ):
        reject("旧资产VAT政策不适用于真实处置日。", "unsupported_policy")
    base, payable = parse_rate(params["tax_base_rate"]), parse_rate(params["payable_rate"])
    if payable > base:
        reject("旧资产实纳率不能超过计税基础率。", "unsupported_policy")
    gross = parse_fen(data["gross_proceeds_fen"])
    with localcontext() as calculation:
        calculation.prec = 80
        taxable = checked(
            int((Decimal(gross) / (1 + base)).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        )
        tax = checked(
            int((Decimal(taxable) * payable).quantize(Decimal(1), rounding=ROUND_HALF_UP))
        )
    adopted = result_for(context, policy["business_id"])
    source(plan, adopted)
    plan["basis"]["policies"].append(
        {
            "policy_business_id": policy["business_id"],
            "policy_kind": policy["kind"],
            "parameters": deepcopy(params),
        }
    )
    plan["rule_refs"][0].update(
        policy_business_ids=[policy["business_id"]], official_urls=[identity["official_url"]]
    )
    plan["tax_sales"].append(
        {
            "source_business_id": fact["business_id"],
            "tax_period": fact["period"],
            "net_sales_fen": str(taxable),
            "vat_fen": str(tax),
            "nominal_rate": params["tax_base_rate"],
            "tax_base_rate": params["tax_base_rate"],
            "payable_rate": params["payable_rate"],
            "invoice_kind": data.get("invoice_kind", "unestablished"),
            "exemption_eligible": data["exemption_eligible"],
            "filing_bucket": "used_asset",
            "ranges": [],
        }
    )
    return gross, gross - tax, tax


def disposal(fact, context):
    card, accumulated, account, results, adopted_card = disposal_state(fact, context)
    asset_type, asset_id = card["asset_type"], card["asset_id"]
    if asset_type == "intangible":
        last = calendar.monthrange(int(fact["period"][:4]), int(fact["period"][5:]))[1]
        if fact["data"]["type"] != "scrap" or int(fact["effective_on"][-2:]) != last:
            reject("首交无形资产处置只承接实际月末无收入报废。", "unsupported_business")
    cost = parse_fen(card["cost_fen"])
    carrying = cost - accumulated
    if carrying < 0:
        reject("资产累计摊折不能超过原价。")
    plan = base_plan(fact, context, {})
    plan["basis"]["effective_on"] = fact["effective_on"]
    for result in results:
        source(plan, result)
    if adopted_card is not None:
        plan["basis"]["adopted_quantities"].append(deepcopy(adopted_card))
    else:
        acquired, _ = acquisition_card(fact, context, asset_id)
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
        consume(plan, definition, cost)
    gross, net, tax = _vat(fact, context, plan) if fact["data"]["type"] == "sale" else (0, 0, 0)
    gain = checked(net - carrying)
    plan["target"]["values"] = {
        "asset_id": asset_id,
        "cost_fen": str(cost),
        "accumulated_fen": str(accumulated),
        "carrying_fen": str(carrying),
        "gross_proceeds_fen": str(gross),
        "net_proceeds_fen": str(net),
        "vat_fen": str(tax),
        "gain_loss_fen": str(gain),
    }
    if gross:
        obligation(
            plan, "primary", fact["data"]["buyer_id"], gross, account="1122", direction="receivable"
        )
    line(plan, ACCUMULATED[asset_type], accumulated, debit=True, object_id=asset_id)
    line(plan, account, cost, object_id=asset_id)
    line(plan, "222101", tax)
    if gain > 0:
        line(plan, "630101", gain)
    elif gain < 0:
        line(plan, "571101" if asset_type == "fixed" else "571102", -gain, debit=True)
    return plan
