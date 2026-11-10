"""The same declared checkpoint decision is used before and during evaluation."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import YearMonth

from ._investment_events import POSITION_KINDS
from ._investment_rules import resolved_terms

REVISION = 2


@dataclass(frozen=True)
class ChangeIndex:
    facts: dict
    old_periods: dict
    old_holdings: dict


@dataclass(frozen=True)
class Seed:
    period: str
    source: dict
    values: dict


def change_index(context, responsibilities):
    # An existing inventory event has only its main result. If application
    # notifications included it for recalculation, replay must cover its month
    # even when its business fact did not change. Terms have many month slots;
    # their description-only calendar membership does not invalidate the prefix.
    recalculated = {
        row["business_id"]
        for row in responsibilities
        if (row.get("fact") or {}).get("kind") in POSITION_KINDS
    }
    ids = tuple(sorted(context.changed_business_ids | recalculated))
    facts = {key: context.get_fact(key) for key in ids}
    old_periods = {
        row["business_id"]: row["business_period"]
        for offset in range(0, len(ids), 500)
        for row in context.current_result_refs(ids[offset : offset + 500])
        if row["result_slot"] == "main"
    }
    old_holdings = {}
    for offset in range(0, len(ids), 500):
        for edge in context.current_dependencies(ids[offset : offset + 500]):
            if (
                edge["producer_kind"] == "business"
                and edge["read_mode"] == "result"
                and edge["relation_kind"] == "calculation"
                and edge["producer_public_slot"] == "main"
            ):
                old_holdings.setdefault(edge["consumer_business_id"], set()).add(
                    edge["producer_id"]
                )
    return ChangeIndex(facts, old_periods, old_holdings)


def choose_seed(terms_fact, context, changes):
    terms_id = terms_fact["business_id"]
    if context.result_requires_recalculation(terms_id, "main"):
        return None
    first = max(terms_fact["period"], context.read_context.start_period)
    affected = []
    for business_id, fact in changes.facts.items():
        if business_id == terms_id:
            return None
        if fact is not None:
            if fact["kind"] == "opening.package" and any(
                row["data"].get("terms_id") == terms_id and row["data"].get("type") == "investment"
                for row in fact["data"]["members"]
            ):
                return None
            if fact["kind"] in POSITION_KINDS and fact["data"]["terms_id"] == terms_id:
                affected.append(fact["period"])
            if fact["kind"] in {"investment.income", "investment.unit_change_basis"}:
                continue
        if terms_id in changes.old_holdings.get(business_id, ()):
            affected.append(changes.old_periods[business_id])
    cutoff = min(
        affected,
        default=(
            str(YearMonth.from_ordinal(YearMonth(context.previous_period_to).ordinal + 1))
            if context.evaluation_period_to > context.previous_period_to
            else context.evaluation_period_to
        ),
    )
    candidates = [
        row
        for row in context.current_result_refs((terms_id,))
        if row["result_slot"].startswith("position:")
        and first <= row["result_slot"].removeprefix("position:") < cutoff
        and not context.result_requires_recalculation(terms_id, row["result_slot"])
    ]
    if not candidates:
        return None
    reference = max(candidates, key=lambda row: row["result_slot"])
    ref = {"business_id": terms_id, "slot": reference["result_slot"]}
    previous = context.get_result(terms_id, ref["slot"])
    values = previous.target["values"]
    if values.get("inventory_revision") != REVISION or not all(
        "allocation_state" in lot for lot in values["lots"]
    ):
        return None
    adopted = context.get_result(terms_id, "main")
    if adopted is None or resolved_terms(terms_fact["data"]) != adopted.target["values"]:
        return None
    if not any(source == adopted.source for source in previous.basis["source_results"]):
        return None
    return Seed(ref["slot"].removeprefix("position:"), previous.source, values)
