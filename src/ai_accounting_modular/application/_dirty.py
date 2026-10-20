"""Current slot changes propagate through declared inputs, never whole businesses."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import canonical_json, resource_use_affects

from ._publications import overlaps
from ._resources import normalize as normalize_resource_dependency


def consumer(edge):
    return edge["consumer_business_id"], edge["consumer_result_slot"]


def publication_slot(value):
    return value["business_id"], value["result_slot"]


def numerical(edge):
    return edge.get("notify_effect", "result") == "result"


def signatures(dependencies, publications):
    result = defaultdict(set)
    for edge in dependencies:
        if numerical(edge):
            value = normalize_resource_dependency(dict(edge))
            value.pop("notify_effect", None)
            if not value.get("excluded_result_slots"):
                value.pop("excluded_result_slots", None)
            result[consumer(edge)].add(("dependency", canonical_json(value)))
    for value in publications:
        result[publication_slot(value)].add(("publication", canonical_json(value)))
    return result


def propagate(
    dirty, retired, old_edges, dependencies, old_publications, publications, repo, notify
):
    """One declaration snapshot; a later round must add a business, not guess new edges."""
    by_result = defaultdict(list)
    for edge in dependencies:
        if edge["producer_kind"] == "business" and edge["read_mode"] == "result":
            by_result[edge["producer_id"], edge["producer_public_slot"]].append(edge)
    by_user, by_publisher = defaultdict(list), defaultdict(list)
    for edge in (*old_edges, *dependencies):
        if edge["read_mode"] == "use":
            by_user[consumer(edge)].append(edge)
    for value in (*old_publications, *publications):
        by_publisher[publication_slot(value)].append(value)
    balances = [edge for edge in dependencies if edge["read_mode"] == "balance"]
    aggregates = [edge for edge in dependencies if edge["producer_kind"] == "aggregate"]
    pending, visited = dirty | retired, set()
    while pending:
        visited.update(pending)
        notices = list(repo.result_consumers(sorted(pending)))
        notices.extend(edge for key in sorted(pending) for edge in by_result[key])
        uses = [edge for key in sorted(pending) for edge in by_user[key]]
        published = [value for key in sorted(pending) for value in by_publisher[key]]
        if uses:
            notices.extend(repo.resource_observers(uses))
            notices.extend(
                edge for edge in balances if any(resource_use_affects(use, edge) for use in uses)
            )
        if published:
            notices.extend(repo.aggregate_observers(published))
            notices.extend(
                edge for edge in aggregates if any(overlaps(value, edge) for value in published)
            )
        notify(notices)
        targets = {consumer(edge) for edge in notices if numerical(edge)}
        dirty.update(targets)
        pending = targets - visited
