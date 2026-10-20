"""Direct fact membership is declared before result responsibilities are described."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract


def identity(edge):
    return tuple(
        edge[key]
        for key in ("consumer_business_id", "producer_kind", "producer_id", "relation_kind")
    )


def declare(grouped, context):
    relations, roots = {}, set()
    for owner, changes in grouped.items():
        expected = {change["business_id"]: change for change in changes}
        roots.update(expected)
        for edge in owner.declare_relations(changes, context):
            validate_contract("business-facts.schema.json", "Dependency", edge)
            change = expected.get(edge["consumer_business_id"])
            if (
                change is None
                or change["op"] in ("withdraw", "handoff_out")
                or edge["producer_kind"] not in ("business", "object")
                or edge["read_mode"] not in ("fact", "result")
            ):
                raise ContractError(
                    "invalid_input", "直接关系只能声明本领域有效业务对业务或对象的引用。"
                )
            relations[identity(edge)] = dict(edge)
    result = tuple(relations[key] for key in sorted(relations))
    context.set_relations(roots, result)
    return result


def validate_description(description, relations):
    owners = {
        row["business_id"]
        for row in description["businesses"]
        if row["op"] not in ("withdraw", "handoff_out")
    }
    final = {
        identity(edge)
        for edge in description["dependencies"]
        if edge["producer_kind"] in ("business", "object")
        and edge["read_mode"] in ("fact", "result")
    }
    missing = [
        identity(edge)
        for edge in relations
        if edge["consumer_business_id"] in owners and identity(edge) not in final
    ]
    if missing:
        consumer, kind, producer, relation = min(missing)
        raise ContractError(
            "invalid_input",
            f"最终描述遗漏已声明的直接关系：{consumer} → {kind}:{producer}（{relation}）。",
        )
