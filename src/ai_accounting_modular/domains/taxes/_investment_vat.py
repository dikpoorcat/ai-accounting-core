"""One return combines ordinary sales and monthly adopted investment tax amounts."""

from copy import deepcopy
from decimal import Decimal, localcontext

from ._common import adopt_source, amount, line, missing, rounded, total
from ._financial_transfer import INVESTMENT_TAX_KINDS, period_results
from ._investment_effects import interest_effects, pool_effect
from ._investment_qualification import economic_rights, qualify
from ._investment_simple import separate
from ._investment_tax_math import (
    allocate_interest_vat,
    event_key,
    is_financial_transfer,
    tax_on_gross,
    taxable_interest,
)


def _check_actual_sources(fact, actual, views):
    if actual is None:
        return
    for field, qualifies, message in (
        (
            "financial_transfer_vat_fen",
            is_financial_transfer,
            "实际转让税额没有本期相应正式转让税源。",
        ),
        ("investment_interest_vat_fen", taxable_interest, "实际利息税额没有本期相应正式利息税源。"),
    ):
        if amount(actual.get(field, "0")) and not any(qualifies(view.event) for view in views):
            missing(fact, "/data/" + field, message)


def components(fact, context, plan, policy, actual=None, *, actual_fact=None):
    results = period_results(fact, context, plan)
    positions = [result.target["values"] for result in results]
    if not positions:
        events = ()
        facts = context.facts_in_scope(
            INVESTMENT_TAX_KINDS, fact["data"]["start_month"], fact["data"]["end_month"]
        )
        if facts:
            events = tuple(
                context.investment_economic_events(
                    fact["data"]["start_month"], fact["data"]["end_month"]
                )
            )
            _, events, _ = separate(fact, (), economic_rights(fact, context, events))
            events, _ = qualify(fact, context, plan, events, purpose="vat_assessment")
            if any(
                is_financial_transfer(view.event) or taxable_interest(view.event) for view in events
            ):
                missing(
                    fact,
                    "/data",
                    "本期投资税源需要先登记公司金融商品买入价方法并完成月度税务位置。",
                )
        _check_actual_sources(actual_fact or fact, actual, events)
        return (), None
    if policy is None and actual is None:
        missing(fact, "/data/vat_policy_id", "采用投资税额需要明确本期适用的小规模VAT政策。")
    last = positions[-1]
    if last["tax_period_start"] != fact["data"]["start_month"]:
        missing(fact, "/data/start_month", "月度金融商品税务位置与本次完整纳税期不一致。")
    gross = max(
        0, amount(last["tax_period_difference_fen"]) - amount(last["tax_period_opening_loss_fen"])
    )
    interest = total(amount(row["taxable_interest_gross_fen"]) for row in positions)
    recognized = {
        "transfer": amount(last["payable_tax_fen"]),
        "interest": total(amount(row["interest_vat_fen"]) for row in positions),
    }
    views = tuple(
        context.investment_economic_events(fact["data"]["start_month"], fact["data"]["end_month"])
    )
    # Monthly positions already own inventory continuity. The return only
    # classifies income/exit events, not every purchase again across the quarter.
    views = tuple(view for view in views if "terms_id" in view.event)
    views, _ = qualify(fact, context, plan, views, purpose="vat_assessment")
    _check_actual_sources(actual_fact or fact, actual, views)
    if not gross and not interest and not any(recognized.values()):
        if not (
            actual is not None
            and any(
                amount(actual.get(key, "0"))
                for key in ("financial_transfer_vat_fen", "investment_interest_vat_fen")
            )
        ):
            return (), None
    rows = []
    parameters = policy
    if parameters is None:
        parameters = next(
            (
                adopted["parameters"]
                for result in results
                for adopted in result.basis["policies"]
                if adopted["policy_kind"] == "taxes.vat_policy"
            ),
            None,
        )
        if parameters is None:
            missing(fact, "/data", "月度投资正式结果缺少所采用税率的冻结依据。")
    for category, value in (("transfer", gross), ("interest", interest)):
        if not value:
            continue
        actual_field = (
            "financial_transfer_vat_fen"
            if category == "transfer"
            else "investment_interest_vat_fen"
        )
        if policy is None:
            if actual_field not in actual:
                missing(
                    actual_fact or fact,
                    "/data/" + actual_field,
                    "没有可用参考时须采用实际来源的完整投资VAT组成。",
                )
            tax = amount(actual[actual_field])
        else:
            tax = tax_on_gross(value, policy["payable_rate"])
        rows.append(
            {
                "source_business_id": fact["business_id"],
                "tax_period": fact["data"]["end_month"],
                "net_sales_fen": str(value - tax),
                "vat_fen": str(tax),
                "nominal_rate": parameters["nominal_rate"],
                "tax_base_rate": parameters["tax_base_rate"],
                "payable_rate": parameters["payable_rate"],
                "invoice_kind": "uninvoiced" if category == "transfer" else "unestablished",
                "exemption_eligible": True,
                "filing_bucket": "standard",
                "ranges": [],
            }
        )
    income_recognized = {}
    for result in results:
        for row in result.basis["adopted_quantities"]:
            if row["type"] == "investment_income_vat":
                key = event_key(row["source"], row["event_index"])
                income_recognized[key] = total(
                    (income_recognized.get(key, 0), amount(row["amount_fen"]))
                )
    return tuple(rows), {
        "recognized": recognized,
        "transfer_gross": gross,
        "interest_gross": interest,
        "views": views,
        "income_recognized": income_recognized,
    }


def ordinary_metrics(fact, ordinary, full, policy, surtax, calculate):
    """Use the single whole-return threshold decision for the ordinary component."""
    net, threshold = amount(full["net_sales_fen"]), amount(policy["threshold_fen"])
    exempt = (
        net < threshold if policy["threshold_operator"] == "strictly_below" else net <= threshold
    )
    forced = deepcopy(policy)
    if not exempt:
        forced["threshold_fen"] = "0"
        forced["threshold_operator"] = "strictly_below"
    result, _ = calculate(fact, ordinary, forced, surtax)
    for field in ("urban_fen", "education_fen", "local_education_fen", "surtax_fen"):
        result[field] = full[field]
    return result


def final_amounts(fact, info, metrics, ordinary, actual):
    if info is None:
        return None
    total_gross = total((info["transfer_gross"], info["interest_gross"]))
    if actual is not None and any(
        key in actual for key in ("financial_transfer_vat_fen", "investment_interest_vat_fen")
    ):
        if not all(
            key in actual for key in ("financial_transfer_vat_fen", "investment_interest_vat_fen")
        ):
            missing_field = next(
                field
                for field in ("financial_transfer_vat_fen", "investment_interest_vat_fen")
                if field not in actual
            )
            missing(
                fact, "/data/" + missing_field, "实际来源需同时明确金融转让及投资利息的VAT组成。"
            )
        transfer, interest = (
            amount(actual["financial_transfer_vat_fen"]),
            amount(actual["investment_interest_vat_fen"]),
        )
        if transfer + interest > amount(metrics["payable_vat_fen"]):
            missing(fact, "/data", "实际投资VAT组成超过同一实际申报应纳总额。")
        return {"transfer": transfer, "interest": interest}
    financial = amount(metrics["payable_vat_fen"]) - amount(ordinary["payable_vat_fen"])
    if financial < 0:
        missing(fact, "/data", "本期实际VAT需要明确服务与投资税额组成。")
    if total_gross:
        with localcontext() as precision:
            precision.prec = 80
            transfer = rounded(Decimal(financial) * info["transfer_gross"] / total_gross)
        interest = financial - transfer
    else:
        transfer = interest = 0
        if financial:
            missing(fact, "/data", "没有对应投资税源的实际投资VAT需要明确来源。")
    return {"transfer": transfer, "interest": interest}


def adjustment_lines(plan, info, final):
    if info is None:
        return
    allocations = allocate_interest_vat(info["views"], final["interest"])
    for quantity in allocations:
        key = event_key(quantity["source"], quantity["event_index"])
        quantity["amount_fen"] = str(
            amount(quantity["amount_fen"]) - info["income_recognized"].get(key, 0)
        )
        adopt_source(plan, quantity["source"])
        plan["basis"]["adopted_quantities"].append(quantity)
    interest_effects(plan, info["views"], allocations)
    pool_effect(plan, "transfer_vat", final["transfer"] - info["recognized"]["transfer"])
    for category, account in (("transfer", "222107"), ("interest", "222101")):
        difference = final[category] - info["recognized"][category]
        if difference:
            line(
                plan,
                "5111",
                abs(difference),
                debit=difference > 0,
                summary="完整纳税期核对投资VAT差额",
            )
            line(
                plan,
                account,
                abs(difference),
                debit=difference < 0,
                summary="完整纳税期投资VAT调整",
            )
