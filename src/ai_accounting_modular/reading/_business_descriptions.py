"""Current business prose, selected by exact formal adoption without reading Facts."""

from ai_accounting_modular.contracts.public import ContractError, QuerySelection

from ._business_labels import origin_fields
from ._funds_items import name_key


def merged(values):
    unique = {value for value in values if value}
    if len(unique) <= 1:
        return next(iter(unique), "")
    return "；".join(sorted(unique, key=name_key))


def source_reference(source):
    return {
        "business_id": source["business_id"],
        "result_id": source["result_id"],
        "generation": source["result_generation"],
        "result_slot": source["result_slot"],
    }


def allows_component(content, *, category=None, component=None):
    """Whole payroll, opening and tax prose never describes their individual obligations."""
    kind = origin_fields(content)[0]["kind"]
    return not (
        kind.startswith(("people.", "taxes.", "opening."))
        or category in {"social", "housing", "withholding", "tax"}
        or component is not None
    )


class Descriptions:
    def __init__(self, reader, references, read):
        requested = {}
        for ref in references:
            if ref["result_slot"] == "main":
                requested[ref["result_id"], ref["generation"]] = ref
        ids = sorted({ref["business_id"] for ref in requested.values()})
        self.current, self.texts = set(), {}
        for start in range(0, len(ids), 500):
            refs = reader.ledger.get_result_refs(
                ids[start : start + 500], read, selection=QuerySelection("current")
            )
            for ref in refs:
                key = ref["result_id"], ref["generation"]
                if key in requested:
                    expected = requested[key]
                    if any(
                        ref[field] != expected[field] for field in ("business_id", "result_slot")
                    ):
                        raise ContractError("storage_corrupt", "事项说明的正式业务身份不一致。")
                    self.current.add(key)
        scopes = tuple(
            sorted({"business:" + requested[key]["business_id"] for key in self.current})
        )
        self.scopes = scopes
        for start in range(0, len(scopes), 500):
            batch = scopes[start : start + 500]
            expected_scopes = set(batch)
            for note in reader.records.current_notes(batch, read):
                if note["scope"] not in expected_scopes:
                    raise ContractError("storage_corrupt", "事项说明不属于请求的业务范围。")
                if (
                    note["state"] == "current"
                    and isinstance(note["text"], str)
                    and note["text"].strip()
                ):
                    self.texts[note["scope"]] = note["text"]

    def scope(self, reference):
        key = reference["result_id"], reference["generation"]
        return "business:" + reference["business_id"] if key in self.current else None

    def text(self, reference):
        return self.texts.get(self.scope(reference))

    def source_text(self, source):
        return self.text(source_reference(source))


def part_references(body, part, cache):
    if not part.scope["allocations"]:
        return (body.reference,)
    references = []
    for allocation in part.scope["allocations"]:
        source = allocation["producer"]
        producer = cache.saved[source["result_id"], source["result_generation"]]
        if allows_component(producer, component=allocation["component"]):
            references.append(producer.reference)
    return tuple(references)


def part_display(body, part, cache, prose, role):
    prefix = (
        "冲正原业务 · "
        if role == "reversal"
        else "更正原业务 · "
        if role in {"replacement", "reclassification"}
        else ""
    )
    fallback = prefix + part.label.title
    if role == "reversal":
        return fallback, ()
    refs = part_references(body, part, cache)
    scopes = tuple(sorted({scope for ref in refs if (scope := prose.scope(ref)) is not None}))
    texts = [prefix + text if (text := prose.text(ref)) else fallback for ref in refs]
    return merged(texts) or fallback, scopes
