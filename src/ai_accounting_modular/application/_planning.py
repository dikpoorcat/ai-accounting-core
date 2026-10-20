"""Collect owner responsibilities, find explicit dependants, evaluate complete slots."""

from collections import defaultdict, deque

from ai_accounting_modular.contracts.public import ContractError, validate_contract


def _membership(edge):
    return edge["producer_kind"], edge["producer_id"], edge["relation_kind"]


def _dependencies(edges, responsibilities):
    """One dependency identity has one covering notification range."""
    indexed = {}
    identity_fields = (
        "consumer_business_id",
        "consumer_result_slot",
        "producer_kind",
        "producer_id",
        "producer_public_slot",
        "read_mode",
        "relation_kind",
    )
    for value in edges:
        edge = dict(value)
        if edge["consumer_business_id"] not in responsibilities:
            raise ContractError("invalid_input", "领域不能声明其他业务的依赖责任。")
        start, end = edge.get("period_from"), edge.get("period_to")
        if start and end and start > end:
            raise ContractError("invalid_input", "依赖范围起月不能晚于末月。")
        key = tuple(edge[field] for field in identity_fields)
        previous = indexed.get(key)
        if previous is not None:
            for field, choose in (("period_from", min), ("period_to", max)):
                bounds = previous.get(field), edge.get(field)
                if None in bounds:
                    edge.pop(field, None)
                else:
                    edge[field] = choose(bounds)
        indexed[key] = edge
    return [indexed[key] for key in sorted(indexed)]


def collect_changes(changes, changed_objects, context, registry):
    repo = context.read_context.repository("application")
    grouped, included = defaultdict(list), set()
    queue = deque(("object", object_id) for object_id in changed_objects)
    changed = {change.business_id: change for change in changes if change.changed}
    for change in changes:
        if not change.changed and change.fact is None:
            continue
        mutation = change.mutation
        kind = mutation["fact"]["kind"] if "fact" in mutation else change.before["kind"]
        if not change.changed:
            mutation = {
                "op": "recalculate",
                "expected_seq": change.before["seq"],
                "fact": change.fact,
            }
        grouped[registry.owner(kind)].append({**mutation, "business_id": change.business_id})
        included.add(change.business_id)
        queue.append(("business", change.business_id))
    visited, descriptions = set(), []

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
            queue.append(("business", business_id))

    if context.evaluation_period_to > context.previous_period_to:
        include(repo.calendar_observers(context.previous_period_to, context.evaluation_period_to))

    while queue or grouped:
        while queue:
            producer = queue.popleft()
            if producer not in visited:
                visited.add(producer)
                include(repo.consumers(*producer))
        current, grouped = grouped, defaultdict(list)
        for owner, mutations in current.items():
            description = owner.describe_change(mutations, context)
            validate_contract("business-facts.schema.json", "ChangeDescription", description)
            expected = {item["business_id"]: item for item in mutations}
            description["dependencies"] = _dependencies(description["dependencies"], expected)
            actual = [item["business_id"] for item in description["businesses"]]
            if set(actual) != expected.keys() or len(actual) != len(expected):
                raise ContractError("invalid_input", "领域未完整承担本单业务身份，或重复声明责任。")
            for responsibility in description["businesses"]:
                source = expected[responsibility["business_id"]]
                if any(responsibility[key] != source[key] for key in ("op", "expected_seq")):
                    raise ContractError("invalid_input", "领域不得改写应用已核对的变更前提。")
                if responsibility.get("fact") != source.get("fact"):
                    raise ContractError("invalid_input", "领域责任必须使用本单完整正确事实。")
                context.declare_slots(responsibility)
            if description["issues"]:
                raise ContractError(
                    "needs_information",
                    "本单业务尚有会影响核算的资料缺项。",
                    fact_issues=description["issues"],
                )
            descriptions.append((owner, description))
            context.add_description(description)
            old_edges = repo.dependencies(expected)
            for business in description["businesses"]:
                business_id = business["business_id"]
                before = [
                    edge
                    for edge in old_edges
                    if edge["consumer_business_id"] == business_id
                    and edge["read_mode"] != "collection"
                ]
                after = [
                    edge
                    for edge in description["dependencies"]
                    if edge["consumer_business_id"] == business_id
                    and edge["read_mode"] != "collection"
                ]
                old_members = {_membership(edge) for edge in before}
                new_members = {_membership(edge) for edge in after}
                change = changed.get(business_id)
                if change is None and old_members == new_members:
                    continue
                facts = [
                    value
                    for value in (
                        business.get("fact"),
                        registry.fact(change.before, context.read_context)
                        if change and change.before
                        else None,
                    )
                    if value is not None
                ]
                periods = {fact["period"] for fact in facts}
                observers = set()
                for producer_kind, producer_id, relation_kind in old_members | new_members:
                    observers.update(
                        repo.observers(producer_kind, producer_id, relation_kind, periods)
                    )
                for fact in facts:
                    observers.update(repo.scope_observers(fact["kind"], {fact["period"]}))
                include(observers)
    return descriptions


def evaluate(descriptions, context, ledger, correction_period):
    context.begin_candidate(descriptions, correction_period)
    owners, edges = {}, {}
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
                elif edge["producer_id"] in context._slots:
                    raise ContractError("dependency_conflict", "本单退出的结果仍被另一业务采用。")
    plans = {}
    while len(plans) < len(owners):
        ready = sorted(key for key in owners if key not in plans and edges[key] <= plans.keys())
        if not ready:
            raise ContractError("dependency_conflict", "本单核算依赖形成循环，不能拆开发布。")
        for business_id, result_slot in ready:
            ledger.prepare_result(
                business_id, result_slot, context.read_context, correction_period=correction_period
            )
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
                ],
            },
        )
        for owner, description in descriptions
    )
    context.complete_candidate()
    context.validate_occurrences(list(plans.values()))
    for _, description in descriptions:
        for edge in description["dependencies"]:
            if edge["producer_kind"] in ("scope", "calendar"):
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
        owner.validate_candidate(plan, context)
    return result
