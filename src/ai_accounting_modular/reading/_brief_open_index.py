"""BriefOpen's own compact indexes and publication proof; no formal accounting lives here."""

from copy import deepcopy
from dataclasses import dataclass, replace

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    content_digest,
    loads_json,
)

from . import _brief_open_buckets as buckets
from . import _dependencies
from ._collections import IndexItem, PreparedModel, _decode, prepare_items, prepare_model

COMPONENT = "brief_open_items"
FORMAT_VERSION = 8
DIRTY = "_open_pending"
BUSINESSES = "_open_businesses"
OBJECTS = "_open_objects"
FOLDS = "_open_folds"
VOUCHERS = "_open_displays"
BUCKETS = "_open_buckets"
COLLECTIONS = ("open_items", "open_members", BUSINESSES, OBJECTS, FOLDS, VOUCHERS)
PUBLIC = ("open_items", "open_members")


def corrupt(message="待收待付读取索引不完整，请重新生成。"):
    raise ContractError("storage_corrupt", message)


def item(collection, identity, value):
    return IndexItem(collection, identity, identity, value)


def semantic(payload):
    return content_digest(
        {
            key: value
            for key, value in payload.items()
            if key not in {"_item_epoch", "_semantic_digest"}
        }
    )


def validate(payload):
    cooperation_inputs(payload)
    epoch = payload["_item_epoch"]
    if type(epoch) is not int or epoch < 0 or payload["_semantic_digest"] != semantic(payload):
        corrupt()
    roots = payload["_buckets"]
    buckets.validate(roots, COLLECTIONS, VOUCHERS, PUBLIC)
    if (
        buckets.manifest(roots) != payload["_collections"]
        or buckets.display_head(roots[VOUCHERS]) != payload["_voucher_display_head"]
    ):
        corrupt()
    return epoch


def base(repository, state):
    row = repository.payload(state["scope_key"], COMPONENT)
    if row is None:
        return None
    value = loads_json(row["payload_json"])
    if row["head"] != state["ready_head"] or content_digest(value) != state["content_digest"]:
        corrupt()
    validate(value)
    _dependencies.inputs(value)
    return value


def cooperation_inputs(payload):
    try:
        return ReadInputs.from_wire(payload["_cooperation_inputs"])
    except (KeyError, TypeError, ValueError):
        corrupt("待收待付缺少完整的合作位置输入依赖，请重新生成。")


def seal(payload, rows, epoch):
    payload = dict(payload)
    payload["_semantic_digest"] = semantic(payload)
    payload["_item_epoch"] = epoch
    return replace(prepare_model(payload), rows=tuple(rows))


def _prepared(entries):
    public, grouped, identities = [], {}, set()
    for entry in entries:
        if (
            not isinstance(entry, IndexItem)
            or entry.collection not in COLLECTIONS
            or not isinstance(entry.item_id, str)
            or not entry.item_id
        ):
            corrupt("待收待付项目引用了未声明的集合。")
        identity = entry.collection, entry.item_id
        if identity in identities:
            corrupt("待收待付集合不能重复保存同一项目。")
        identities.add(identity)
        if entry.collection in PUBLIC:
            public.append(entry)
        else:
            if entry.sort_key != entry.item_id or any(
                getattr(entry, field) is not None
                for field in ("business_id", "object_id", "group_key", "category")
            ):
                corrupt("待收待付私有项目只能使用其明确身份。")
            grouped.setdefault(buckets.key(*identity), {})[entry.item_id] = entry.value
    prepared = prepare_items(public)
    for row, digest in zip(prepared.rows, prepared.row_digests, strict=True):
        collection, identity, order = row[:3]
        grouped.setdefault(buckets.key(collection, identity), {})[identity] = order, digest
    return prepared.rows, grouped


def _bucket(identity, values):
    collection, bucket = identity.rsplit(":", 1)
    descriptor, raw = buckets.prepare(
        collection,
        bucket,
        values,
        private=collection not in PUBLIC,
        displays=collection == VOUCHERS,
    )
    return descriptor, (BUCKETS, identity, identity, None, None, None, None, raw)


def complete(summary, entries, epoch):
    public, grouped = _prepared(entries)
    roots = {collection: {} for collection in COLLECTIONS}
    rows = list(public)
    for key, values in grouped.items():
        collection, bucket = key.rsplit(":", 1)
        descriptor, row = _bucket(key, values)
        roots[collection][bucket] = descriptor
        rows.append(row)
    return seal(_summary(summary, roots), rows, epoch)


def decoded(row, epoch):
    if row["head"] != epoch:
        corrupt("待收待付项目混入了其他生成代次，请重新生成。")
    return _decode(row)


def _summary(summary, roots):
    return dict(
        summary,
        _buckets=roots,
        _collections=buckets.manifest(roots),
        _voucher_display_head=buckets.display_head(roots[VOUCHERS]),
    )


class Directory:
    """Same-read bucket proofs only; never a cache across transactions or scopes."""

    def __init__(self, repository, scope_key, payload):
        self.repository, self.scope_key = repository, scope_key
        self.epoch = validate(payload)
        self.roots, self.loaded = payload["_buckets"], {}
        if repository.open_bucket_count(scope_key) != sum(
            len(root) for root in self.roots.values()
        ):
            corrupt("待收待付物理桶数量与完整目录不一致。")

    def load(self, keys):
        missing = set(keys) - self.loaded.keys()
        if not missing:
            return
        for key in missing:
            collection, bucket = key.rsplit(":", 1)
            if (
                collection not in COLLECTIONS
                or len(bucket) != 2
                or any(c not in "0123456789abcdef" for c in bucket)
            ):
                corrupt()
        selected = {}
        for row in self.repository.open_bucket_rows(self.scope_key, tuple(sorted(missing))):
            key = row["item_id"]
            if (
                key not in missing
                or key in selected
                or row["collection_key"] != BUCKETS
                or row["sort_key"] != key
                or row["scope_key"] != self.scope_key
                or row["component_key"] != COMPONENT
                or row["head"] != self.epoch
                or any(
                    row[field] is not None
                    for field in ("business_id", "object_id", "group_key", "category")
                )
            ):
                corrupt()
            collection, bucket = key.rsplit(":", 1)
            expected = self.roots[collection].get(bucket)
            if expected is None:
                corrupt("未声明的待收待付桶中存在额外内容。")
            selected[key] = buckets.read(
                row["payload_json"],
                collection,
                bucket,
                expected,
                private=collection not in PUBLIC,
                displays=collection == VOUCHERS,
            )
        for key in missing - selected.keys():
            collection, bucket = key.rsplit(":", 1)
            if bucket in self.roots[collection]:
                corrupt()
            selected[key] = {}
        self.loaded.update(selected)

    def known(self, collection, identities):
        if collection not in COLLECTIONS:
            raise ContractError("invalid_input", "待收待付目录未声明所选集合。")
        identities = set(identities)
        self.load(buckets.key(collection, identity) for identity in identities)
        return {
            identity
            for identity in identities
            if identity in self.loaded[buckets.key(collection, identity)]
        }

    def check(self, row):
        collection, identity = row["collection_key"], row["item_id"]
        if (
            collection not in PUBLIC
            or row["scope_key"] != self.scope_key
            or row["component_key"] != COMPONENT
        ):
            corrupt()
        key = buckets.key(collection, identity)
        self.load((key,))
        value = decoded(row, self.epoch)
        digest = loads_json(row["payload_json"])["digest"]
        if self.loaded[key].get(identity) != (row["sort_key"], digest):
            corrupt()
        return value

    def check_many(self, rows):
        """One bucket read anchors every returned row and any paging lookahead."""
        self.load(buckets.key(row["collection_key"], row["item_id"]) for row in rows)
        return tuple(self.check(row) for row in rows)

    def get(self, collection, identities, *, required=False):
        identities = tuple(dict.fromkeys(identities))
        expected = self.known(collection, identities)
        if required and expected != set(identities):
            corrupt()
        if collection not in PUBLIC:
            return {
                identity: deepcopy(self.loaded[buckets.key(collection, identity)][identity])
                for identity in identities
                if identity in expected
            }
        rows = (
            self.repository.item_get(self.scope_key, COMPONENT, collection, identities)
            if identities
            else ()
        )
        if {row["item_id"] for row in rows} != expected:
            corrupt()
        return {row["item_id"]: self.check(row) for row in rows}


@dataclass(frozen=True)
class PreparedOpenPatch:
    model: PreparedModel
    deletes: tuple[tuple[str, str], ...]
    base_ready: int
    base_digest: str
    item_epoch: int
    base_scope: str


def patch(summary, entries, deletes, directory, state):
    public, updates = _prepared(entries)
    deletes = set(deletes)
    if any(collection not in COLLECTIONS for collection, _ in deletes):
        corrupt()
    changed = deletes | {
        (key.rsplit(":", 1)[0], identity) for key, values in updates.items() for identity in values
    }
    keys = {buckets.key(*identity) for identity in changed}
    directory.load(keys)
    values = {key: dict(directory.loaded[key]) for key in keys}
    for collection, identity in deletes:
        values[buckets.key(collection, identity)].pop(identity, None)
    for key, additions in updates.items():
        values[key].update(additions)
    roots = deepcopy(directory.roots)
    rows = list(public)
    removed = {(collection, identity) for collection, identity in changed if collection in PUBLIC}
    for key, selected in values.items():
        collection, bucket = key.rsplit(":", 1)
        removed.add((BUCKETS, key))
        if selected:
            roots[collection][bucket], row = _bucket(key, selected)
            rows.append(row)
        else:
            roots[collection].pop(bucket, None)
    return PreparedOpenPatch(
        seal(_summary(summary, roots), rows, directory.epoch),
        tuple(sorted(removed)),
        state["ready_head"],
        state["content_digest"],
        directory.epoch,
        directory.scope_key,
    )
