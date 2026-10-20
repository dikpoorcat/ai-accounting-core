"""Normalize and verify complete owner declarations before selecting any plans."""

from ai_accounting_modular.contracts.public import ContractError, validate_contract

from ._dirty import numerical
from ._publications import normalize as normalize_publications
from ._resources import normalize as normalize_resource_dependency


def _dependencies(edges, responsibilities):
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
        normalize_resource_dependency(edge, previous)
        if previous is not None:
            if edge["read_mode"] == "collection":
                edge["notify_effect"] = (
                    "result" if numerical(edge) or numerical(previous) else "description"
                )
            for field, choose in (("period_from", min), ("period_to", max)):
                bounds = previous.get(field), edge.get(field)
                if None in bounds:
                    edge.pop(field, None)
                else:
                    edge[field] = choose(bounds)
        normalize_resource_dependency(edge)
        indexed[key] = edge
    return [indexed[key] for key in sorted(indexed)]


def describe(grouped, context):
    descriptions = []
    context.reset_descriptions()
    for owner, mutations in tuple((owner, tuple(rows)) for owner, rows in grouped.items()):
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
        description["publications"] = normalize_publications(
            description["publications"], description["businesses"]
        )
        if description["issues"]:
            raise ContractError(
                "needs_information",
                "本单业务尚有会影响核算的资料缺项。",
                fact_issues=description["issues"],
            )
        descriptions.append((owner, description))
    return descriptions
