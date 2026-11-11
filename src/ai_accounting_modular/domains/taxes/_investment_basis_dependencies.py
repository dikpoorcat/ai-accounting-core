"""Bounded qualification discovery shares the existing relation directory."""

from . import _investment_basis as basis
from ._common import month_end


def _terms(fact):
    if fact is None:
        return ()
    data = fact["data"]
    if fact["kind"] == "opening.package":
        return tuple(
            row["data"]["terms_id"]
            for row in data["members"]
            if row["data"].get("type") == "investment"
        )
    return (data["terms_id"],) if "terms_id" in data else ()


def terms_in_scope(context, kinds, start, end, previous=None):
    identities = {
        identity for fact in context.facts_in_scope(kinds, start, end) for identity in _terms(fact)
    }
    if previous is not None:
        result = context.get_result(*previous)
        if result is not None:
            for row in result.target["values"].get("positions", ()):
                if "unclassified_terms_id" in row:
                    identities.add(row["unclassified_terms_id"])
                identities.update(
                    lot["terms_id"] for lot in row.get("eit_lots", ()) if "terms_id" in lot
                )
    changed = []
    for identity in context.changed_business_ids:
        fact = context.get_fact(identity)
        if fact is None:
            changed.append(identity)
        elif fact["kind"] in kinds and fact["period"] <= end:
            changed.append(identity)
            identities.update(_terms(fact))
    for offset in range(0, len(changed), 500):
        for edge in context.current_dependencies(changed[offset : offset + 500]):
            if edge["producer_kind"] != "business" or edge["relation_kind"] != "calculation":
                continue
            producer = context.get_fact(edge["producer_id"])
            if producer is not None and producer["kind"] == "investment.terms":
                identities.add(producer["business_id"])
    return identities


def dependencies(fact, context, kinds, start, end, *, consumer="main", previous=None):
    terms_ids = terms_in_scope(context, kinds, start, end, previous)
    result = []
    for identity, candidates in basis.candidates(context, terms_ids).items():
        common = {
            "consumer_business_id": fact["business_id"],
            "consumer_result_slot": consumer,
            "producer_kind": "business",
            "producer_public_slot": "main",
        }
        result.append(
            {
                **common,
                "producer_id": identity,
                "read_mode": "collection",
                "relation_kind": "reference",
                "period_from": "0001-01",
                "period_to": "9999-12",
                "notify_effect": "description",
            }
        )
        result.extend(
            {
                **common,
                "producer_id": candidate["business_id"],
                "read_mode": "result",
                "relation_kind": "calculation",
            }
            for candidate in candidates
            if candidate["data"]["applicable_from"] <= month_end(end)
            and candidate["data"].get("applicable_to", "9999-12-31") >= start + "-01"
        )
    return result
