"""Cooperation consumes declared trade quantities, never another owner's JSON."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    CooperationTradeQuantity,
    EvaluatedSlot,
    ResourceCutoff,
    ResourceDefinition,
    validate_contract,
)

from ._common import amount, fact_error, fail
from ._cooperation_common import AGENCIES, event_order, require_source_period


@dataclass(frozen=True)
class Trade:
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


@dataclass(frozen=True)
class PublishedTrade(Trade):
    result: EvaluatedSlot


def adopted_trade(item, definition, source):
    validate_contract("business-facts.schema.json", "CooperationTradeQuantity", item)
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
        or definition.source != source
        or definition.reference != item["source"]
        or definition.value["amount_fen"] != str(abs(company))
        or definition.value["category"] != "trade"
        or "party_id" not in definition.value
    ):
        fail("合作公开经营量与其实际净经营义务不一致。")
    return Trade(item, definition)


def result_trades(result, context):
    quantities = [
        item for item in result.basis["adopted_quantities"] if item["type"] == "cooperation_trade"
    ]
    trades = []
    for item in quantities:
        definitions = context.resource_definitions([item["source"]], "obligation")
        if len(definitions) != 1:
            fail("合作经营量必须指向唯一的当前净经营义务。")
        trade = adopted_trade(item, definitions[0], result.source)
        trades.append(PublishedTrade(trade.quantity, trade.definition, result))
    return tuple(trades)


def source_trade(fact, context):
    reference = fact["data"]["source"]
    definitions = context.resource_definitions([reference], "obligation")
    if len(definitions) != 1:
        fact_error(
            fact,
            "/data/source",
            "所引用的原经营义务尚无唯一正式定义，请检查其业务编号、结果槽和正式结果。",
            error="dependency_conflict",
            code="result_not_ready",
            precision=("source",),
        )
    definition = definitions[0]
    result = context.get_result(definition.source["business_id"], definition.source["result_slot"])
    if result is None:
        fact_error(
            fact,
            "/data/source",
            "所引用的原经营义务尚无正式采用结果，请检查上游核算。",
            error="dependency_conflict",
            code="result_not_ready",
            precision=("source",),
        )
    require_source_period(fact, result, "/data/source", "原经营业务核算月")
    found = [trade for trade in result_trades(result, context) if trade.reference == reference]
    if len(found) != 1:
        fact_error(
            fact,
            "/data/source",
            "原结果没有公开唯一的合作净经营量；不能从摘要或其他模块内部字段推断。",
            error="invalid_input",
            code="incompatible_source_result",
            precision=("source",),
        )
    trade = found[0]
    if trade.quantity["contract_id"] != fact["data"]["contract_id"]:
        fact_error(
            fact,
            "/data/source",
            "代理清偿的原经营结果不属于该合作合同。",
            error="invalid_input",
            code="incompatible_source_result",
        )
    direction = "receivable" if fact["kind"].endswith("collection") else "payable"
    if definition.value["direction"] != direction:
        fact_error(
            fact,
            "/data/source",
            "经营者实际代收或代付的方向与原净经营义务不一致。",
            error="invalid_input",
            code="incompatible_source_result",
        )
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
