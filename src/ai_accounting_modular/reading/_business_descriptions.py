"""Separate current short purposes and complete notes under exact formal adoption."""

from dataclasses import dataclass

from ai_accounting_modular.contracts.public import ContractError, QuerySelection, content_digest
from ai_accounting_modular.ledger.public import AdoptedContent, AdoptedOriginProjection

from ._business_labels import (
    BusinessLabel,
    business_label,
    origin_fields,
    remuneration_identity,
    settlement_title,
)
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


def purpose_reference(content, label=None, *, component=None):
    """Only eligible subjects may replace their standard names with a short purpose."""
    original = business_label(content)
    label = label or original
    kind = origin_fields(content)[0]["kind"]
    if (
        label.code in _STANDARD
        or label.code != original.code
        or kind == "opening.package"
        or kind.startswith("taxes.")
        or (component is not None and not (label.code == "labor" and component == "net_pay"))
    ):
        return None
    return content.reference


def note_reference(content):
    return None if origin_fields(content)[0]["kind"] == "opening.package" else content.reference


@dataclass(frozen=True)
class DescriptionSubject:
    content: AdoptedContent | AdoptedOriginProjection
    label: BusinessLabel
    component: str | None = None
    source_slot: str | None = None


def obligation_subject(content, resource, *, label=None, component=None):
    if component is None and remuneration_identity(content) is not None:
        component = resource.reference["slot"]
    return DescriptionSubject(
        content, label or business_label(content, resource), component, resource.reference["slot"]
    )


def note_scope(subject):
    """A precise obligation may be only one part of its business's complete prose."""
    content = subject.content
    if note_reference(content) is None:
        return None
    if subject.component is not None or subject.label.code != business_label(content).code:
        return "whole_business"
    if subject.source_slot is None:
        return "selected"
    target, basis = origin_fields(content)
    kind = target["kind"]
    data = basis.get("input", {})
    bundled = (
        kind in {"operations.deposit", "taxes.assessment", "taxes.actual_declaration"}
        or "cooperation_id" in data
        or kind == "investment.acquire"
        and data.get("separate_declared_income_fen", "0") != "0"
        or kind == "investment.dispose"
        and "maturity_income" in data
    )
    return "whole_business" if subject.source_slot != "primary" or bundled else "selected"


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
        self.current, self.notes = set(), {}
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
                if note["state"] == "current":
                    self.notes[note["scope"]] = note

    def scope(self, reference):
        key = reference["result_id"], reference["generation"]
        return "business:" + reference["business_id"] if key in self.current else None

    def text(self, reference):
        note = self.notes.get(self.scope(reference), {})
        text = note.get("text")
        return text if text and text.strip() else None

    def purpose(self, subject):
        reference = purpose_reference(subject.content, subject.label, component=subject.component)
        note = self.notes.get(self.scope(reference), {}) if reference else {}
        purpose = note.get("purpose")
        return purpose if purpose and purpose.strip() else None

    def source_text(self, source):
        return self.text(source_reference(source))

    def digest(self, subjects=(), *, notes=()):
        """Bind lazy prose to the parent snapshot using only its effective display inputs."""
        values = {}
        for subject in subjects:
            content = subject.content
            reference = note_reference(content)
            scope = self.scope(reference) if reference else None
            if scope is not None:
                purpose = self.purpose(subject)
                text = self.text(reference)
                if purpose is None and text is None:
                    continue
                identity = [
                    scope,
                    reference["result_id"],
                    reference["generation"],
                    subject.label.code,
                    subject.component,
                    subject.source_slot,
                ]
                values[content_digest(identity)] = {
                    "purpose": purpose,
                    "text": text,
                    "scope": note_scope(subject),
                }
        for reference in notes:
            scope = self.scope(reference)
            text = self.text(reference)
            if scope is not None and text is not None:
                identity = [scope, reference["result_id"], reference["generation"]]
                values[content_digest(identity)] = {"text": text}
        return content_digest(values)


def part_subjects(body, part):
    if not part.scope["allocations"]:
        if body.target["kind"] == "people.remuneration" and part.phase == "deductions":
            return ()
        if part.label.code != business_label(body).code:
            return ()
        return (DescriptionSubject(body, part.label),)
    return part.subjects


def part_references(body, part):
    return tuple(
        reference
        for subject in (*part_subjects(body, part), DescriptionSubject(body, part.label))
        if (reference := note_reference(subject.content)) is not None
    )


@dataclass(frozen=True)
class PartDisplay:
    purpose: str
    description: str
    scopes: tuple[str, ...]
    notes_digest: str
    has_hidden_note: bool


def part_display(body, part, prose, role):
    prefix = role_prefix(role)
    fallback = prefix + part.label.title
    if role == "reversal":
        return PartDisplay(fallback, fallback, (), content_digest({}), False)
    subjects = part_subjects(body, part)
    refs = part_references(body, part)
    scopes = tuple(sorted({scope for ref in refs if (scope := prose.scope(ref)) is not None}))
    text = common_text(
        (prose.purpose(subject) for subject in subjects),
        part.label.title,
    )
    if text != part.label.title and part.phase in {"inflow", "outflow", "offset"}:
        text = settlement_title(text, part.label.code, part.phase)
    short = prefix + text
    selected = {
        (
            subject.content.reference["result_id"],
            subject.content.reference["generation"],
        ): subject.content
        for subject in subjects
        if note_scope(subject) == "selected"
    }
    detail = None
    if len(selected) == 1 and all(note_scope(subject) == "selected" for subject in subjects):
        detail = prose.text(next(iter(selected.values())).reference)
    own = note_reference(body)
    visible = set(selected) if detail else set()
    hidden = any(
        prose.text(ref) and (ref["result_id"], ref["generation"]) not in visible for ref in refs
    )
    return PartDisplay(
        short, detail or short, scopes, prose.digest(subjects, notes=(own,) if own else ()), hidden
    )
