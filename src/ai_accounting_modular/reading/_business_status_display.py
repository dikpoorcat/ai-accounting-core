"""Sparse owner-facing names and amounts from the selected formal business meanings."""

from . import _payroll_journals
from ._brief_activity_amounts import amount
from ._brief_activity_parts import business_parties
from ._brief_activity_sources import identities
from ._brief_vouchers import assets
from ._business_descriptions import DescriptionSubject, merged, note_reference, role_prefix
from ._business_labels import (
    business_fund_accounts,
    business_label,
    business_matter,
    settlement_title,
)


def business_kind(selected):
    """Project the selected adopted remuneration into its existing dashboard category."""
    content = selected.content
    if content is None or content.target["kind"] != "people.remuneration":
        return selected.kind
    remuneration_type = content.target["values"]["remuneration_type"]
    if remuneration_type == "labor":
        destination = content.basis["input"].get("destination", {})
        return "labor_project_cost" if destination.get("type") == "project" else "labor"
    return {"wage": "payroll", "bonus": "annual_bonus"}[remuneration_type]


def profile(identifier, name, obj=None, *, note=None):
    data = obj.get("data", {}) if obj else {}
    return {
        "entity_id": identifier,
        "values": {
            "display_name": name,
            "display_number": data.get("code"),
            "account_number": data.get("account_number"),
            "purpose": None,
            "note": note,
            "note_scope": None,
            "employment_start": None,
            "employment_end": None,
            "employment_status": None,
            "employment_state": None,
            "active": data.get("active"),
            "category_label": None,
        },
    }


def description_subjects(selected, settlements):
    entries = (*selected.range.get("allocations", ()), *selected.range.get("obligations", ()))
    if not entries:
        if (
            selected.content is not None
            and selected.content.target["kind"] == "people.remuneration"
            and _payroll_journals.phase_from_scope(selected.range) == "deductions"
        ):
            return ()
        if selected.content is not None and selected.range.get("purpose") not in {
            None,
            business_label(selected.content).code,
        }:
            return ()
        return (
            ()
            if selected.content is None
            else (DescriptionSubject(selected.content, business_label(selected.content)),)
        )
    values = []
    for entry in entries:
        source = entry["producer"]
        key = source["result_id"], (entry["source"]["business_id"], entry["source"]["slot"])
        body, meaning = settlements.meanings[key]
        values.append(
            DescriptionSubject(body, meaning.label, meaning.component, entry["source"]["slot"])
        )
    return tuple(values)


def whole_note_reference(selected):
    """The business's own note remains independent of its allocation sources."""
    content = selected.content
    return note_reference(content) if content is not None else None


def description_references(selected, settlements):
    """Only the selected subjects and their real settlement meanings consume current prose."""
    if selected.range.get("voucher_role") == "reversal":
        return ()
    subjects = description_subjects(selected, settlements)
    refs = [ref for subject in subjects if (ref := note_reference(subject.content)) is not None]
    own = whole_note_reference(selected)
    if own:
        refs.append(own)
    refs.extend(
        ref
        for body, meaning in settlements.meanings.values()
        if (ref := note_reference(body)) is not None
    )
    return tuple(refs)


def profiles(reader, selected, settlements, events, read, *, descriptions=None):
    counterparties, employees, cards, accounts = set(), set(), set(), set()
    content = selected.content
    if content is not None and content.target["kind"] != "system.withdrawal":
        if selected.detail is not None and selected.range.get("allocations"):
            for allocation in selected.range["allocations"]:
                if allocation.get("recipient_id"):
                    counterparties.add(allocation["recipient_id"])
                if allocation.get("person_id"):
                    employees.add(allocation["person_id"])
        else:
            counterparties.update(business_parties(content, selected.cache))
        cards.update(item["asset_id"] for item in assets(content))
        matter = business_matter(content)
        cards.update(
            value for key, value in matter.items() if key in {"asset_id", "project_id", "fund_id"}
        )
        if content.target["kind"] in {"people.remuneration", "people.remuneration_tax"}:
            employees.add(content.target["values"]["person_id"])
    allowed = [source for sources in settlements.allowed_sources.values() for source in sources]
    for body, meaning in settlements.meanings.values():
        if body.source not in allowed:
            continue
        cards.update(item["asset_id"] for item in assets(body))
        cards.update(
            value
            for key, value in business_matter(body).items()
            if key in {"asset_id", "project_id", "fund_id"}
        )
        if meaning.person_id:
            employees.add(meaning.person_id)
        if meaning.recipient_id:
            counterparties.add(meaning.recipient_id)
        if meaning.party_id:
            counterparties.add(meaning.party_id)
    if content is not None:
        accounts.update(business_fund_accounts(content))
    accounts.update(
        event["funds_account_id"] for event in events if event["funds_account_id"] is not None
    )
    for row in settlements.position_rows:
        value = row["_detail_scope"]["position"]
        counterparties.add(value["operator_id"])
        cards.add(value["project_id"])
    counterparties -= employees
    objects = identities(reader.identity, counterparties | employees | cards | accounts, read)

    def entries(ids):
        return [
            profile(ident, objects[ident]["name"] if ident in objects else None, objects.get(ident))
            for ident in sorted(ids)
        ]

    title, note, note_scope = "业务详情", None, None
    subjects = description_subjects(selected, settlements)
    if content is not None:
        title = (
            "已撤去业务"
            if content.target["kind"] == "system.withdrawal"
            else business_label(content).title
        )
        if selected.detail is not None and content.target["kind"] in {
            "people.remuneration",
            "people.remuneration_tax",
        }:
            title = _payroll_journals.title(
                content,
                journal_key=_payroll_journals.phase_from_scope(selected.range),
                component=_payroll_journals.component_from_scope(selected.range),
            )
        own = whole_note_reference(selected)
        if descriptions is not None and own:
            represented = any(body.reference == own for body, _ in settlements.meanings.values())
            if not represented:
                note = descriptions.text(own)
        if note:
            external_sources = any(
                entry["producer"] != content.source
                for entry in selected.range.get("allocations", ())
            )
            whole = selected.detail is None or (
                selected.detail["key"].startswith("voucher:")
                and _payroll_journals.phase_from_scope(selected.range) in {None, "main"}
            )
            note_scope = "selected" if whole and not external_sources else "whole_business"
    purpose = None
    if (
        descriptions is not None
        and content is not None
        and selected.range.get("voucher_role") != "reversal"
    ):
        purposes = [descriptions.purpose(subject) for subject in subjects]
        purpose = (
            merged(
                value or subject.label.title
                for value, subject in zip(purposes, subjects, strict=True)
            )
            if any(purposes)
            else None
        )
        if purpose:
            data = content.basis.get("input", {})
            direction = selected.range.get("direction")
            if content.target["kind"] == "money.movement":
                direction = data.get("direction", "outflow")
            elif content.target["kind"] == "money.transfer" and data["type"] == "offset":
                direction = "offset"
            if direction in {"inflow", "outflow", "offset"}:
                codes = {subject.label.code for subject in subjects}
                code = next(iter(codes)) if len(codes) == 1 else "mixed_settlement"
                purpose = settlement_title(purpose, code, direction)
            purpose = role_prefix(selected.range.get("voucher_role")) + purpose
    business_profile = profile(selected.business["state"]["business_id"], title, note=note)
    business_profile["values"]["purpose"] = purpose
    business_profile["values"]["note_scope"] = note_scope
    return {
        "business": business_profile,
        "counterparties": entries(counterparties),
        "employees": entries(employees),
        "assets": entries(cards),
        "fund_accounts": entries(accounts),
    }


def result(content, *, frozen=False, closing_period=None, scope=None):
    if content is None or content.target["kind"] == "system.withdrawal":
        return None
    value = amount(
        content,
        journal_key=_payroll_journals.phase_from_scope(scope) if scope is not None else None,
        component=_payroll_journals.component_from_scope(scope) if scope is not None else None,
    )
    return {
        "amount_fen": value.amount_fen,
        "amount_label": value.label,
        "close_period" if frozen else "posting_period": closing_period
        if frozen
        else content.reference["posting_period"],
    }
