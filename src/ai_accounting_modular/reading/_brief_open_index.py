"""Open's immutable range roots, strict logical rows and publication candidates."""

from collections import defaultdict
from copy import deepcopy
from dataclasses import dataclass, replace

from ai_accounting_modular.contracts.public import ReadInputs, canonical_json, content_digest
from ai_accounting_modular.storage.public import TreePlan

from . import _brief_open_tree as blocks
from ._brief_open_queries import Queries
from ._collections import IndexItem, PreparedModel, _index, _read_item, prepare_model
from ._open_store import OpenPool

COMPONENT = "brief_open_items"
FORMAT_VERSION = 9
DIRTY = "_open_pending"
BUSINESSES, OBJECTS, FOLDS, VOUCHERS = blocks.PRIVATE
COLLECTIONS, PUBLIC = blocks.COLLECTIONS, blocks.PUBLIC
corrupt = blocks.corrupt


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


def cooperation_inputs(payload):
    try:
        return ReadInputs.from_wire(payload["_cooperation_inputs"])
    except (KeyError, TypeError, ValueError):
        corrupt("待收待付缺少完整的合作位置输入依赖，请重新生成。")


def validate(payload):
    cooperation_inputs(payload)
    epoch = payload["_item_epoch"]
    if type(epoch) is not int or epoch < 0 or payload["_semantic_digest"] != semantic(payload):
        corrupt()
    blocks.validate(payload["_roots"])
    if payload["_collections"] != blocks.manifests(payload["_roots"]) or payload[
        "_voucher_display_head"
    ] != blocks.display_head(payload["_roots"]):
        corrupt()
    return epoch


def seal(payload, rows, epoch):
    payload = dict(payload)
    payload["_semantic_digest"] = semantic(payload)
    payload["_item_epoch"] = epoch
    return replace(prepare_model(payload), rows=tuple(rows))


def _summary(summary, roots):
    saved = blocks.wire(roots)
    return dict(
        summary,
        _roots=saved,
        _collections=blocks.manifests(saved),
        _voucher_display_head=blocks.display_head(saved),
    )


@dataclass(frozen=True)
class PreparedOpen:
    model: PreparedModel
    pool_token: str | None
    tree_plan: TreePlan
    scope_key: str

    @property
    def payload_json(self):
        return self.model.payload_json

    @property
    def content_digest(self):
        return self.model.content_digest

    @property
    def rows(self):
        return self.model.rows


@dataclass(frozen=True)
class PreparedOpenPatch(PreparedOpen):
    base_ready: int
    base_digest: str
    item_epoch: int
    base_scope: str


def base(repository, state):
    row = repository.payload(state["scope_key"], COMPONENT)
    if row is None:
        if state["ready_head"] != 0 or state["content_digest"] is not None:
            corrupt("待收待付已发布范围缺少汇总，须重新生成共享索引。")
        return None
    return Directory.from_row(repository, state, row)


def complete(summary, entries, epoch, *, repository, scope_key):
    pool = OpenPool(repository)
    trees = blocks.Trees(pool)
    state = repository.state(scope_key, COMPONENT)
    previous = base(repository, state) if state is not None else None
    old = blocks.digests(previous.roots) if previous is not None else ()
    roots = trees.full(entries)
    return PreparedOpen(
        seal(_summary(summary, roots), (), epoch),
        pool.token,
        trees.store.finish(old, blocks.digests(roots)),
        scope_key,
    )


class Directory(Queries):
    """Actual roots belong to this read; immutable nodes can be shared across months."""

    def __init__(self, repository, scope_key, payload):
        self.epoch = validate(payload)
        self._bind(repository, scope_key, payload)

    @classmethod
    def from_row(cls, repository, state, row):
        from ._component_view import summary

        payload, epoch = summary(state["scope_key"], COMPONENT, state, row)
        result = cls.__new__(cls)
        result.epoch = epoch
        result._bind(repository, state["scope_key"], payload)
        return result

    def _bind(self, repository, scope_key, payload):
        self.scope_key, self.payload = scope_key, payload
        self.pool = OpenPool(repository)
        if self.pool.token is None:
            corrupt("待收待付范围缺少实际共享索引，请重新生成。")
        self.trees = blocks.Trees(self.pool)
        self.trees.store.read_blocks(
            (self.trees.store.NODES, root[0])
            for root in payload["_roots"].values()
            if root is not None
        )
        self.roots = self.trees.load(payload["_roots"])
        for collection in PUBLIC:
            first, last = blocks.bounds(collection)
            count = self.trees.trees["rows"].summary(self.roots["rows"], first=first, last=last)
            if count != self.payload["_collections"][collection]["count"]:
                corrupt("待收待付完整项目与排序目录数量不一致。")

    def _entries(self, collection, identities):
        if collection not in COLLECTIONS:
            corrupt("待收待付项目引用了未声明的集合。")
        keys = tuple(blocks.key(collection, identity) for identity in identities)
        return self.trees.trees["rows"].get_many(self.roots["rows"], keys)

    def _row(self, collection, identity, entry):
        try:
            saved = entry.value
            if entry.key != blocks.key(collection, identity) or set(saved) != {
                "index",
                "value",
                "digest",
            }:
                corrupt()
            index = saved["index"]
            if (
                set(index)
                != {
                    "collection",
                    "item_id",
                    "sort_key",
                    "business_id",
                    "object_id",
                    "group_key",
                    "category",
                }
                or index["collection"] != collection
                or index["item_id"] != identity
            ):
                corrupt()
            row = {
                "scope_key": self.scope_key,
                "component_key": COMPONENT,
                "head": self.epoch,
                **{
                    ("collection_key" if key == "collection" else key): value
                    for key, value in index.items()
                },
                "payload_json": canonical_json(
                    {"digest": saved["digest"], "value": saved["value"]}
                ),
            }
            item, _, digest = _read_item(row)
            return row, item, digest
        except (KeyError, TypeError, ValueError, AttributeError):
            corrupt()

    def known(self, collection, identities):
        identities = tuple(dict.fromkeys(identities))
        return {
            identity
            for identity, entry in zip(
                identities, self._entries(collection, identities), strict=True
            )
            if entry is not None
        }

    def _materialize(self, collection, item_ids):
        if collection not in PUBLIC:
            corrupt()
        values = tuple(
            self._row(collection, identity, entry)
            for identity, entry in zip(item_ids, self._entries(collection, item_ids), strict=True)
            if entry is not None
        )
        selected = self.trees.trees[collection].get_many(
            self.roots[collection],
            tuple(blocks.key(item.sort_key, item.item_id) for _, item, _ in values),
        )
        for (_, _, digest), entry in zip(values, selected, strict=True):
            if entry is None or entry.value != digest:
                corrupt("待收待付项目未被本范围的排序根引用。")
        return values

    def item_get(self, collection, item_ids):
        return tuple(row for row, _, _ in self._materialize(collection, item_ids))

    def check(self, row):
        return self.check_many((row,))[0]

    def check_many(self, rows):
        rows, grouped, expected = tuple(rows), defaultdict(set), {}
        try:
            for row in rows:
                collection, identity = row["collection_key"], row["item_id"]
                if (
                    collection not in PUBLIC
                    or row["scope_key"] != self.scope_key
                    or row["component_key"] != COMPONENT
                    or row["head"] != self.epoch
                ):
                    corrupt()
                grouped[collection].add(identity)
            for collection, identities in grouped.items():
                for row, item, _ in self._materialize(collection, tuple(sorted(identities))):
                    expected[collection, item.item_id] = row, item.value
            result = []
            for row in rows:
                actual, value = expected[row["collection_key"], row["item_id"]]
                if actual != row:
                    corrupt()
                result.append(deepcopy(value))
            return tuple(result)
        except (KeyError, TypeError, ValueError):
            corrupt()

    def get(self, collection, identities, *, required=False):
        identities = tuple(dict.fromkeys(identities))
        if collection in PUBLIC:
            values = self._materialize(collection, identities)
            if required and len(values) != len(identities):
                corrupt()
            return {item.item_id: item.value for _, item, _ in values}
        actual = self._entries(collection, identities)
        if required and any(entry is None for entry in actual):
            corrupt()
        result = {}
        for identity, entry in zip(identities, actual, strict=True):
            if entry is not None:
                value = entry.value
                if not isinstance(value, dict):
                    corrupt()
                result[identity] = value
        return result


def patch(summary, entries, deletes, directory, state, *, scope_key):
    additions = {}
    for entry in entries:
        if not isinstance(entry, IndexItem) or entry.collection not in COLLECTIONS:
            corrupt()
        _index(entry)
        identity = entry.collection, entry.item_id
        if identity in additions:
            corrupt("待收待付集合不能重复保存同一项目。")
        additions[identity] = entry
    deletes = set(deletes)
    if any(collection not in COLLECTIONS for collection, _ in deletes):
        corrupt()
    identities, old = deletes | set(additions), {}
    for collection in COLLECTIONS:
        selected = tuple(identity for name, identity in identities if name == collection)
        if collection in PUBLIC:
            for _, value, _ in directory._materialize(collection, selected):
                old[collection, value.item_id] = value
        else:
            for identity, value in directory.get(collection, selected).items():
                old[collection, identity] = item(collection, identity, value)
    roots = directory.trees.patch(directory.roots, old, additions, deletes)
    return PreparedOpenPatch(
        seal(_summary(summary, roots), (), directory.epoch),
        directory.pool.token,
        directory.trees.store.finish(
            blocks.digests(directory.roots) if scope_key == directory.scope_key else (),
            blocks.digests(roots),
        ),
        scope_key,
        state["ready_head"],
        state["content_digest"],
        directory.epoch,
        directory.scope_key,
    )
