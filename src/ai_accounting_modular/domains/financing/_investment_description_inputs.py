"""One description call indexes each related holding once, without result caching."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import YearMonth

from ._investment_events import POSITION_KINDS, _select
from ._investment_lots import ActiveLots
from ._investment_math import Event, Lot, quantity, rebase
from ._investment_order import ordered_facts
from ._investment_rules import resolved_terms
from ._investment_seed import change_index, choose_seed


class DescriptionInputs:
    def __init__(self, context, changes):
        self._context, self._holdings, self._uses = context, {}, {}
        self._responsibilities = changes
        self._period_facts = {}
        self._legacy_dependencies, self._existing_slots, self._seeds = {}, {}, {}
        self._changes = None

    def __getattr__(self, name):
        return getattr(self._context, name)

    def holding(self, terms_id):
        if terms_id not in self._holdings:
            if self._changes is None:
                self._changes = change_index(self._context, self._responsibilities)
            seed = choose_seed(self._context.get_fact(terms_id), self._context, self._changes)
            self._seeds[terms_id] = seed
            first = (
                str(YearMonth.from_ordinal(YearMonth(seed.period).ordinal + 1))
                if seed is not None
                else None
            )
            related = self._context.related_facts(terms_id, "calculation", period_from=first)
            facts = tuple(ordered_facts(row for row in related if row["kind"] in POSITION_KINDS))
            openings = tuple(row for row in related if row["kind"] == "opening.package")
            if seed is not None:
                self._existing_slots[terms_id] = {
                    row["result_slot"]
                    for row in self._context.current_result_refs((terms_id,))
                    if row["result_slot"].startswith("position:")
                }
                by_slot = {}
                for edge in self._context.current_dependencies((terms_id,)):
                    slot = edge["consumer_result_slot"]
                    if (
                        slot.startswith("position:")
                        and slot.removeprefix("position:") <= seed.period
                    ):
                        by_slot.setdefault(slot, []).append(edge)
                self._legacy_dependencies[terms_id] = by_slot
            self._holdings[terms_id] = facts, openings
        return self._holdings[terms_id]

    def investment_facts(self, terms_id):
        return self.holding(terms_id)[0]

    def investment_period_facts(self, terms_id, period):
        if terms_id not in self._period_facts:
            grouped = {}
            for fact in self.investment_facts(terms_id):
                grouped.setdefault(fact["period"], []).append(fact)
            self._period_facts[terms_id] = {month: tuple(facts) for month, facts in grouped.items()}
        return self._period_facts[terms_id].get(period, ())

    def investment_openings(self, terms_id):
        return self.holding(terms_id)[1]

    def investment_existing_positions(self, terms_id):
        self.holding(terms_id)
        return self._existing_slots.get(terms_id, ())

    def investment_retained_dependencies(self, terms_id, slot):
        self.holding(terms_id)
        return self._legacy_dependencies.get(terms_id, {}).get(slot)

    def investment_seed(self, terms_id):
        self.holding(terms_id)
        return self._seeds[terms_id]

    def investment_uses(self, terms_id, business_id):
        if terms_id not in self._uses:
            self._uses[terms_id] = self._lineage(terms_id)
        return self._uses[terms_id].get(business_id, ())

    def _lineage(self, terms_id):
        """Declare exact source candidates using the shared quantity/FIFO indexes.

        No price, income, tax, carrying amount or formal balance is calculated here.
        Zero prices allow a harmless stable choice within an unresolved FIFO tie;
        the actual accounting replay still rejects a material price ambiguity.
        """
        facts, openings = self.holding(terms_id)
        terms = resolved_terms(self._context.get_fact(terms_id)["data"])
        seed = self.investment_seed(terms_id)
        initial = []
        if seed is not None:
            for row in seed.values["lots"]:
                initial.append(
                    Lot(
                        deepcopy(row["source"]),
                        terms_id,
                        terms["product_id"],
                        terms["quantity_unit"],
                        quantity(row["quantity"]),
                        0,
                        0,
                        row["acquired_period"],
                        row.get("acquired_on"),
                        origin_source=deepcopy(row.get("origin_source")),
                        acquisition_sequence=row.get("acquisition_sequence"),
                    )
                )
        for opening in openings:
            for member in opening["data"]["members"]:
                data = member["data"]
                if data.get("type") != "investment" or data["terms_id"] != terms_id:
                    continue
                initial.append(
                    Lot(
                        {
                            "business_id": opening["business_id"],
                            "slot": "member:" + member["member_id"] + ":cost",
                        },
                        terms_id,
                        terms["product_id"],
                        terms["quantity_unit"],
                        quantity(data["quantity"]),
                        0,
                        0,
                        data["acquired_period"],
                        data.get("acquired_on"),
                        acquisition_sequence=data.get("acquisition_sequence"),
                    )
                )
        lots, uses = ActiveLots(initial), {}
        origins = {row["business_id"]: row for row in facts}
        for fact in facts:
            kind, data = fact["kind"], fact["data"]
            if kind in {"investment.acquire", "investment.reinvest"}:
                lots.append(
                    Lot(
                        {"business_id": fact["business_id"], "slot": "cost"},
                        terms_id,
                        terms["product_id"],
                        terms["quantity_unit"],
                        quantity(data["quantity"]),
                        0,
                        0,
                        fact["period"],
                        fact.get("effective_on"),
                        acquisition_sequence=data.get("transaction_sequence"),
                    )
                )
            elif kind in {"investment.dispose", "investment.loss"}:
                selections = (
                    _select(lots, fact, terms, origins)
                    if kind == "investment.dispose"
                    else [(lots.find(data["source"]), quantity(data["quantity"]))]
                )
                uses[fact["business_id"]] = tuple(deepcopy(lot.source) for lot, _ in selections)
                consumed = [lot.consume(units) for lot, units in selections]
                lots.synchronize(consumed)
            elif kind in {
                "investment.capital_return",
                "investment.unit_change",
                "investment.debt_interest",
            }:
                lot = lots.find(data["source"])
                uses[fact["business_id"]] = (deepcopy(lot.source),)
                event = Event(fact["business_id"])
                successor = rebase(
                    lot,
                    event,
                    quantity_after=(
                        quantity(data["quantity_after"])
                        if kind == "investment.unit_change"
                        else None
                    ),
                )
                lots.append(successor)
        return uses
