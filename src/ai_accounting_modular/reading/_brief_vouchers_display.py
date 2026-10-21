"""Bind only the identity display fields referenced by exact voucher business meanings."""

from ai_accounting_modular.contracts.public import content_digest

from ._brief_activity_parts import business_parties
from ._brief_activity_sources import identities
from ._business_labels import origin_fields


def party_ids(content, prepared, cache):
    result = {party for part in prepared for party in part.parties}
    for part in prepared:
        for allocation in part.scope["allocations"]:
            result.update(
                allocation[key] for key in ("person_id", "recipient_id") if allocation.get(key)
            )
    result.update(business_parties(content, cache))
    for quantity in origin_fields(content)[1].get("adopted_quantities", ()):
        if quantity["type"] in {"obligation", "opening_balance"} and "party_id" in quantity:
            result.add(quantity["party_id"])
    return tuple(sorted(result))


def bind(reader, descriptors, read, *, objects=None):
    descriptors = list(descriptors)
    requested = {ident for item in descriptors for ident in item["party_ids"]}
    requested.update(card["asset_id"] for item in descriptors for card in item["assets"])
    objects = dict(objects or {})
    objects.update(identities(reader.identity, requested - objects.keys(), read))
    result = []
    for item in descriptors:
        saved = {key: value for key, value in item.items() if key != "party_ids"}
        saved["display"] = {
            "parties": {
                ident: objects[ident]["name"] if ident in objects else None
                for ident in item["party_ids"]
            },
            "assets": {
                card["asset_id"]: {
                    "name": objects[card["asset_id"]]["name"]
                    if card["asset_id"] in objects
                    else None,
                    "code": objects[card["asset_id"]].get("data", {}).get("code")
                    if card["asset_id"] in objects
                    else None,
                }
                for card in item["assets"]
            },
        }
        result.append(saved)
    return tuple(result)


def head(descriptors):
    unique = {
        item["template"]["voucher_version_id"]: item["display"]
        for item in descriptors
        if item is not None
    }
    return content_digest(unique)
