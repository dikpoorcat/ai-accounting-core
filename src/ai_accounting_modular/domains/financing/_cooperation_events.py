"""One public event stream separates recognition, pool cash and outside discharge."""

from collections import defaultdict, deque
from dataclasses import dataclass, fields

from ai_accounting_modular.contracts.public import ResourceCutoff

from ._common import amount, fail
from ._cooperation_common import AGENCIES, KINDS, PREFIX, actual_on
from ._cooperation_math import Totals
from ._cooperation_sources import result_trades


@dataclass(frozen=True)
class Event:
    business_id: str
    period: str
    on: str | None
    delta: Totals
    fact: dict | None = None
    reference: dict | None = None


def project_facts(contract_id, context, period_to):
    return tuple(
        fact
        for fact in context.related_facts(contract_id, "calculation", period_to=period_to)
        if fact["business_id"] != contract_id
    )


def recovery_parent(trade):
    """Even a zero-company recovery adopts its original public obligation."""
    if trade.result.target["kind"] != "operations.overpayment":
        return None
    originals = [
        item["source"]
        for item in trade.result.basis["adopted_quantities"]
        if item["type"] == "obligation"
    ]
    if len(originals) != 1:
        fail("合作追偿必须采用唯一的原经营义务，不能推测项目超付来源。")
    return originals[0]


def own_event(fact, result):
    data, kind = fact["data"], fact["kind"]
    delta = Totals()
    if kind == PREFIX + "funding":
        delta = delta.plus(company_funded_fen=amount(data["amount_fen"]))
    elif kind == PREFIX + "distribution":
        value = amount(data["amount_fen"])
        changes = {"company_received_fen": value}
        if data["receipt_kind"] == "profit":
            changes["company_profit_received_fen"] = value
        elif data["receipt_kind"] == "capital_return":
            changes["company_capital_returned_fen"] = value
        else:
            changes["operator_funded_fen"] = value
        delta = delta.plus(**changes)
    elif kind == PREFIX + "operator_transfer":
        value = amount(data["amount_fen"])
        changes = {
            "operator_funded_fen"
            if data["direction"] == "into_pool"
            else "operator_withdrawn_fen": value
        }
        if data["purpose"] == "profit":
            changes["operator_profit_received_fen"] = value
        delta = delta.plus(**changes)
    elif kind in AGENCIES:
        values = result.target["values"]
        quantities = [
            item
            for item in result.basis["adopted_quantities"]
            if item["type"] == "cooperation_trade"
        ]
        if len(quantities) != 1:
            fail("正式代理结果必须采用唯一的合作净经营量。")
        trade = quantities[0]
        sign = 1 if (trade["side"] == "revenue") == kind.endswith("collection") else -1
        part = "collected" if trade["side"] == "revenue" else "paid"
        delta = delta.plus(
            **{
                "project_" + part + "_fen": sign * amount(values["project_amount_fen"]),
                "company_" + part + "_fen": sign * amount(values["company_amount_fen"]),
            }
        )
    return Event(
        fact["business_id"],
        fact["period"],
        actual_on(fact),
        delta,
        fact,
        fact["data"].get("source") if kind in AGENCIES else None,
    )


def recognition_event(fact, trade):
    suffix = "revenue_fen" if trade.side == "revenue" else "expense_fen"
    delta = Totals().plus(
        **{
            "project_" + suffix: amount(trade.quantity["project_amount_fen"]),
            "company_" + suffix: amount(trade.quantity["company_net_amount_fen"]),
        }
    )
    return Event(fact["business_id"], fact["period"], fact.get("effective_on"), delta, fact)


def _outside_event(use, state, side, sign, result):
    value = amount(use.value["amount_fen"])
    part = "collected" if side == "revenue" else "paid"
    out = state.definition.value["direction"] == "payable"
    delta = Totals().plus(
        **{
            "project_" + part + "_fen": sign * value,
            "company_" + part + "_fen": sign * value,
            "company_outside_settled_out_fen" if out else "company_outside_settled_in_fen": value,
        }
    )
    day = use.value.get("actual_on", result.basis.get("effective_on"))
    return Event(
        use.source["business_id"],
        result.basis["period"],
        day,
        delta,
        reference=state.definition.reference,
    )


@dataclass(frozen=True)
class Inputs:
    events: tuple[Event, ...]
    trades: tuple
    states: tuple
    sources: tuple
    project_unsettled: bool


def inputs(contract_id, context, period, *, excluding_consumer=None):
    cutoff = ResourceCutoff(period)
    events, trades, sources = [], [], []
    for fact in project_facts(contract_id, context, period):
        result = context.get_result(fact["business_id"], "main")
        if result is None:
            fail("合作位置缺少已声明的当前业务正式结果。")
        sources.append(result.source)
        if fact["kind"] in KINDS:
            events.append(own_event(fact, result))
        else:
            for trade in result_trades(result, context):
                if trade.quantity["contract_id"] != contract_id:
                    continue
                trades.append(trade)
                if fact["kind"] != "operations.overpayment":
                    events.append(recognition_event(fact, trade))
    claims = defaultdict(list)
    for trade in trades:
        parent = recovery_parent(trade)
        if parent is not None:
            claims[(parent["business_id"], parent["slot"])].append(trade)
    queue = deque(
        (trade.reference, trade.side, trade.sign, trade.reference)
        for trade in trades
        if trade.result.target["kind"] != "operations.overpayment"
    )
    states, visited, project_unsettled = [], set(), False
    roots = {}
    while queue:
        reference, side, sign, root = queue.popleft()
        key = reference["business_id"], reference["slot"]
        if key in visited:
            continue
        visited.add(key)
        roots[key] = root
        for claim in claims[key]:
            if claim.side != side or claim.sign != -sign:
                fail("合作追偿的公开方向与原经营义务不一致。")
            queue.append((claim.reference, side, claim.sign, root))
        (state,) = context.resource_states(
            [reference], "obligation", cutoff, excluding_consumer=excluding_consumer
        )
        states.append(state)
        sources.append(state.definition.source)
        for use in state.uses:
            result = context.get_result(use.source["business_id"], use.source["result_slot"])
            if result is None:
                fail("合作池外清偿缺少正式采用依据。")
            sources.append(use.source)
            if use.kind == "reclassification" and use.value["mode"] == "reclassify_excess":
                queue.append((use.value["target"], side, -sign, root))
            elif use.kind == "reclassification" or use.value.get("method") != "agency":
                events.append(_outside_event(use, state, side, sign, result))
    cleared = defaultdict(int)
    for event in events:
        reference = event.reference
        if reference is not None:
            root = roots.get((reference["business_id"], reference["slot"]))
            if root is not None:
                key = root["business_id"], root["slot"]
                cleared[key] += event.delta.project_collected_fen + event.delta.project_paid_fen
    for trade in trades:
        if trade.result.target["kind"] == "operations.overpayment":
            continue
        project_paid = (
            trade.sign * cleared[(trade.reference["business_id"], trade.reference["slot"])]
        )
        project_unsettled |= project_paid != abs(amount(trade.quantity["project_amount_fen"]))
    unique_sources = {
        (item["business_id"], item["result_slot"], item["result_id"]): item for item in sources
    }
    return Inputs(
        tuple(events),
        tuple(trades),
        tuple(states),
        tuple(unique_sources[key] for key in sorted(unique_sources)),
        project_unsettled,
    )


def totals(events):
    result = Totals()
    for event in events:
        result = result.plus(
            **{field.name: getattr(event.delta, field.name) for field in fields(Totals)}
        )
    return result
