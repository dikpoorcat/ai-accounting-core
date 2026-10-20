"""Declared result collections, without amounts or accounting formulas."""

from ai_accounting_modular.contracts.public import ContractError


def normalize(publications, responsibilities):
    slots = {
        (item["business_id"], slot)
        for item in responsibilities
        for slot in item["complete_result_slots"]
    }
    indexed = {}
    for value in publications:
        publication = dict(value)
        key = publication["business_id"], publication["result_slot"], publication["collection"]
        if key[:2] not in slots:
            raise ContractError("invalid_input", "集合发布必须属于本领域实际承担的完整结果槽。")
        if publication["period_from"] > publication["period_to"]:
            raise ContractError("invalid_input", "集合发布起月不能晚于末月。")
        if key in indexed:
            previous = indexed[key]
            publication["period_from"] = min(previous["period_from"], publication["period_from"])
            publication["period_to"] = max(previous["period_to"], publication["period_to"])
        indexed[key] = publication
    return [indexed[key] for key in sorted(indexed)]


def overlaps(publication, dependency):
    return (
        publication["collection"] == dependency["producer_id"]
        and publication["period_from"] <= dependency["period_to"]
        and dependency["period_from"] <= publication["period_to"]
    )
