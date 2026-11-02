"""Company method adoption owns monthly investment VAT, independently of a return."""

from calendar import monthrange
from copy import deepcopy

from ai_accounting_modular.contracts.public import YearMonth, parse_rate

from ._common import adopt_source, amount, line, missing, month_end, plan_for, reject
from ._investment_effects import check_effects, interest_effects, pool_effect
from ._investment_tax_math import (
    allocate_interest_vat,
    inventory_month,
    is_financial_transfer,
    tax_on_gross,
    taxable_interest,
    transfer_position,
)
from ._investment_units import UNIT_BASIS_KIND
from ._policies import adopt_policy, covers

BASIS_KIND = "taxes.financial_transfer_basis"
SLOT_PREFIX = "financial_transfer:"
INVESTMENT_TAX_KINDS = (
    "investment.acquire",
    "investment.income",
    "investment.dispose",
    "investment.capital_return",
    "investment.reinvest",
    "investment.unit_change",
    "investment.loss",
    "investment.debt_interest",
    "investment.money_fund_purchase",
    "investment.money_fund_redemption",
    "opening.package",
)
ACCOUNTING_URL = "https://www.mof.gov.cn/zcsjtsgb/gfxwj/201612/t20161203_3583467.htm"


def rule_urls(period, views):
    result = [ACCOUNTING_URL]
    if period >= "2026-01":
        result.extend(
            (
                "https://szs.mof.gov.cn/zhengcefabu/202601/t20260131_3983035.htm",
                "https://shanghai.chinatax.gov.cn/zzzb/zcwj/202602/t479285.html",
            )
        )
    else:
        result.append("https://12366.chinatax.gov.cn/bzds/010/010-5-2.html")
    if any(
        view.event.get("exit_mode") == "maturity"
        or view.event["tax_facts"]["instrument_type"]
        in {"bank_wealth", "contractual_asset_management"}
        and view.event["event_kind"] == "income"
        for view in views
    ):
        result.append("https://www.mof.gov.cn/gp/xxgkml/szs/201612/t20161221_2510638.htm")
    if any(
        view.event["tax_facts"]["instrument_type"] in {"government_bond", "local_government_bond"}
        and taxable_interest(view.event)
        for view in views
    ):
        result.append("https://m.mof.gov.cn/zcfb/202508/t20250801_3969257.htm")
    if period >= "2026-09" and any(
        view.event["tax_facts"]["instrument_type"] in {"unlisted_equity", "listed_equity"}
        for view in views
    ):
        result.append("https://shanghai.chinatax.gov.cn/zcfw/zcfgk/zzs/202608/t481411.html")
    return result


def month_before(period):
    return str(YearMonth.from_ordinal(YearMonth(period).ordinal - 1))


def bases(context):
    return sorted(
        context.facts_in_scope((BASIS_KIND,), "0001-01", "9999-12"),
        key=lambda row: (row["data"]["elected_on"], row["business_id"]),
    )


def validate_bases(context):
    choices = bases(context)
    for current in choices:
        if current["period"] != current["data"]["elected_on"][:7]:
            reject("金融商品买入价方法的业务月份须为有源选择日期所属月。", "invalid_input")
    changed_on = choices[0]["data"]["elected_on"] if choices else None
    for earlier, later in zip(choices, choices[1:], strict=False):
        first, second = earlier["data"], later["data"]
        if first["elected_on"][:7] == second["elected_on"][:7]:
            reject("同一生效月不能登记两份公司金融商品买入价方法。")
        if first["method"] != second["method"]:
            first_date, second_date = changed_on, second["elected_on"]
            months = YearMonth(second_date[:7]).ordinal - YearMonth(first_date[:7]).ordinal
            boundary_day = min(
                int(first_date[8:]), monthrange(int(second_date[:4]), int(second_date[5:7]))[1]
            )
            if months < 36 or months == 36 and int(second_date[8:]) < boundary_day:
                reject("金融商品买入价方法选定后36个月内不得变更。", "invalid_input")
            changed_on = second_date


def slots(fact, context):
    first = max(fact["period"], context.read_context.start_period)
    last = max(first, context.evaluation_period_to)
    later = [
        row for row in bases(context) if row["data"]["elected_on"] > fact["data"]["elected_on"]
    ]
    if later:
        last = min(last, month_before(later[0]["period"]))
    return ["main"] + [
        SLOT_PREFIX + str(YearMonth.from_ordinal(number))
        for number in range(YearMonth(first).ordinal, YearMonth(last).ordinal + 1)
    ]


def previous_slot(fact, context, period):
    previous = month_before(period)
    first = max(fact["period"], context.read_context.start_period)
    if period > first:
        return fact["business_id"], SLOT_PREFIX + previous
    earlier = [
        row for row in bases(context) if row["data"]["elected_on"] < fact["data"]["elected_on"]
    ]
    if earlier and previous >= context.read_context.start_period:
        return earlier[-1]["business_id"], SLOT_PREFIX + previous
    return None


def edge(
    fact,
    producer,
    *,
    consumer="main",
    producer_slot="main",
    mode="result",
    producer_kind="business",
    first=None,
    last=None,
    notify=None,
):
    result = {
        "consumer_business_id": fact["business_id"],
        "consumer_result_slot": consumer,
        "producer_kind": producer_kind,
        "producer_id": producer,
        "producer_public_slot": producer_slot,
        "read_mode": mode,
        "relation_kind": "calculation",
    }
    if first is not None:
        result.update(period_from=first, period_to=last)
    if notify is not None:
        result["notify_effect"] = notify
    return result


def investment_dependencies(
    fact, context, start, end, *, consumer="main", kinds=INVESTMENT_TAX_KINDS
):
    result = [
        edge(
            fact,
            kind,
            consumer=consumer,
            mode="collection",
            producer_kind="scope",
            first=start,
            last=end,
            notify="description",
        )
        for kind in kinds
    ]
    result.extend(
        edge(fact, row["business_id"], consumer=consumer)
        for row in context.facts_in_scope(kinds, start, end)
    )
    return result


def dependencies(fact, context, result_slots):
    result = [
        edge(
            fact,
            BASIS_KIND,
            mode="collection",
            producer_kind="scope",
            first="0001-01",
            last="9999-12",
            notify="description",
        ),
        edge(
            fact,
            "work",
            mode="collection",
            producer_kind="calendar",
            first=max(fact["period"], context.read_context.start_period),
            last="9999-12",
            notify="description",
        ),
    ]
    for slot in result_slots[1:]:
        period = slot.removeprefix(SLOT_PREFIX)
        result.append(edge(fact, fact["business_id"], consumer=slot))
        before = previous_slot(fact, context, period)
        if before:
            result.append(edge(fact, before[0], consumer=slot, producer_slot=before[1]))
        result.extend(
            edge(fact, identity, consumer=slot) for identity in fact["data"]["vat_policy_ids"]
        )
        start = period if before else context.read_context.start_period
        result.extend(investment_dependencies(fact, context, start, period, consumer=slot))
        result.append(
            edge(
                fact,
                UNIT_BASIS_KIND,
                consumer=slot,
                mode="collection",
                producer_kind="scope",
                first=context.read_context.start_period,
                last=period,
                notify="description",
            )
        )
        result.extend(
            edge(fact, row["business_id"], consumer=slot)
            for row in context.facts_in_scope(
                (UNIT_BASIS_KIND,), context.read_context.start_period, period
            )
        )
    return result


def policy_for_month(fact, context, period, plan):
    matches = [
        identity
        for identity in fact["data"]["vat_policy_ids"]
        if (policy := context.get_fact(identity)) is not None
        and policy["kind"] == "taxes.vat_policy"
        and covers(policy, period + "-01", month_end(period))
    ]
    if len(matches) != 1:
        missing(
            fact,
            "/data/vat_policy_ids",
            "月度投资税务需要唯一且完整覆盖该月的已采用小规模VAT政策。",
        )
    policy = adopt_policy(plan, fact, context, matches[0], "taxes.vat_policy", period, period)
    if parse_rate(policy["nominal_rate"]) != parse_rate("0.03"):
        reject(
            "本轮投资VAT只支持已明确采用的小规模纳税人规则，不扩展一般纳税人算法。",
            "unsupported_business",
        )
    return policy


def adoption(fact, context):
    validate_bases(context)
    plan = plan_for(fact, context, {key: fact["data"][key] for key in ("method", "elected_on")})
    plan["basis"]["input"]["vat_policy_ids"].sort()
    return plan


def _adopt_events(plan, views):
    for view in views:
        adopt_source(plan, view.source)
        adopted = {
            "type": "financial_transfer_event",
            "source": deepcopy(view.source),
            "event": deepcopy(view.event),
            "period": view.period,
            "event_index": view.event_index,
        }
        if view.effective_on is not None:
            adopted["effective_on"] = view.effective_on
        plan["basis"]["adopted_quantities"].append(adopted)


def _prior_inventory(fact, context, period, plan):
    start, end = context.read_context.start_period, month_before(period)
    if end < start:
        return []
    views = tuple(context.investment_tax_events(start, end))
    if any(is_financial_transfer(view.event) or taxable_interest(view.event) for view in views):
        missing(
            fact,
            "/data/elected_on",
            "先前月份已有投资应税事项，需要接续当时的真实方法和月度税务依据，不能从本月假设零起点。",
        )
    _adopt_events(plan, views)
    positions = []
    for month in sorted({view.period for view in views}):
        current = tuple(view for view in views if view.period == month)
        adopted_from = len(plan["basis"]["adopted_quantities"])
        positions, _, _, _ = inventory_month(
            positions,
            current,
            fact["data"]["method"],
            unit_bases=unit_bases(context, month),
            adoptions=plan["basis"]["adopted_quantities"],
        )
        _adopt_unit_sources(plan, adopted_from)
    return positions


def unit_bases(context, period):
    results = []
    for fact in context.facts_in_scope(
        (UNIT_BASIS_KIND,), context.read_context.start_period, period
    ):
        result = context.get_result(fact["business_id"], "main")
        if result is None:
            reject("所引用产品单位变换依据尚无正式结果。")
        results.append(result)
    return results


def _adopt_unit_sources(plan, start):
    for quantity in plan["basis"]["adopted_quantities"][start:]:
        if quantity["type"] == "investment_unit_normalization" and "basis_source" in quantity:
            adopt_source(plan, quantity["basis_source"])


def position(fact, result_slot, context):
    if result_slot not in slots(fact, context):
        reject("金融商品月度税务槽超出方法生效及明确工作月份。", "invalid_input")
    period = result_slot.removeprefix(SLOT_PREFIX)
    plan = plan_for(fact, context, {})
    plan["basis"]["input"]["vat_policy_ids"].sort()
    plan["result_slot"] = plan["target"]["result_slot"] = plan["basis"]["result_slot"] = result_slot
    plan["accounting_period"] = plan["basis"]["period"] = period
    plan["basis"].pop("effective_on", None)
    policy = policy_for_month(fact, context, period, plan)
    previous = {}
    before = previous_slot(fact, context, period)
    if before:
        result = context.get_result(*before)
        if result is None:
            reject("金融商品月度税务缺少前月正式位置。")
        previous = result.target["values"]
        adopt_source(plan, result.source)
    else:
        previous["positions"] = _prior_inventory(fact, context, period, plan)
    views = tuple(context.investment_tax_events(period, period))
    for url in rule_urls(period, views):
        if url not in plan["rule_refs"][0]["official_urls"]:
            plan["rule_refs"][0]["official_urls"].append(url)
    _adopt_events(plan, views)
    adopted_from = len(plan["basis"]["adopted_quantities"])
    positions, disposals, difference, interest = inventory_month(
        previous.get("positions", ()),
        views,
        fact["data"]["method"],
        unit_bases=unit_bases(context, period),
        adoptions=plan["basis"]["adopted_quantities"],
    )
    _adopt_unit_sources(plan, adopted_from)
    values = transfer_position(
        previous,
        difference,
        policy["payable_rate"],
        period=period,
        tax_period=policy["threshold_period"],
        december=period.endswith("-12"),
    )
    interest_vat = tax_on_gross(interest, policy["payable_rate"])
    interest_allocations = allocate_interest_vat(views, interest_vat)
    plan["basis"]["adopted_quantities"].extend(interest_allocations)
    values.update(
        type="financial_transfer_position",
        period=period,
        positions=positions,
        disposals=disposals,
        taxable_interest_gross_fen=str(interest),
        interest_vat_fen=str(interest_vat),
    )
    plan["target"]["values"] = values
    tax_end = values["tax_period_start"]
    if policy["threshold_period"] == "quarter":
        tax_end = f"{tax_end[:4]}-{int(tax_end[5:]) + 2:02d}"
    plan["basis"]["adopted_quantities"].append(
        {
            "type": "financial_transfer_tax_position",
            "period": period,
            "tax_period_start": values["tax_period_start"],
            "tax_period_end": tax_end,
            "carry_asset_fen": values["closing_tax_carry_fen"],
            "current_period_payable_fen": values["payable_tax_fen"],
        }
    )
    change = amount(values["recognized_tax_change_fen"])
    if change:
        line(plan, "5111", abs(change), debit=change > 0, summary="金融商品转让本月增值税影响")
        line(
            plan, "222107", abs(change), debit=change < 0, summary="金融商品转让本月税额及负差承接"
        )
    if interest_vat:
        line(plan, "5111", interest_vat, debit=True, summary="投资利息本月增值税影响")
        line(plan, "222101", interest_vat, summary="投资利息本月应计增值税")
    pool_effect(plan, "transfer_vat", change)
    interest_effects(plan, views, interest_allocations)
    check_effects(plan)
    return plan


def period_results(fact, context, plan):
    start, end = fact["data"]["start_month"], fact["data"]["end_month"]
    results = []
    for source in bases(context):
        for slot in slots(source, context)[1:]:
            period = slot.removeprefix(SLOT_PREFIX)
            if start <= period <= end:
                result = context.get_result(source["business_id"], slot)
                if result is None:
                    reject("本期VAT采用前，月度投资税务位置尚未完成。")
                adopt_source(plan, result.source)
                results.append(result)
    if len({row.target["values"]["period"] for row in results}) != len(results):
        reject("同一月份不能采用两个金融商品税务位置。")
    return sorted(results, key=lambda row: row.target["values"]["period"])


def assessment_dependencies(fact, context):
    start, end = fact["data"]["start_month"], fact["data"]["end_month"]
    result = [
        edge(
            fact,
            BASIS_KIND,
            mode="collection",
            producer_kind="scope",
            first="0001-01",
            last="9999-12",
            notify="description",
        )
    ]
    for basis in bases(context):
        for slot in slots(basis, context)[1:]:
            if start <= slot.removeprefix(SLOT_PREFIX) <= end:
                result.append(edge(fact, basis["business_id"], producer_slot=slot))
    result.extend(investment_dependencies(fact, context, start, end))
    return result
