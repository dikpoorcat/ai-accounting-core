"""Sparse owner-facing names and amounts from the selected formal business meanings."""

from ._brief_activity_amounts import amount
from ._brief_activity_parts import business_parties
from ._brief_activity_sources import identities
from ._brief_vouchers import assets
from ._business_descriptions import description_reference, merged, role_prefix
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
        if selected.content is not None and selected.range.get("purpose") not in {
            None,
            business_label(selected.content).code,
        }:
            return ()
        return (
            ()
            if selected.content is None
            else ((selected.content, business_label(selected.content), None),)
        )
    values = []
    for entry in entries:
        source = entry["producer"]
        key = source["result_id"], (entry["source"]["business_id"], entry["source"]["slot"])
        body, meaning = settlements.meanings[key]
        values.append((body, meaning.label, meaning.component))
    return tuple(values)


def whole_note_reference(selected):
    """A payment's own prose belongs to its whole business, independently of its sources."""
    content = selected.content
    if content is None:
        return None
    entries = (*selected.range.get("allocations", ()), *selected.range.get("obligations", ()))
    is_part = selected.range.get("purpose") not in {None, business_label(content).code}
    if is_part or any(entry["producer"] != content.source for entry in entries):
        return description_reference(content)
    return None


def description_references(selected, settlements):
    """Only the selected subjects and their real settlement meanings consume current prose."""
    if selected.range.get("voucher_role") == "reversal":
        return ()
    subjects = description_subjects(selected, settlements)
    refs = [
        ref
        for body, label, component in subjects
        if (ref := description_reference(body, label, component=component)) is not None
    ]
    own = whole_note_reference(selected)
    if own:
        refs.append(own)
    refs.extend(
        ref
        for body, meaning in settlements.meanings.values()
        if (ref := description_reference(body, meaning.label, component=meaning.component))
        is not None
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
    references = [
        ref
        for body, label, component in subjects
        if (ref := description_reference(body, label, component=component)) is not None
    ]
    if content is not None:
        title = (
            "已撤去业务"
            if content.target["kind"] == "system.withdrawal"
            else business_label(content).title
        )
        own = whole_note_reference(selected)
        if descriptions is not None and own:
            note = descriptions.text(own)
        if note:
            whole = selected.detail is None or selected.detail["key"].startswith("voucher:")
            note_scope = "selected" if whole else "whole_business"
    purpose = None
    if (
        descriptions is not None
        and content is not None
        and selected.range.get("voucher_role") != "reversal"
    ):
        purpose = merged(descriptions.text(reference) for reference in references) or None
        if purpose:
            data = content.basis.get("input", {})
            direction = selected.range.get("direction")
            if content.target["kind"] == "money.movement":
                direction = data.get("direction", "outflow")
            elif content.target["kind"] == "money.transfer" and data["type"] == "offset":
                direction = "offset"
            if direction in {"inflow", "outflow", "offset"}:
                codes = {label.code for _, label, _ in subjects}
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


def result(content, *, frozen=False, closing_period=None):
    if content is None or content.target["kind"] == "system.withdrawal":
        return None
    value = amount(content)
    return {
        "amount_fen": value.amount_fen,
        "amount_label": value.label,
        "close_period" if frozen else "posting_period": closing_period
        if frozen
        else content.reference["posting_period"],
    }
