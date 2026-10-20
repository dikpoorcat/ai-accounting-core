"""Collect owner responsibilities, find explicit dependants, evaluate complete slots."""

from collections import defaultdict, deque

from ai_accounting_modular.contracts.public import ContractError, validate_contract


def collect_changes(changes, changed_objects, context, registry):
    repo = context.read_context.repository("application")
    grouped, included = defaultdict(list), set()
    queue = deque(("object", object_id) for object_id in changed_objects)
    for change in changes:
        if not change.changed:
            continue
        mutation = change.mutation
        kind = mutation["fact"]["kind"] if "fact" in mutation else change.before["kind"]
        grouped[registry.owner(kind)].append({**mutation, "business_id": change.business_id})
        included.add(change.business_id)
        queue.append(("business", change.business_id))
    visited = set()
    while queue:
        producer = queue.popleft()
        if producer in visited:
            continue
        visited.add(producer)
        ids = [key for key in repo.consumers(*producer) if key not in included]
        heads = repo.heads(ids)
        for business_id in ids:
            head = heads.get(business_id)
            if head is None or head["state"] != "active":
                raise ContractError("storage_corrupt", "当前依赖引用了不存在的有效业务。")
            fact = registry.fact(head, context.read_context)
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
    descriptions = []
    for owner, mutations in grouped.items():
        description = owner.describe_change(mutations, context)
        validate_contract("business-facts.schema.json", "ChangeDescription", description)
        expected = {item["business_id"] for item in mutations}
        actual = [item["business_id"] for item in description["businesses"]]
        if set(actual) != expected or len(actual) != len(expected):
            raise ContractError("invalid_input", "领域未完整承担本单业务身份，或重复声明责任。")
        for responsibility in description["businesses"]:
            source = next(
                item for item in mutations if item["business_id"] == responsibility["business_id"]
            )
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
    return descriptions


def evaluate(descriptions, context, ledger, correction_period):
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
    return tuple(
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
