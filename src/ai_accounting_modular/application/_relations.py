"""Direct fact membership is declared before result responsibilities are described."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract


def identity(edge):
    return tuple(
        edge[key]
        for key in ("consumer_business_id", "producer_kind", "producer_id", "relation_kind")
    )


def declare(grouped, context):
    relations, roots = {}, set()
    withdrawn = {
        change["business_id"]
        for changes in grouped.values()
        for change in changes
        if change["op"] == "withdraw"
    }
    for owner, changes in grouped.items():
        expected = {change["business_id"]: change for change in changes}
        roots.update(expected)
        for edge in owner.declare_relations(changes, context):
            validate_contract("business-facts.schema.json", "DirectRelation", edge)
            change = expected.get(edge["consumer_business_id"])
            if change is None or change["op"] in ("withdraw", "handoff_out"):
                raise ContractError(
                    "invalid_input", "直接关系只能声明本领域有效业务对业务或对象的引用。"
                )
            if edge["producer_kind"] == "business" and edge["producer_id"] in withdrawn:
                raise ContractError(
                    "dependency_conflict",
                    f"本单撤去的业务 {edge['producer_id']} 仍被当前有效业务引用；"
                    "请一并处理依赖方，或先修正其引用。",
                ).at_business(edge["consumer_business_id"])
            relations[identity(edge)] = dict(edge)
    result = tuple(relations[key] for key in sorted(relations))
    context.set_relations(roots, result)
    return result
