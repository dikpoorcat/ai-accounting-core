"""Current business prose, selected by exact formal adoption without reading Facts."""

from ai_accounting_modular.contracts.public import ContractError, QuerySelection

from ._business_labels import business_label, origin_fields, settlement_title
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


_STANDARD = frozenset(
    {
        "wage",
        "bonus",
        "withholding",
        "social",
        "housing",
        "employee_social",
        "employer_social",
        "employee_housing",
        "employer_housing",
        "vat",
        "surtax",
        "income_tax",
        "tax",
        "tax_credit",
        "tax_release",
        "bank_interest",
        "asset_consumption",
    }
)


def description_reference(content, label=None, *, component=None):
    """A current note may describe this precise subject, never its containing package."""
    label = label or business_label(content)
    if (
        label.code in _STANDARD
        or origin_fields(content)[0]["kind"] == "opening.package"
        or (component is not None and not (label.code == "labor" and component == "net_pay"))
    ):
        return None
    return content.reference


def common_text(values, fallback):
    """A list heading identifies its common meaning; individual prose stays in its members."""
    unique = {value or fallback for value in values}
    return next(iter(unique)) if len(unique) == 1 else fallback


def role_prefix(role):
    return (
        "冲正原业务 · "
        if role == "reversal"
        else "更正原业务 · "
        if role in {"replacement", "reclassification"}
        else ""
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
        if part.label.code != business_label(body).code:
            return ()
        reference = description_reference(body, part.label)
        return (reference,) if reference else ()
    references = []
    for allocation in part.scope["allocations"]:
        source = allocation["producer"]
        producer = cache.saved[source["result_id"], source["result_generation"]]
        reference = description_reference(producer, component=allocation["component"])
        if reference:
            references.append(reference)
    return tuple(references)


def part_display(body, part, cache, prose, role):
    prefix = role_prefix(role)
    fallback = prefix + part.label.title
    if role == "reversal":
        return fallback, ()
    refs = part_references(body, part, cache)
    scopes = tuple(sorted({scope for ref in refs if (scope := prose.scope(ref)) is not None}))
    text = common_text((prose.text(ref) for ref in refs), part.label.title)
    if text != part.label.title and part.phase in {"inflow", "outflow", "offset"}:
        text = settlement_title(text, part.label.code, part.phase)
    return prefix + text, scopes
