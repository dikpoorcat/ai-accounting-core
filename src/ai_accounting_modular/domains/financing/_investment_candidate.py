"""One immutable inventory replay per holding and application candidate."""

from copy import deepcopy
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType

from ai_accounting_modular.contracts.public import YearMonth

from ._common import fail, total
from ._investment_events import POSITION_KINDS, opening_lots, replay
from ._investment_math import (
    Lot,
    quantity_total,
)
from ._investment_order import ordered_facts
from ._investment_rules import decorate_lot
from ._investment_seed import REVISION, Seed


def _months(first, last):
    return tuple(
        str(YearMonth.from_ordinal(i))
        for i in range(YearMonth(first).ordinal, YearMonth(last).ordinal + 1)
    )


@dataclass(frozen=True)
class Prefix:
    quantity: Decimal
    cost: int
    arrangement: str


@dataclass(frozen=True)
class Snapshot:
    prefix: Prefix
    lots: tuple
    market_value_fen: str | None = None
    market_as_of: str | None = None


@dataclass(frozen=True)
class Inputs:
    facts: tuple
    opening_refs: tuple
    months: tuple[str, ...]
    seed: Seed | None


def inputs(terms_fact, adopted, context):
    terms_id = terms_fact["business_id"]

    def build():
        refs = tuple(
            ref
            for ref in context.declared_result_refs(terms_id, "reference")
            if ref["slot"].startswith("position:")
        )
        if len(refs) > 1:
            fail("同一候选库存只能采用一个预先声明的接续位置。", "dependency_conflict")
        seed = None
        if refs:
            (expected,) = context.require_result_sources(refs)
            if context.is_result_pending(terms_id, refs[0]["slot"]):
                fail("接续位置也在本单重算，不能把它当成保留状态。", "dependency_conflict")
            previous = context.get_result(terms_id, refs[0]["slot"])
            value = previous.target["values"]
            if (
                value.get("inventory_revision") != REVISION
                or not all("allocation_state" in lot for lot in value["lots"])
                or not any(source == adopted.source for source in previous.basis["source_results"])
            ):
                fail(
                    "预先声明的接续位置不能证明当前核算依据，须完整声明输入后重算。",
                    "dependency_conflict",
                )
            seed = Seed(refs[0]["slot"].removeprefix("position:"), expected, value)
        first = str(YearMonth.from_ordinal(YearMonth(seed.period).ordinal + 1)) if seed else None
        related = context.related_facts(terms_id, "calculation", period_from=first)
        facts = tuple(ordered_facts(row for row in related if row["kind"] in POSITION_KINDS))
        openings = tuple(
            {"business_id": row["business_id"], "slot": "main"}
            for row in related
            if row["kind"] == "opening.package"
        )
        months = _months(
            max(terms_fact["period"], context.read_context.start_period),
            context.evaluation_period_to,
        )
        return Inputs(facts, openings, months, seed)

    return context.candidate_memo("investment.inputs", terms_id, build)


class ReplayResult:
    def __init__(self, *, events, prefixes, snapshots, opening, sources, refs, facts):
        self._events, self._prefixes, self._snapshots = events, prefixes, snapshots
        self._opening = opening
        self.expected_sources, self.required_refs = sources, refs
        month_refs = {}
        for fact in facts:
            month_refs.setdefault(fact["period"], []).append(
                {"business_id": fact["business_id"], "slot": "main"}
            )
        self._month_refs = MappingProxyType(
            {period: tuple(refs) for period, refs in month_refs.items()}
        )

    def event(self, business_id):
        if business_id not in self._events:
            fail("当前投资事件不在已核验的候选回放范围内。", "dependency_conflict")
        return deepcopy(self._events[business_id]), self._prefixes[business_id]

    def month(self, period):
        snapshot = self._snapshots[period]
        previous = str(YearMonth.from_ordinal(YearMonth(period).ordinal - 1))
        beginning = self._snapshots.get(previous, self._opening)
        return beginning.prefix, deepcopy(snapshot)

    def month_refs(self, period):
        return deepcopy(self._month_refs.get(period, ()))


def _restore_lots(snapshot):
    """Restore the original interval frozen in the formal monthly checkpoint."""
    return tuple(Lot.from_wire(value) for value in snapshot["lots"])


def inventory(terms_fact, adopted, context):
    data = inputs(terms_fact, adopted, context)
    refs = ({"business_id": terms_fact["business_id"], "slot": "main"}, *data.opening_refs)
    if data.seed is not None:
        refs += (
            {"business_id": terms_fact["business_id"], "slot": "position:" + data.seed.period},
        )
    sources = context.require_result_sources(refs)
    if data.seed is not None and sources[-1] != data.seed.source:
        fail("投资接续来源已改变，不能复用旧候选计算。", "dependency_conflict")

    def build():
        terms = deepcopy(adopted.target["values"])
        initial, start, estimate = None, context.read_context.start_period, (None, None)
        facts = data.facts
        seed = data.seed.values if data.seed is not None else None
        if seed is not None:
            initial = _restore_lots(seed)
            start = str(YearMonth.from_ordinal(YearMonth(data.seed.period).ordinal + 1))
            terms["holding_arrangement"] = seed["holding_arrangement"]
            estimate = seed.get("market_value_fen"), seed.get("market_as_of")
        else:
            initial = tuple(
                opening_lots(terms_fact["business_id"], context, opening_refs=data.opening_refs)
            )
        first = Prefix(
            quantity_total(lot.remaining_quantity for lot in initial),
            total(lot.remaining_cost for lot in initial),
            terms["holding_arrangement"],
        )
        if seed is not None and (
            first.quantity != Decimal(seed["quantity"])
            or first.cost != int(seed["closing_cost_fen"])
        ):
            fail("正式月末持仓的批次合计与位置金额或数量不一致。", "storage_corrupt")
        first_lots = tuple(
            decorate_lot(lot.wire(allocation_state=True), terms, first.arrangement)
            for lot in initial
        )
        opening = Snapshot(first, first_lots, *estimate)
        snapshots = {data.seed.period: opening} if seed is not None else {}
        prefixes, month_ends = {}, {}
        facts = tuple(row for row in facts if row["period"] >= start)
        last_ids = {row["period"]: row["business_id"] for row in facts}
        latest = list(estimate)

        def observe(fact, event, lots, arrangement):
            prefix = Prefix(lots.quantity, lots.cost, arrangement)
            prefixes[fact["business_id"]] = prefix
            if fact["kind"] == "investment.market_estimate":
                latest[:] = fact["data"]["estimated_value_fen"], fact["data"]["as_of_on"]
            if last_ids[fact["period"]] == fact["business_id"]:
                values = tuple(
                    decorate_lot(lot.wire(allocation_state=True), terms, arrangement)
                    for lot in lots
                )
                month_ends[fact["period"]] = Snapshot(prefix, values, *latest)

        events, _, _ = replay(
            terms_fact["business_id"],
            terms,
            facts,
            initial=initial,
            start_period=start,
            observe=observe,
            ordered=True,
        )
        last = opening
        for month in data.months:
            if month < start:
                continue
            last = month_ends.get(month, last)
            snapshots[month] = last
        return ReplayResult(
            events=events,
            prefixes=prefixes,
            snapshots=snapshots,
            opening=opening,
            sources=sources,
            refs=refs,
            facts=facts,
        )

    result = context.candidate_memo("investment.inventory", terms_fact["business_id"], build)
    if result.expected_sources != sources or tuple(result.required_refs) != tuple(refs):
        fail("投资候选计算的正式来源已经变化，不能复用旧状态。", "dependency_conflict")
    return result
