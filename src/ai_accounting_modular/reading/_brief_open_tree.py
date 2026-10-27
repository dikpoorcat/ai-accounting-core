"""Open's immutable row, ordering and sparse filter indexes; no accounting rules."""

from ai_accounting_modular.contracts.public import ContractError, canonical_json, content_digest
from ai_accounting_modular.storage.public import Entry, Tree, TreeStore

from ._collections import IndexItem, _bound, _index
from ._open_store import NAMESPACE

PUBLIC = ("open_items", "open_members")
DESCRIPTIONS = "_open_group_descriptions"
PRIVATE = (
    "_open_businesses",
    "_open_objects",
    "_open_folds",
    "_open_displays",
    "_open_notes",
    "_open_provider_inputs",
    DESCRIPTIONS,
)
COLLECTIONS = PUBLIC + PRIVATE
ROLES = ("rows", *PUBLIC, "filters", "voucher_keys")
FILTERS = ("object_id", "group_key", "category")


def corrupt(message="待收待付范围索引不完整，请重新生成。"):
    raise ContractError("storage_corrupt", message)


def key(*values):
    # A terminator sorts before an escaped NUL. This preserves tuple/string
    # ordering without hex-encoding already encoded public sort keys again.
    return "".join(str.replace(value, "\0", "\0\1") + "\0\0" for value in values)


def parts(value):
    result, part, position = [], [], 0
    while position < len(value):
        char = value[position]
        position += 1
        if char != "\0":
            part.append(char)
            continue
        if position == len(value) or value[position] not in ("\0", "\1"):
            corrupt()
        if value[position] == "\1":
            part.append("\0")
        else:
            result.append("".join(part))
            part = []
        position += 1
    if part or not result or key(*result) != value:
        corrupt()
    return tuple(result)


def bounds(*values):
    first = key(*values)
    return first, first[:-1] + "\1"


def digests(roots):
    return tuple(root.digest for root in roots.values() if root is not None)


def wire(roots):
    return {role: root.wire() if root is not None else None for role, root in roots.items()}


def validate(roots):
    if not isinstance(roots, dict) or set(roots) != set(ROLES):
        corrupt()
    for value in roots.values():
        if value is None:
            continue
        if (
            type(value) is not list
            or len(value) != 5
            or type(value[0]) is not str
            or len(value[0]) != 64
            or any(char not in "0123456789abcdef" for char in value[0])
            or any(type(item) is not str for item in value[1:3])
            or value[1] > value[2]
            or type(value[3]) is not int
            or value[3] < 1
            or type(value[4]) is not int
            or value[4] < 0
        ):
            corrupt()


def manifests(roots):
    return {
        collection: {
            "count": roots[collection][3] if roots[collection] is not None else 0,
            "digest": content_digest(
                [
                    "reading-open-order/1",
                    collection,
                    roots[collection][0] if roots[collection] else None,
                ]
            ),
        }
        for collection in PUBLIC
    }


def display_head(roots):
    value = roots["voucher_keys"]
    return content_digest(["reading-open-display/1", value[0] if value else None])


class Count:
    def leaf(self, entries):
        return len(entries)

    def branch(self, children):
        return sum(child.summary for child in children)

    def encode_summary(self, value):
        return self.decode_summary(value)

    def decode_summary(self, value):
        if type(value) is not int or value < 0:
            corrupt()
        return value

    def summary_from_count(self, count):
        return self.decode_summary(count)


class Trees:
    def __init__(self, pool):
        self.store = TreeStore(pool, namespace=NAMESPACE)
        self.trees = {
            role: Tree(
                "reading-open-" + role + ("/2" if role in ("rows", *PUBLIC) else "/1"),
                Count(),
                self.store,
                max_items=16 if role in ("rows", *PUBLIC) else 128,
            )
            for role in ROLES
        }

    def load(self, saved):
        validate(saved)
        return {role: self.trees[role].root(saved[role]) for role in ROLES}

    def full(self, entries):
        values = {role: [] for role in ROLES}
        seen = set()
        for entry in entries:
            values_for_entry = prepared(entry)
            identity = entry.collection, entry.item_id
            if identity in seen:
                corrupt("待收待付集合不能重复保存同一项目。")
            seen.add(identity)
            for role, item in values_for_entry:
                values[role].append(item)
        return {role: self.trees[role].full(values[role]) for role in ROLES}

    def patch(self, roots, old, entries, deletes):
        changes = {role: {} for role in ROLES}
        for identity in deletes | set(entries):
            if identity in old:
                for role, entry in prepared(old[identity]):
                    changes[role][entry.key] = None
        for value in entries.values():
            for role, entry in prepared(value):
                changes[role][entry.key] = entry
        return {role: self.trees[role].patch(roots[role], changes[role]) for role in ROLES}


def prepared(item):
    if not isinstance(item, IndexItem) or item.collection not in COLLECTIONS:
        corrupt("待收待付项目引用了未声明的集合。")
    index = _index(item)
    if item.collection == DESCRIPTIONS:
        fields, value = parts(item.item_id), item.value
        if (
            len(fields) != 2
            or type(value) is not dict
            or set(value) != {"text", "count"}
            or type(value["text"]) is not str
            or not value["text"]
            or content_digest(value["text"]) != fields[1]
            or type(value["count"]) is not int
            or value["count"] < 1
        ):
            corrupt("待收待付说明的身份或引用数量不完整。")
    identity = (
        key(item.collection, *parts(item.item_id))
        if item.collection == DESCRIPTIONS
        else key(item.collection, item.item_id)
    )
    if item.collection in PRIVATE:
        if item.sort_key != item.item_id or any(
            index[field] is not None for field in FILTERS + ("business_id",)
        ):
            corrupt("待收待付私有项目只能使用其明确身份。")
        canonical_json([item.collection, item.item_id])
        result = [("rows", Entry(identity, item.value))]
        if item.collection == "_open_displays":
            result.append(("voucher_keys", Entry(item.item_id, None)))
        return result
    _, digest = _bound(index, item.value)
    result = [
        ("rows", Entry(identity, item.sort_key)),
        (
            item.collection,
            Entry(
                key(item.sort_key, item.item_id),
                {"index": index, "value": item.value, "digest": digest},
            ),
        ),
    ]
    result.extend(
        (
            "filters",
            Entry(key(item.collection, field, index[field], item.sort_key, item.item_id), digest),
        )
        for field in FILTERS
        if index[field] is not None
    )
    return result
