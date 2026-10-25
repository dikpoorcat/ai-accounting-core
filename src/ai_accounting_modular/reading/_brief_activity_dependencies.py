"""Current root ownership and reversible dependency membership, without process history."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    content_digest,
    validate_contract,
)

from . import _item_buckets as buckets
from ._collections import IndexItem

ROOTS = "activity_roots"
BUSINESSES = "_activity_businesses"
OBJECTS = "_activity_objects"


def corrupt():
    raise ContractError("storage_corrupt", "业务活动的当前依赖或展示索引不完整。")


def ids(values, *, uuid=False):
    if type(values) is not list or any(type(value) is not str or not value for value in values):
        corrupt()
    if values != sorted(set(values)):
        corrupt()
    if uuid:
        for identity in values:
            validate_contract("core.schema.json", "Id", identity)


def root(value):
    if type(value) is not dict or set(value) != {
        "business_id",
        "sources",
        "object_ids",
        "members",
        "vouchers",
        "note_scopes",
    }:
        corrupt()
    validate_contract("core.schema.json", "Id", value["business_id"])
    for field in ("object_ids", "members", "vouchers", "note_scopes"):
        ids(value[field], uuid=field == "object_ids")
    sources = value["sources"]
    if type(sources) is not list or not sources:
        corrupt()
    for source in sources:
        validate_contract("business-facts.schema.json", "ResultSourceRef", source)
    source_scopes = {"business:" + source["business_id"] for source in sources}
    if not set(value["note_scopes"]) <= source_scopes:
        corrupt()
    identities = [(source["result_id"], source["result_generation"]) for source in sources]
    if identities != sorted(set(identities)) or not any(
        source["business_id"] == value["business_id"] for source in sources
    ):
        corrupt()
    return value


def root_item(business_id, sources, object_ids, *, members=(), vouchers=(), note_scopes=()):
    unique = {}
    for source in sources:
        identity = source["result_id"], source["result_generation"]
        if identity in unique and unique[identity] != source:
            corrupt()
        unique[identity] = source
    value = root(
        {
            "business_id": business_id,
            "sources": [unique[key] for key in sorted(unique)],
            "object_ids": sorted(set(object_ids)),
            "members": sorted(set(members)),
            "vouchers": sorted(set(vouchers)),
            "note_scopes": sorted(set(note_scopes)),
        }
    )
    return IndexItem(ROOTS, business_id, business_id, value, business_id=business_id)


def owned(item, roots):
    saved = roots.get(item.business_id)
    if saved is None:
        corrupt()
    if item.collection == "activity_members":
        row, scope = item.value["row"], item.value["scope"]
        if (
            row["key"] != item.item_id
            or row["subject_id"] != item.business_id
            or item.item_id not in saved["members"]
            or scope["source"] not in saved["sources"]
            or row["group_key"] != item.group_key
            or (row["voucher_version_id"] and row["voucher_version_id"] not in saved["vouchers"])
        ):
            corrupt()
        for value in (*scope["allocations"], *scope["obligations"]):
            if value["producer"] not in saved["sources"]:
                corrupt()
    else:
        reference = item.value["reference"]
        if (
            item.item_id not in saved["vouchers"]
            or item.value["template"]["voucher_version_id"] != item.item_id
            or item.value["template"]["subject_id"] != item.business_id
            or not any(
                source["result_id"] == reference["result_id"]
                and source["result_generation"] == reference["generation"]
                and source["business_id"] == reference["business_id"]
                and source["result_slot"] == reference["result_slot"]
                for source in saved["sources"]
            )
        ):
            corrupt()
        if any(
            identity not in saved["object_ids"]
            for kind in ("parties", "assets")
            for identity in item.value["display"][kind]
        ):
            corrupt()


def memberships(roots, collection):
    grouped = defaultdict(set)
    for item in roots:
        values = (
            {source["business_id"] for source in item.value["sources"]}
            if collection == BUSINESSES
            else item.value["object_ids"]
        )
        for identity in values:
            grouped[identity].add(item.item_id)
    return grouped


def inputs(business_ids, object_ids, note_scopes=()):
    return ReadInputs(
        posted_results=True,
        business_ids=tuple(sorted(business_ids)),
        object_ids=tuple(sorted(object_ids)),
        note_scopes=tuple(sorted(note_scopes)),
    )


def complete(roots):
    entries, keys = [], {}
    for collection in (BUSINESSES, OBJECTS):
        values = memberships(roots, collection)
        keys[collection] = set(values)
        entries.extend(
            IndexItem(collection, identity, identity, {"roots": sorted(owners)})
            for identity, owners in values.items()
        )
    return entries, inputs(
        keys[BUSINESSES],
        keys[OBJECTS],
        {scope for item in roots for scope in item.value["note_scopes"]},
    )


def validate_inputs(payload, directories):
    from ._dependencies import inputs as declared_inputs

    selected = declared_inputs(payload)
    if selected != inputs(selected.business_ids, selected.object_ids, selected.note_scopes):
        corrupt()
    if not set(selected.note_scopes) <= {"business:" + ident for ident in selected.business_ids}:
        corrupt()
    for collection, identities in (
        (BUSINESSES, selected.business_ids),
        (OBJECTS, selected.object_ids),
    ):
        grouped = defaultdict(list)
        for identity in identities:
            grouped[buckets.key(collection, identity).rsplit(":", 1)[1]].append(identity)
        expected = directories[collection]
        if grouped.keys() != expected.keys() or any(
            expected[key]["count"] != len(values)
            or expected[key]["keys_digest"] != content_digest(sorted(values))
            for key, values in grouped.items()
        ):
            corrupt()
    return selected


def changes(directory, before, after, entries, deletes):
    selected = validate_inputs(directory.payload, directory.roots)
    keys = {BUSINESSES: set(selected.business_ids), OBJECTS: set(selected.object_ids)}
    for collection in (BUSINESSES, OBJECTS):
        previous, proposed = memberships(before, collection), memberships(after, collection)
        changed = {
            identity
            for identity in previous.keys() | proposed.keys()
            if previous[identity] != proposed[identity]
        }
        directory.dependents(collection, changed)
        current = directory.get(collection, changed)
        for identity in changed:
            owners = set(current.get(identity, {}).get("roots", ()))
            if not previous[identity] <= owners:
                corrupt()
            owners.difference_update(previous[identity])
            owners.update(proposed[identity])
            deletes.add((collection, identity))
            if owners:
                entries.append(IndexItem(collection, identity, identity, {"roots": sorted(owners)}))
                keys[collection].add(identity)
            else:
                keys[collection].discard(identity)
    scopes = set(selected.note_scopes)
    # Scope membership follows business dependencies; surviving owners still subscribe.
    for item in before:
        scopes.difference_update(item.value["note_scopes"])
    scopes.update(scope for item in after for scope in item.value["note_scopes"])
    candidates = {scope for item in before for scope in item.value["note_scopes"]} - scopes
    owners = directory.dependents(BUSINESSES, {scope[9:] for scope in candidates})
    survivors = directory.get(ROOTS, owners - {item.item_id for item in before})
    scopes.update(scope for value in survivors.values() for scope in value["note_scopes"])
    return inputs(keys[BUSINESSES], keys[OBJECTS], scopes)
