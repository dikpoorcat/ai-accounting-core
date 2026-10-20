"""Cooperation consumes declared trade quantities, never another owner's JSON."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    CooperationTradeQuantity,
    EvaluatedSlot,
    ResourceCutoff,
    ResourceDefinition,
    validate_contract,
)

from ._common import amount, fail, missing
from ._cooperation_common import AGENCIES, event_order, require_source_period


@dataclass(frozen=True)
class Trade:
    result: EvaluatedSlot
    quantity: CooperationTradeQuantity
    definition: ResourceDefinition

    @property
    def reference(self):
        return self.quantity["source"]

    @property
    def side(self):
        return self.quantity["side"]

    @property
    def sign(self):
        return (
            1
            if ((self.side == "revenue") == (self.definition.value["direction"] == "receivable"))
            else -1
        )


def result_trades(result, context):
    quantities = [
        item for item in result.basis["adopted_quantities"] if item["type"] == "cooperation_trade"
    ]
    trades = []
    for item in quantities:
        validate_contract("business-facts.schema.json", "CooperationTradeQuantity", item)
        definitions = context.resource_definitions([item["source"]], "obligation")
        if len(definitions) != 1:
            fail("合作经营量必须指向唯一的当前净经营义务。")
        definition = definitions[0]
        project, company = (
            amount(item["project_amount_fen"]),
            amount(item["company_net_amount_fen"]),
        )
        direction = definition.value["direction"]
        expected_sign = 1 if (item["side"] == "revenue") == (direction == "receivable") else -1
        if (
            (not project and company != 0)
            or project
            and (project > 0) != (expected_sign > 0)
            or company
            and (project > 0) != (company > 0)
            or abs(company) > abs(project)
            or definition.source != result.source
            or definition.reference != item["source"]
            or definition.value["amount_fen"] != str(abs(company))
            or definition.value["category"] != "trade"
            or "party_id" not in definition.value
        ):
            fail("合作公开经营量与其实际净经营义务不一致。")
        trades.append(Trade(result, item, definition))
    return tuple(trades)


def source_trade(fact, context):
    reference = fact["data"]["source"]
    definitions = context.resource_definitions([reference], "obligation")
    if len(definitions) != 1:
        missing(fact, "/data/source", "需要原经营业务公开的净经营义务。", precision=("source",))
    definition = definitions[0]
    result = context.get_result(definition.source["business_id"], definition.source["result_slot"])
    if result is None:
        missing(fact, "/data/source", "原经营义务缺少正式采用结果。", precision=("source",))
    require_source_period(fact, result, "/data/source", "原经营业务核算月")
    found = [trade for trade in result_trades(result, context) if trade.reference == reference]
    if len(found) != 1:
        missing(
            fact,
            "/data/source",
            "原结果没有公开唯一的合作净经营量；不能从摘要或其他模块内部字段推断。",
            precision=("source",),
        )
    trade = found[0]
    if trade.quantity["contract_id"] != fact["data"]["contract_id"]:
        fail("代理清偿的原经营结果不属于该合作合同。")
    direction = "receivable" if fact["kind"].endswith("collection") else "payable"
    if definition.value["direction"] != direction:
        fail("经营者实际代收或代付的方向与原净经营义务不一致。")
    return trade


def agencies_for(fact, context):
    reference = fact["data"]["source"]
    found = {
        item["business_id"]: item
        for item in context.related_facts(reference["business_id"], "settlement")
        if item["kind"] in AGENCIES and item["data"]["source"] == reference
    }
    found[fact["business_id"]] = fact
    return tuple(sorted(found.values(), key=event_order))


def mixed_settlement(fact, context):
    (window,) = context.resource_windows(
        [fact["data"]["source"]],
        "obligation",
        ResourceCutoff(fact["period"], fact.get("effective_on")),
        excluding_consumer=(fact["business_id"], "main"),
        components=("money", "offset", "transfer"),
    )
    state = window.certain
    return bool(
        state.money_settled_fen
        or state.offset_settled_fen
        or state.transferred_out_fen
        or window.uncertain_uses
    )
