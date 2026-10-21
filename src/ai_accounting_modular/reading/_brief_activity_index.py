"""BriefActivity's current root dependencies and checked reusable display rows."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    content_digest,
    loads_json,
    validate_contract,
)

from . import _dependencies
from ._collections import IndexItem, _decode, prepare_items

COMPONENT = "brief_activity"
FORMAT_VERSION = 8
ROOTS = "activity_roots"
DIRTY = "activity_pending"
COLLECTIONS = ("activity", "activity_members", "brief_vouchers", ROOTS)


def corrupt():
    raise ContractError("storage_corrupt", "业务活动的当前依赖或展示索引不完整。")


def _ids(values):
    if type(values) is not list or values != sorted(set(values)):
        corrupt()
    for identity in values:
        validate_contract("core.schema.json", "Id", identity)


def root(value):
    if type(value) is not dict or set(value) != {"business_id", "sources", "object_ids"}:
        corrupt()
    validate_contract("core.schema.json", "Id", value["business_id"])
    _ids(value["object_ids"])
    sources = value["sources"]
    if type(sources) is not list or not sources:
        corrupt()
    for source in sources:
        validate_contract("business-facts.schema.json", "ResultSourceRef", source)
    identities = [(source["result_id"], source["result_generation"]) for source in sources]
    if identities != sorted(set(identities)) or not any(
        source["business_id"] == value["business_id"] for source in sources
    ):
        corrupt()
    return value


def root_item(business_id, sources, object_ids):
    unique = {}
    for source in sources:
        key = source["result_id"], source["result_generation"]
        if key in unique and unique[key] != source:
            corrupt()
        unique[key] = source
    value = root(
        {
            "business_id": business_id,
            "sources": [unique[key] for key in sorted(unique)],
            "object_ids": sorted(set(object_ids)),
        }
    )
    return IndexItem(ROOTS, business_id, business_id, value, business_id=business_id)


def dependencies(roots):
    return ReadInputs(
        posted_results=True,
        business_ids=tuple(
            sorted({source["business_id"] for item in roots for source in item.value["sources"]})
        ),
        object_ids=tuple(
            sorted({identity for item in roots for identity in item.value["object_ids"]})
        ),
    )


def _owned(item, roots):
    if item.business_id not in roots:
        corrupt()
    saved = roots[item.business_id]
    if item.collection == "activity_members":
        scope = item.value["scope"]
        if (
            item.value["row"]["subject_id"] != item.business_id
            or scope["source"] not in saved["sources"]
        ):
            corrupt()
        for value in (*scope["allocations"], *scope["obligations"]):
            if value["producer"] not in saved["sources"]:
                corrupt()
    else:
        reference = item.value["reference"]
        if item.value["template"]["subject_id"] != item.business_id or not any(
            source["result_id"] == reference["result_id"]
            and source["result_generation"] == reference["generation"]
            and source["business_id"] == reference["business_id"]
            and source["result_slot"] == reference["result_slot"]
            for source in saved["sources"]
        ):
            corrupt()
        if any(
            identity not in saved["object_ids"]
            for kind in ("parties", "assets")
            for identity in item.value["display"][kind]
        ):
            corrupt()


def base(repository, state):
    """Read and check only Reading's own complete previous small projection."""
    if state["ready_head"] is None:
        return None
    saved = repository.payload(state["scope_key"], COMPONENT)
    try:
        if saved is None or saved["head"] != state["ready_head"]:
            corrupt()
        payload = loads_json(saved["payload_json"])
        if content_digest(payload) != state["content_digest"]:
            corrupt()
        rows = repository.activity_rows(state["scope_key"])
        if any(row["head"] != state["ready_head"] for row in rows):
            corrupt()
        items = []
        for row in rows:
            if row["collection_key"] not in COLLECTIONS:
                corrupt()
            items.append(
                IndexItem(
                    row["collection_key"],
                    row["item_id"],
                    row["sort_key"],
                    _decode(row),
                    **{
                        key: row[key]
                        for key in ("business_id", "object_id", "group_key", "category")
                    },
                )
            )
        manifest = prepare_items(items, collections=COLLECTIONS).with_summary({})["_collections"]
        if manifest != payload["_collections"]:
            corrupt()
        roots = {item.item_id: root(item.value) for item in items if item.collection == ROOTS}
        for item in items:
            if item.collection == ROOTS:
                if item.item_id != item.business_id or item.item_id != item.value["business_id"]:
                    corrupt()
            elif item.collection in {"activity_members", "brief_vouchers"}:
                _owned(item, roots)
        if _dependencies.inputs(payload) != dependencies(
            [item for item in items if item.collection == ROOTS]
        ):
            corrupt()
        return tuple(items)
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "业务活动的当前依赖或展示索引不完整。") from error


def pending(repository, scope_key, *, wanted=None):
    rows = repository.activity_pending_rows(scope_key)
    if not rows:
        return None
    try:
        if len(rows) != 1:
            corrupt()
        row = rows[0]
        value = _decode(row)
        if (
            row["item_id"] != "current"
            or set(value) != {"full", "business_ids"}
            or type(value["full"]) is not bool
            or (wanted is not None and row["head"] > wanted)
        ):
            corrupt()
        _ids(value["business_ids"])
        if value["full"] and value["business_ids"]:
            corrupt()
        return value
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "业务活动的待更新范围损坏。") from error
