"""Sparse owner-facing names and amounts from the selected formal business meanings."""

from ._brief_activity_amounts import amount
from ._brief_activity_parts import business_parties
from ._brief_activity_sources import identities
from ._brief_vouchers import assets
from ._business_labels import business_label, business_matter


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
            "employment_start": None,
            "employment_end": None,
            "employment_status": None,
            "employment_state": None,
            "active": data.get("active"),
            "category_label": None,
        },
    }


def profiles(reader, selected, settlements, events, read):
    counterparties, employees, cards, accounts = set(), set(), set(), set()
    content = selected.content
    if content is not None and content.target["kind"] != "system.withdrawal":
        counterparties.update(business_parties(content, selected.cache))
        cards.update(item["asset_id"] for item in assets(content))
        matter = business_matter(content)
        cards.update(
            value for key, value in matter.items() if key in {"asset_id", "project_id", "fund_id"}
        )
        if content.target["kind"] == "people.remuneration":
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
        kind, data = content.target["kind"], content.basis.get("input", {})
        if kind in {
            "money.movement",
            "financing.owner_funding",
            "financing.loan_drawdown",
            "investment.cooperation_funding",
            "investment.cooperation_distribution",
        }:
            if "funds_account_id" in data:
                accounts.add(data["funds_account_id"])
        elif kind == "money.transfer" and data["type"] == "physical":
            accounts.update((data["from_account_id"], data["to_account_id"]))
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

    title, note = "业务详情", None
    if content is not None:
        title = (
            "已撤去业务"
            if content.target["kind"] == "system.withdrawal"
            else business_label(content).title
        )
        data = content.basis.get("input", {})
        notes = [
            data[key] for key in ("note", "memo") if isinstance(data.get(key), str) and data[key]
        ]
        note = "；".join(dict.fromkeys(notes)) or None
    return {
        "business": profile(selected.business["state"]["business_id"], title, note=note),
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
