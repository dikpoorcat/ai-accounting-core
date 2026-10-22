"""Activity's current directory factory and exact publication guard."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, replace

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
)

from . import _brief_activity_dependencies as dependencies
from . import _brief_activity_totals as totals
from . import _item_buckets as buckets
from ._brief_activity_dependencies import BUSINESSES as BUSINESSES
from ._brief_activity_dependencies import OBJECTS as OBJECTS
from ._brief_activity_dependencies import ROOTS as ROOTS
from ._brief_activity_dependencies import corrupt as corrupt
from ._brief_activity_dependencies import root_item as root_item
from ._collections import PreparedModel, _decode, prepare_model, prepare_rows

COMPONENT = "brief_activity"
FORMAT_VERSION = 9
NAMESPACE = "ai-accounting-reading-activity-bucket/1"
DIRTY = "activity_pending"
BUCKETS = "_activity_buckets"
PUBLIC = ("activity", "activity_members", "brief_vouchers")
MIRRORS = (*PUBLIC, totals.CANDIDATES)
KEYS = (BUSINESSES, OBJECTS)
COLLECTIONS = (*MIRRORS, ROOTS, BUSINESSES, OBJECTS, totals.FOLDS)


def semantic(payload):
    return content_digest(
        {
            key: value
            for key, value in payload.items()
            if key not in {"_item_epoch", "_semantic_digest"}
        }
    )


def validate(payload, scope_key):
    try:
        return _validate(payload, scope_key)
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "业务活动的范围或完整目录不一致。") from error


def _validate(payload, scope_key):
    if payload["_scope"] != scope_key or payload["_semantic_digest"] != semantic(payload):
        corrupt()
    epoch = payload["_item_epoch"]
    if type(epoch) is not int or epoch < 0:
        corrupt()
    buckets.validate(payload["_buckets"], COLLECTIONS, KEYS, MIRRORS)
    if buckets.manifest(payload["_buckets"]) != payload["_collections"]:
        corrupt()
    dependencies.validate_inputs(payload, payload["_buckets"])
    categories, reviews = totals.summaries(payload["_category_folds"], payload["_review_folds"])
    manifests = payload["_collections"]
    if any(
        type(payload[field]) is not int
        for field in ("activity_count", "group_count", "voucher_count")
    ):
        corrupt()
    if (
        payload["activity_groups"] != categories
        or payload["review_businesses"] != reviews
        or payload["activity_count"] != manifests["activity_members"]["count"]
        or payload["group_count"] != manifests["activity"]["count"]
        or payload["voucher_count"] != manifests["brief_vouchers"]["count"]
        or sum(value["event_count"] for value in categories) != payload["activity_count"]
        or sum(value["group_count"] for value in categories) != payload["group_count"]
        or sum(value["count"] for value in reviews) != payload["activity_count"]
        or manifests[totals.FOLDS]["count"] != payload["group_count"]
        or not payload["group_count"]
        <= manifests[totals.CANDIDATES]["count"]
        <= payload["activity_count"]
    ):
        corrupt()
    return epoch


def base(repository, state):
    if state["format_version"] != FORMAT_VERSION:
        return None
    saved = repository.payload(state["scope_key"], COMPONENT)
    if saved is None:
        if state["ready_head"] == 0 and state["content_digest"] is None:
            return None
        corrupt()
    try:
        from ._brief_activity_directory import Directory

        return Directory.from_row(repository, state, saved)
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "业务活动的目录基底不完整。") from error


def _prepared(entries):
    entries = tuple(entries)
    if len({(entry.collection, entry.item_id) for entry in entries}) != len(entries) or any(
        entry.collection not in COLLECTIONS for entry in entries
    ):
        corrupt()
    rows, digests = prepare_rows(entry for entry in entries if entry.collection in MIRRORS)
    grouped = {}
    for entry in entries:
        if entry.collection not in MIRRORS:
            grouped.setdefault(buckets.key(entry.collection, entry.item_id), {})[entry.item_id] = (
                entry.value
            )
    for row, digest in zip(rows, digests, strict=True):
        grouped.setdefault(buckets.key(row[0], row[1]), {})[row[1]] = row[2], digest
    return rows, grouped


def _bucket(identity, values):
    collection, bucket = identity.rsplit(":", 1)
    descriptor, raw = buckets.prepare(
        collection,
        bucket,
        values,
        namespace=NAMESPACE,
        private=collection not in MIRRORS,
        keyed=collection in KEYS,
    )
    return descriptor, (BUCKETS, identity, identity, None, None, None, None, raw)


def _summary(summary, directories):
    categories, reviews = totals.summaries(summary["_category_folds"], summary["_review_folds"])
    manifests = buckets.manifest(directories)
    return dict(
        summary,
        _buckets=directories,
        _collections=manifests,
        activity_groups=categories,
        review_businesses=reviews,
        activity_count=manifests["activity_members"]["count"],
        group_count=manifests["activity"]["count"],
        voucher_count=manifests["brief_vouchers"]["count"],
    )


def seal(payload, rows, epoch):
    payload = dict(payload, _item_epoch=epoch)
    payload["_semantic_digest"] = semantic(payload)
    validate(payload, payload["_scope"])
    return replace(prepare_model(payload), rows=tuple(rows))


def complete(members, vouchers, roots, scope_key, epoch):
    entries, categories, reviews = totals.complete(members)
    owned = {item.item_id: dependencies.root(item.value) for item in roots}
    member_ids, voucher_ids = defaultdict(set), defaultdict(set)
    for member in members:
        member_ids[member["row"]["subject_id"]].add(member["row"]["key"])
    for item in vouchers:
        voucher_ids[item.business_id].add(item.item_id)
    for root_id, value in owned.items():
        if (
            value["business_id"] != root_id
            or set(value["members"]) != member_ids[root_id]
            or set(value["vouchers"]) != voucher_ids[root_id]
        ):
            corrupt()
    for entry in vouchers:
        dependencies.owned(entry, owned)
    for entry in entries:
        if entry.collection == "activity_members":
            dependencies.owned(entry, owned)
    dependency_items, selected = dependencies.complete(roots)
    entries.extend((*vouchers, *roots, *dependency_items))
    public, grouped = _prepared(entries)
    directories = {collection: {} for collection in COLLECTIONS}
    rows = list(public)
    for identity, values in grouped.items():
        collection, bucket = identity.rsplit(":", 1)
        directories[collection][bucket], row = _bucket(identity, values)
        rows.append(row)
    summary = {
        "_scope": scope_key,
        "_inputs": selected.wire(),
        "_category_folds": categories,
        "_review_folds": reviews,
    }
    return seal(_summary(summary, directories), rows, epoch)


@dataclass(frozen=True)
class PreparedActivityPatch:
    model: PreparedModel
    deletes: tuple[tuple[str, str], ...]
    base_ready: int
    base_digest: str
    item_epoch: int
    scope_key: str


def patch(summary, entries, deletes, directory, state):
    public, updates = _prepared(entries)
    deletes = set(deletes)
    if any(collection not in COLLECTIONS for collection, _ in deletes):
        corrupt()
    changed = deletes | {
        (identity.rsplit(":", 1)[0], item_id)
        for identity, values in updates.items()
        for item_id in values
    }
    keys = {buckets.key(*identity) for identity in changed}
    directory.load(keys)
    values = {key: dict(directory.loaded[key]) for key in keys}
    for collection, identity in deletes:
        values[buckets.key(collection, identity)].pop(identity, None)
    for key, additions in updates.items():
        values[key].update(additions)
    directories, rows = deepcopy(directory.roots), list(public)
    removed = {(collection, identity) for collection, identity in changed if collection in MIRRORS}
    for identity, contents in values.items():
        collection, bucket = identity.rsplit(":", 1)
        removed.add((BUCKETS, identity))
        if contents:
            directories[collection][bucket], row = _bucket(identity, contents)
            rows.append(row)
        else:
            directories[collection].pop(bucket, None)
    return PreparedActivityPatch(
        seal(_summary(summary, directories), rows, directory.epoch),
        tuple(sorted(removed)),
        state["ready_head"],
        state["content_digest"],
        directory.epoch,
        directory.scope_key,
    )


def pending(repository, scope_key, *, wanted=None):
    rows = repository.activity_pending_rows(scope_key)
    if not rows:
        return None
    try:
        if len(rows) != 1:
            corrupt()
        row, value = rows[0], _decode(rows[0])
        if (
            row["item_id"] != "current"
            or set(value) != {"full", "business_ids"}
            or type(value["full"]) is not bool
            or (wanted is not None and row["head"] > wanted)
        ):
            corrupt()
        dependencies.ids(value["business_ids"], uuid=True)
        if value["full"] and value["business_ids"]:
            corrupt()
        return value
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "业务活动的待更新范围损坏。") from error
