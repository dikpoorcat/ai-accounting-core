"""Opening retains its fact; this module supplies only asset calculations."""

from ai_accounting_modular.contracts.public import canonical_json, validate_contract

from ._card_rules import validate_card
from ._cards import consumption_end, related
from ._common import (
    add_months,
    assets_in,
    finalize_plan,
    issue,
    month_at,
    month_number,
    reject,
    unique_items,
)
from ._consumption import consumption, slot_for
from ._description import _edge, monthly_dependencies, publications, source_scopes
from ._inputs import AssetInputs
from ._origins import opening_geometry, validate_origins


def _check_fact(fact, context):
    validate_contract("business-facts.schema.json", "Fact", fact)
    if fact["kind"] != "opening.package" or fact["period"] != context.read_context.start_period:
        reject("资产期初接续只能采用公司起账月的严格期初事实。", "invalid_input")


def _context(fact, context):
    _check_fact(fact, context)
    inputs = AssetInputs(context, facts=(fact,))
    inputs.load_assets(item["asset_id"] for item in assets_in(fact))
    return inputs


def cards(fact, context):
    _check_fact(fact, context)
    result = []
    for index, member in enumerate(fact["data"]["members"]):
        data = member["data"]
        if data["type"] != "asset":
            continue
        obj = context.get_object(data["asset_id"])
        if obj is None:
            message = "期初卡片所填资产编号不存在；先查询已有资产档案或登记实际资产。"
            reject(
                message,
                "invalid_input",
                fact_issues=[
                    issue(
                        fact,
                        f"/data/members/{index}/data/asset_id",
                        message,
                        code="reference_not_found",
                    )
                ],
            )
        if obj["kind"] != "asset" or obj["data"]["asset_type"] != data["asset_type"]:
            reject("期初卡片资产类别须与稳定资产档案一致。", "invalid_input")
        if data.get("in_use_on", data["in_use_period"])[:7] != data["in_use_period"]:
            reject("期初资产真实启用日须属于明确启用月。", "invalid_input")
        if data["in_use_period"] >= fact["period"]:
            reject("期初在用资产的真实启用月须早于建账月。")
        start = add_months(data["in_use_period"], int(data["asset_type"] == "fixed"))
        available = max(0, month_number(fact["period"]) - month_number(start))
        if data["completed_months"] != min(available, data["useful_life_months"]):
            reject("期初已摊月数须符合真实启用月、固定或无形起摊规则及建账边界。")
        card = {
            **{
                key: value
                for key, value in data.items()
                if key not in {"in_use_period", "in_use_on"}
            },
            "type": "asset_card",
            "consumption_start": start,
        }
        validate_card(card)
        result.append(card)
    unique_items(result, "asset_id", "期初资产卡片不能重复稳定资产身份。")
    return tuple(sorted(result, key=lambda item: item["asset_id"]))


def describe(fact, context):
    context = _context(fact, context)
    geometries = opening_geometry(fact)
    months, edges = set(), source_scopes(fact, context)
    start = context.read_context.start_period
    terminals = []
    for card in geometries:
        asset_id = card["asset_id"]
        edges.append(_edge(fact, "object", asset_id, mode="collection"))
        for disposed in related(context, asset_id, "assets.disposal"):
            edges.append(_edge(fact, "business", disposed["business_id"], relation="calculation"))
        terminal = consumption_end(card, context)
        terminals.append(terminal)
        end = min(terminal, context.evaluation_period_to)
        for number in range(
            month_number(max(start, card["consumption_start"])), month_number(end) + 1
        ):
            months.add(month_at(number))
    if terminals and max(terminals) >= start:
        edges.append(
            _edge(
                fact,
                "calendar",
                "work",
                mode="collection",
                relation="calculation",
                period_from=start,
                period_to=max(terminals),
            )
        )
    slots = [slot_for(month) for month in sorted(months)]
    edges.extend(monthly_dependencies(fact, slots))
    unique = {canonical_json(edge): edge for edge in edges}
    return {
        "complete_result_slots": slots,
        "dependencies": list(unique.values()),
        "publications": publications(fact, slots),
        "issues": [],
    }


def evaluate(fact, result_slot, context):
    context = _context(fact, context)
    if not result_slot.startswith("depreciation:"):
        reject("期初资产只承担公开描述的月度摊折槽。", "invalid_input")
    month = result_slot[len("depreciation:") :]
    validate_contract("core.schema.json", "Month", month)
    if not context.read_context.start_period <= month <= context.evaluation_period_to:
        reject("期初资产摊折须在建账边界及明确工作范围内。", "invalid_input")
    result = context.get_result(fact["business_id"], "main")
    if result is None or result.target["kind"] != "opening.package":
        reject("期初资产须先取得已声明的main正式卡片依据。")
    plan = consumption(fact, month, context)
    finalize_plan(plan)
    validate_contract("business-facts.schema.json", "PostingPlan", plan)
    return plan


def validate(plan, context):
    validate_contract("business-facts.schema.json", "ChangePlan", plan)
    if plan["domain"] != "opening":
        reject("期初资产协作只核验完整opening领域候选。", "invalid_input")
    facts = [item["fact"] for item in plan["businesses"] if "fact" in item]
    context = AssetInputs(context)
    ids = [item["asset_id"] for fact in facts for item in assets_in(fact)]
    context.load_assets(ids)
    validate_origins(context, ids)
