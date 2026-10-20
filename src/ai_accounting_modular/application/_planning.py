"""Collect complete responsibilities and evaluate only affected result slots."""

from collections import defaultdict
from heapq import heapify, heappop, heappush

from ai_accounting_modular.contracts.public import (
    ContractError,
    resource_use_affects,
    validate_contract,
)

from ._descriptions import describe
from ._dirty import consumer as consumer_key
from ._dirty import numerical, propagate, signatures
from ._publications import overlaps
from ._relations import declare as declare_relations


def _membership(edge):
    return edge["producer_kind"], edge["producer_id"], edge["relation_kind"]


def collect_changes(changes, changed_objects, context, registry, handoffs):
    repo = context.read_context.repository("application")
    grouped, included = defaultdict(list), set()
    changed = {change.business_id: change for change in changes if change.changed}
    explicit, notified, previous_facts, references, loaded = set(), set(), {}, {}, set()
    for change in changes:
        if not change.changed and change.fact is None:
            continue
        mutation = change.mutation
        kind = mutation["fact"]["kind"] if "fact" in mutation else change.before["kind"]
        if change.business_id in handoffs:
            outgoing = handoffs[change.business_id]
            grouped[outgoing.old_owner].append(outgoing.outgoing())
        if not change.changed:
            mutation = {
                "op": "recalculate",
                "expected_seq": change.before["seq"],
                "fact": change.fact,
            }
        grouped[registry.owner(kind)].append({**mutation, "business_id": change.business_id})
        included.add(change.business_id)
        explicit.add(change.business_id)

    def include(ids):
        ids = sorted(set(ids) - included)
        heads = repo.heads(ids)
        if any(key not in heads or heads[key]["state"] != "active" for key in ids):
            raise ContractError("storage_corrupt", "当前依赖引用了不存在的有效业务。")
        facts = {
            fact["business_id"]: fact
            for fact in registry.export_heads(list(heads.values()), context.read_context)
        }
        for business_id in ids:
            head, fact = heads[business_id], facts[business_id]
            context.add_fact(fact)
            grouped[registry.owner(head["kind"])].append(
                {
                    "business_id": business_id,
                    "op": "recalculate",
                    "expected_seq": head["seq"],
                    "fact": fact,
                }
            )
            included.add(business_id)

    def notify(edges):
        values = tuple(edges)
        notified.update(consumer_key(edge) for edge in values if numerical(edge))
        include(edge["consumer_business_id"] for edge in values)

    def notify_relations(businesses, edges, old_edges):
        observers = []
        for business in businesses:
            business_id = business["business_id"]
            members = []
            for values in (old_edges, edges):
                members.append(
                    {
                        _membership(edge)
                        for edge in values
                        if edge["consumer_business_id"] == business_id
                        and edge["read_mode"] in ("fact", "result")
                        and edge["producer_kind"] in ("business", "object")
                    }
                )
            change = changed.get(business_id)
            if change is None and members[0] == members[1]:
                continue
            if business_id not in previous_facts:
                previous_facts[business_id] = (
                    registry.fact(change.before, context.read_context)
                    if change and change.before
                    else None
                )
            facts = [
                fact
                for fact in (business.get("fact"), previous_facts[business_id])
                if fact is not None
            ]
            periods = {fact["period"] for fact in facts}
            for kind, producer, relation in members[0] | members[1]:
                observers.extend(repo.observers(kind, producer, relation, periods))
            for fact in facts:
                observers.extend(repo.scope_observers(fact["kind"], {fact["period"]}))
        notify(observers)

    for object_id in changed_objects:
        for mode in ("fact", "result"):
            notify(repo.consumers("object", object_id, read_mode=mode))
    for business_id in changed:
        notify(repo.consumers("business", business_id, read_mode="fact"))

    if context.evaluation_period_to > context.previous_period_to:
        notify(repo.calendar_observers(context.previous_period_to, context.evaluation_period_to))

    while True:
        # Every changed fact shadows its old relations before any owner can
        # discover reverse membership. Descriptions never modify this directory.
        relations = declare_relations(grouped, context)
        participant_count = len(included)
        notifying = [row for rows in grouped.values() for row in rows if row["op"] != "handoff_out"]
        notify_relations(
            notifying,
            relations,
            repo.dependencies([row["business_id"] for row in notifying], direct_only=True),
        )
        if len(included) != participant_count:
            continue
        descriptions = describe(grouped, context, relations)
        missing = sorted(included - loaded)
        for offset in range(0, len(missing), 500):
            references.update(
                ((ref["business_id"], ref["result_slot"]), ref)
                for ref in context.current_result_refs(missing[offset : offset + 500])
            )
        loaded.update(missing)
        responsibilities = [
            row
            for _, description in descriptions
            for row in description["businesses"]
            if row["op"] != "handoff_out"
        ]
        complete = {
            (row["business_id"], slot)
            for row in responsibilities
            for slot in row["complete_result_slots"]
        }
        dependencies = [
            edge for _, description in descriptions for edge in description["dependencies"]
        ]
        publications = [
            item for _, description in descriptions for item in description["publications"]
        ]
        old_edges, old_publications = (
            repo.dependencies(sorted(included)),
            repo.publications(sorted(included)),
        )
        before, after = (
            signatures(old_edges, old_publications),
            signatures(dependencies, publications),
        )
        dirty = (
            notified
            | (complete - references.keys())
            | {key for key in complete if key[0] in explicit or before.get(key) != after.get(key)}
        )
        old_keys = references.keys() | before.keys()
        retired = old_keys - complete
        propagate(
            dirty, retired, old_edges, dependencies, old_publications, publications, repo, notify
        )
        if len(included) == participant_count:
            context.select_results(dirty & complete, references, complete)
            return descriptions
        # Notifications may introduce existing businesses (e.g. a tax reader
        # of a newly reached month). Complete their direct declarations before
        # describing the enlarged set. Each round must add a business; no edge
        # fixed point, owner priority, or arbitrary iteration limit is involved.


def evaluate(descriptions, context, ledger, correction_period):
    context.begin_candidate(descriptions, correction_period)
    owners, edges, reasons = {}, {}, {}
    publications = [item for _, description in descriptions for item in description["publications"]]
    dependencies = [edge for _, description in descriptions for edge in description["dependencies"]]
    uses = [edge for edge in dependencies if edge["read_mode"] == "use"]
    for owner, description in descriptions:
        for responsibility in description["businesses"]:
            for slot in responsibility["complete_result_slots"]:
                key = responsibility["business_id"], slot
                if key in owners:
                    raise ContractError("invalid_input", "同一结果槽被多个领域承担。")
                owners[key], edges[key] = owner, set()
    for _, description in descriptions:
        for edge in description["dependencies"]:
            consumer = edge["consumer_business_id"], edge["consumer_result_slot"]
            if consumer not in owners:
                raise ContractError("invalid_input", "领域依赖必须指向本单承担的完整结果槽。")
            if edge["producer_kind"] == "business" and edge["read_mode"] == "result":
                producer = edge["producer_id"], edge["producer_public_slot"]
                if producer in owners:
                    edges[consumer].add(producer)
                    reasons[consumer, producer] = "业务结果"
                elif edge["producer_id"] in context._slots:
                    raise ContractError("dependency_conflict", "本单退出的结果仍被另一业务采用。")
            elif edge["producer_kind"] == "aggregate":
                for publication in publications:
                    if overlaps(publication, edge):
                        producer = publication["business_id"], publication["result_slot"]
                        edges[consumer].add(producer)
                        reasons[consumer, producer] = (
                            f"{edge['producer_id']}[{edge['period_from']}..{edge['period_to']}]"
                        )
            elif edge["read_mode"] == "balance":
                if not any(
                    item["read_mode"] == "result"
                    and item["producer_kind"] == "business"
                    and item["producer_id"] == edge["producer_id"]
                    and (item["consumer_business_id"], item["consumer_result_slot"]) == consumer
                    for item in dependencies
                ):
                    raise ContractError("invalid_input", "余额依赖还须明确资源定义的生产结果槽。")
                for use in uses:
                    if resource_use_affects(use, edge):
                        producer = use["consumer_business_id"], use["consumer_result_slot"]
                        edges[consumer].add(producer)
                        reasons[consumer, producer] = "资源余额"
    plans = {}
    pending_edges = {
        key: producers & context.pending_slots
        for key, producers in edges.items()
        if key in context.pending_slots
    }
    for business_id, result_slot in ordered_slots(pending_edges, reasons):
        ledger.prepare_result(
            business_id, result_slot, context.read_context, correction_period=correction_period
        )
        with context.result_scope(((business_id, result_slot),)):
            plan = owners[business_id, result_slot].evaluate_slot(business_id, result_slot, context)
        validate_contract("business-facts.schema.json", "PostingPlan", plan)
        if (plan["business_id"], plan["result_slot"]) != (business_id, result_slot):
            raise ContractError("invalid_input", "领域返回了其他业务或其他槽的核算计划。")
        plans[business_id, result_slot] = plan
        context.add_plan(plan)
    result = tuple(
        (
            owner,
            {
                **description,
                "plans": [
                    plans[business["business_id"], slot]
                    for business in description["businesses"]
                    for slot in business["complete_result_slots"]
                    if (business["business_id"], slot) in plans
                ],
                "retained_results": context.retained_results(
                    row["business_id"]
                    for row in description["businesses"]
                    if row["op"] != "handoff_out"
                ),
            },
        )
        for owner, description in descriptions
    )
    context.complete_candidate()
    context.validate_occurrences(list(plans.values()))
    for _, description in descriptions:
        for edge in description["dependencies"]:
            if consumer_key(edge) not in context.pending_slots:
                continue
            if edge["read_mode"] in ("use", "balance"):
                continue
            if edge["producer_kind"] in ("scope", "calendar", "aggregate"):
                continue
            if edge["producer_kind"] == "object":
                found = context.get_object(edge["producer_id"])
            elif edge["read_mode"] == "result":
                found = context.get_result(edge["producer_id"], edge["producer_public_slot"])
            else:
                found = context.get_fact(edge["producer_id"])
            if found is None:
                raise ContractError("dependency_conflict", "声明的业务关联缺少当前有效来源。")
    for owner, plan in result:
        with context.result_scope(
            (business["business_id"], slot)
            for business in plan["businesses"]
            for slot in business["complete_result_slots"]
        ):
            owner.validate_candidate(plan, context)
    return result


def ordered_slots(edges, reasons):
    """A deterministic Kahn traversal; only actual pending slots are graph nodes."""
    remaining = {key: len(producers) for key, producers in edges.items()}
    consumers = defaultdict(set)
    for consumer, producers in edges.items():
        for producer in producers:
            consumers[producer].add(consumer)
    ready = [key for key, count in remaining.items() if count == 0]
    heapify(ready)
    ordered = []
    while ready:
        node = heappop(ready)
        ordered.append(node)
        del remaining[node]
        for consumer in consumers[node]:
            remaining[consumer] -= 1
            if remaining[consumer] == 0:
                heappush(ready, consumer)
    if remaining:
        node, path, positions = min(remaining), [], {}
        while node not in positions:
            positions[node] = len(path)
            path.append(node)
            node = min(producer for producer in edges[node] if producer in remaining)
        cycle = path[positions[node] :] + [node]
        pieces = [f"{cycle[0][0]}/{cycle[0][1]}"]
        for consumer, producer in zip(cycle[:-1], cycle[1:], strict=True):
            piece = f" --{reasons[consumer, producer]}→ {producer[0]}/{producer[1]}"
            if sum(map(len, pieces)) + len(piece) > 1800:
                pieces.append(f" …（循环共{len(cycle) - 1}个结果槽）")
                break
            pieces.append(piece)
        raise ContractError("dependency_conflict", "核算依赖循环：" + "".join(pieces))
    return tuple(ordered)
