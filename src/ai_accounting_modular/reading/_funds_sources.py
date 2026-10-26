"""Resolve only this month's actual funds and allocation sources through formal ledger ports."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    QuerySelection,
    ResourceDefinition,
)
from ai_accounting_modular.ledger.public import (
    AdoptedContent,
    AdoptedDirectoryEntry,
    AdoptedOriginProjection,
)

from ._business_descriptions import Descriptions, common_text, description_reference, merged
from ._business_labels import (
    BusinessLabel,
    business_label,
    origin_fields,
    settlement_label,
    settlement_title,
)


@dataclass(frozen=True)
class FundSources:
    entries: dict[str, AdoptedDirectoryEntry]
    contents: dict[str, AdoptedOriginProjection | AdoptedContent]
    obligations: dict[tuple[str, str], ResourceDefinition]
    descriptions: Descriptions

    def titles(self, view):
        """Resolve source prose once for the standard name, list heading and full purpose."""
        groups = {}
        for part in view["allocations"]:
            resource = self.obligations[part.source["business_id"], part.source["slot"]]
            body = self.contents[resource.source["result_id"]]
            label = business_label(body, resource)
            reference = description_reference(body, label)
            text = self.descriptions.text(reference) if reference else None
            groups.setdefault(label, []).append(text)
        content, direction = (
            self.contents[view["source"]["result_id"]],
            view["movement"]["direction"],
        )
        label = settlement_label(content, tuple(groups), direction).title
        if not groups:
            text = self.descriptions.source_text(view["source"]) or label
            if text != label and origin_fields(content)[0]["kind"] == "money.movement":
                text = settlement_title(text, business_label(content).code, direction)
            return label, text, text
        brief, full = [], []
        for meaning, texts in groups.items():
            brief.append(BusinessLabel(meaning.code, common_text(texts, meaning.title)))
            full.append(
                BusinessLabel(meaning.code, merged(text or meaning.title for text in texts))
            )
        return (
            label,
            settlement_label(content, brief, direction).title,
            settlement_label(content, full, direction).title,
        )


def load(reader, activity, scope, read):
    refs = {
        (part.source["business_id"], part.source["slot"]): part.source
        for view in activity.movements
        for part in view["allocations"]
    }
    obligations, requested = {}, list(refs.values())
    selection = QuerySelection(scope["view"], scope["as_of"])
    for start in range(0, len(requested), 500):
        for resource in reader.ledger.resource_definitions(
            requested[start : start + 500], "obligation", read, selection=selection
        ):
            obligations[(resource.reference["business_id"], resource.reference["slot"])] = resource
    if obligations.keys() != refs.keys():
        raise ContractError("storage_corrupt", "实际资金分项缺少本月所选的正式来源义务。")
    sources = {view["source"]["result_id"]: view["source"] for view in activity.movements}
    sources.update(
        (resource.source["result_id"], resource.source) for resource in obligations.values()
    )
    ids = sorted({source["business_id"] for source in sources.values()})
    entries = {}
    for start in range(0, len(ids), 500):
        cursor = None
        while True:
            page = reader.ledger.query_adopted_directory(
                scope,
                (),
                PageQuery(500, cursor),
                read,
                business_ids=tuple(ids[start : start + 500]),
            )
            entries.update(
                (entry.source["result_id"], entry)
                for entry in page.items
                if entry.source["result_id"] in sources
            )
            cursor = page.next_cursor
            if cursor is None:
                break
    if entries.keys() != sources.keys() or any(
        entries[key].source != value for key, value in sources.items()
    ):
        raise ContractError("storage_corrupt", "资金业务或分项缺少所选月份的精确正式来源。")
    contents, requested = {}, [entry.reference for entry in entries.values()]
    for start in range(0, len(requested), 500):
        for content in reader.ledger.get_obligation_origins(requested[start : start + 500], read):
            entry = entries.get(content.reference["result_id"])
            if (
                entry is None
                or content.reference != entry.reference
                or content.source != entry.source
                or origin_fields(content)[0]["kind"] != entry.business_kind
            ):
                raise ContractError("storage_corrupt", "资金用途正文与所选精确正式结果不一致。")
            contents[content.reference["result_id"]] = content
    if contents.keys() != entries.keys():
        raise ContractError("storage_corrupt", "资金用途所需的正式正文不完整。")
    references = []
    for view in activity.movements:
        for part in view["allocations"]:
            resource = obligations[part.source["business_id"], part.source["slot"]]
            body = contents[resource.source["result_id"]]
            reference = description_reference(body, business_label(body, resource))
            if reference:
                references.append(reference)
        if not view["allocations"]:
            body = contents[view["source"]["result_id"]]
            reference = description_reference(body)
            if reference:
                references.append(reference)
    prose = Descriptions(reader, references, read)
    return FundSources(entries, contents, obligations, prose)
