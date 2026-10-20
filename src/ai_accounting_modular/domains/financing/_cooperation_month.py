"""One month's facts plus exact open resources adopted by the preceding position."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import ResourceCutoff, YearMonth

from ._common import amount, fail, total
from ._cooperation_common import AGENCIES, KINDS
from ._cooperation_events import own_event, project_facts, recognition_event, recovery_parent
from ._cooperation_math import Totals
from ._cooperation_sources import adopted_trade, result_trades


def _key(reference):
    return reference["business_id"], reference["slot"]


@dataclass
class _Adoption:
    quantity: dict
    source_groups: list
    current: bool


def _add(catalog, quantity, sources, *, current):
    key = _key(quantity["source"])
    existing = catalog.get(key)
    if existing is None:
        catalog[key] = _Adoption(quantity, [sources], current)
    else:
        if existing.quantity != quantity:
            fail("同一合作资源在前月接续与本月采用中的经营量不一致。")
        existing.source_groups.append(sources)
        existing.current |= current


def _adopted_quantities(result, contract_id, catalog, *, current):
    if result is None:
        return
    sources = tuple(result.basis["source_results"])
    for quantity in result.basis["adopted_quantities"]:
        if quantity["type"] == "cooperation_trade" and quantity["contract_id"] == contract_id:
            _add(catalog, quantity, sources, current=current)


def _states(context, references, period, excluding_consumer, *, components=None):
    states = tuple(
        state
        for start in range(0, len(references), 500)
        for state in context.resource_states(
            references[start : start + 500],
            "obligation",
            ResourceCutoff(period),
            excluding_consumer=excluding_consumer,
            components=components,
        )
    )
    found = {_key(state.definition.reference): state for state in states}
    expected = [_key(reference) for reference in references]
    if len(found) != len(states) or set(found) != set(expected):
        fail("合作接续目录中的真实资源不完整或不唯一。")
    return tuple(found[key] for key in expected)


def _outside(state):
    return total((state.money_settled_fen, state.offset_settled_fen, state.transferred_out_fen))


def _outside_delta(trade, value):
    part = "collected" if trade.side == "revenue" else "paid"
    outgoing = trade.definition.value["direction"] == "payable"
    return {
        "project_" + part + "_fen": trade.sign * value,
        "company_" + part + "_fen": trade.sign * value,
        "company_outside_settled_out_fen" if outgoing else "company_outside_settled_in_fen": value,
    }


@dataclass(frozen=True)
class MonthInputs:
    events: tuple
    trades: tuple
    states: tuple
    sources: tuple
    outside: Totals
    project_changed: bool


def month_inputs(contract_id, context, period, previous, *, excluding_consumer):
    catalog, events, sources, claims = {}, [], [], []
    project_changed = False
    _adopted_quantities(previous, contract_id, catalog, current=False)
    for fact in project_facts(contract_id, context, period, period_from=period):
        result = context.get_result(fact["business_id"], "main")
        if result is None:
            fail("合作当月事项缺少已声明的正式结果。")
        sources.append(result.source)
        if fact["kind"] in KINDS:
            events.append(own_event(fact, result))
            if fact["kind"] in AGENCIES:
                _adopted_quantities(result, contract_id, catalog, current=True)
                project_changed = True
            continue
        for trade in result_trades(result, context):
            if trade.quantity["contract_id"] != contract_id:
                continue
            _add(catalog, trade.quantity, (result.source,), current=True)
            project_changed |= bool(amount(trade.quantity["project_amount_fen"]))
            parent = recovery_parent(trade)
            if parent is None:
                events.append(recognition_event(fact, trade))
            else:
                claims.append(parent)
    for parent in claims:
        if _key(parent) in catalog:
            catalog[_key(parent)].current = True
            continue
        result = context.get_result(parent["business_id"], "main")
        if result is None:
            fail("本月追偿缺少原经营义务的正式结果。")
        found = [trade for trade in result_trades(result, context) if trade.reference == parent]
        if len(found) != 1 or found[0].quantity["contract_id"] != contract_id:
            fail("本月追偿缺少同一合同的原经营公开量。")
        _add(catalog, found[0].quantity, (result.source,), current=True)
    refs = [catalog[key].quantity["source"] for key in sorted(catalog)]
    closing = _states(context, refs, period, excluding_consumer)
    prior_refs = [
        state.definition.reference
        for state in closing
        if state.definition.value["business_period"] < period
    ]
    opening = {}
    if prior_refs:
        before = str(YearMonth.from_ordinal(YearMonth(period).ordinal - 1))
        opening = {
            _key(state.definition.reference): _outside(state)
            for state in _states(
                context,
                prior_refs,
                before,
                excluding_consumer,
                components=("money", "offset", "transfer"),
            )
        }
    kept_states, kept_trades, outside = [], [], Totals()
    for state in closing:
        key = _key(state.definition.reference)
        entry = catalog[key]
        if any(state.definition.source not in group for group in entry.source_groups):
            fail("前月或本月采用的合作源已改变，须先重算相应正式位置，不能使用旧源接续。")
        trade = adopted_trade(entry.quantity, state.definition, state.definition.source)
        cleared = _outside(state) - opening.get(key, 0)
        if cleared < 0:
            fail("同一真实合作源的累计池外清偿不能少于截至前月的累计清偿。")
        if cleared:
            outside = outside.plus(**_outside_delta(trade, cleared))
            entry.current = project_changed = True
        if not entry.current and not state.remaining_fen:
            continue
        kept_states.append(state)
        kept_trades.append(trade)
        sources.append(state.definition.source)
        sources.extend(use.source for use in state.uses)
    unique = {
        (source["business_id"], source["result_slot"], source["result_id"]): source
        for source in sources
    }
    return MonthInputs(
        tuple(events),
        tuple(kept_trades),
        tuple(kept_states),
        tuple(unique[key] for key in sorted(unique)),
        outside,
        project_changed,
    )
