"""BriefOpen's own compact indexes and publication proof; no formal accounting lives here."""

from copy import deepcopy
from dataclasses import dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
)

from . import _brief_open_buckets as buckets
from ._collections import IndexItem, PreparedModel, _decode, prepare_items

COMPONENT = "brief_open_items"
DIRTY = "_open_pending"
BUSINESSES = "_open_businesses"
OBJECTS = "_open_objects"
FOLDS = "_open_folds"
VOUCHERS = "_open_displays"
DIGESTS = "_open_digests"
COLLECTIONS = ("open_items", "open_members", BUSINESSES, OBJECTS, FOLDS, VOUCHERS)


def corrupt(message="待收待付读取索引不完整，请重新生成。"):
    raise ContractError("storage_corrupt", message)


def item(collection, identity, value):
    return IndexItem(collection, identity, identity, value)


def leaf_id(collection, identity):
    return canonical_json([collection, identity])


def semantic(payload):
    return content_digest(
        {
            key: value
            for key, value in payload.items()
            if key not in {"_item_epoch", "_semantic_digest"}
        }
    )


def validate(payload):
    epoch = payload["_item_epoch"]
    if type(epoch) is not int or epoch < 0 or payload["_semantic_digest"] != semantic(payload):
        corrupt()
    roots = payload["_buckets"]
    buckets.validate(roots, COLLECTIONS, VOUCHERS)
    if (
        buckets.manifest(roots) != payload["_collections"]
        or buckets.display_head(roots[VOUCHERS]) != payload["_voucher_display_head"]
    ):
        corrupt()
    return epoch


def seal(payload, rows, epoch):
    payload = dict(payload)
    payload["_semantic_digest"] = semantic(payload)
    payload["_item_epoch"] = epoch
    return PreparedModel(canonical_json(payload), content_digest(payload), tuple(rows))


def leaves(rows):
    """Mirror only index and content hashes, so later scans never fetch member bodies."""
    result = []
    for row in rows:
        collection, identity, order = row[:3]
        digest = loads_json(row[-1])["digest"]
        result.append(
            (
                DIGESTS,
                leaf_id(collection, identity),
                order,
                None,
                None,
                buckets.key(collection, identity),
                None,
                canonical_json({"item_id": identity, "digest": digest}),
            )
        )
    return tuple(result)


def complete(summary, entries, epoch):
    prepared = prepare_items(entries, collections=COLLECTIONS)
    roots = {collection: {} for collection in COLLECTIONS}
    grouped = {}
    for row in prepared.rows:
        collection, identity, order = row[:3]
        grouped.setdefault(buckets.key(collection, identity), {})[identity] = (
            order,
            loads_json(row[-1])["digest"],
        )
    for key, values in grouped.items():
        collection, bucket = key.rsplit(":", 1)
        roots[collection][bucket] = buckets.describe(values, displays=collection == VOUCHERS)
    return seal(_summary(summary, roots), (*prepared.rows, *leaves(prepared.rows)), epoch)


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


def _leaf(row, epoch):
    try:
        saved = loads_json(row["payload_json"])
        if not isinstance(saved, dict) or set(saved) != {"item_id", "digest"}:
            corrupt()
        identity, digest = saved["item_id"], saved["digest"]
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "待收待付摘要索引不完整，请重新生成。") from error
    if not isinstance(row["group_key"], str) or ":" not in row["group_key"]:
        corrupt()
    collection = row["group_key"].rsplit(":", 1)[0]
    if (
        row["collection_key"] != DIGESTS
        or collection not in COLLECTIONS
        or row["head"] != epoch
        or row["item_id"] != leaf_id(collection, identity)
        or row["group_key"] != buckets.key(collection, identity)
        or not isinstance(identity, str)
        or not identity
        or not isinstance(row["sort_key"], str)
        or not row["sort_key"]
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        corrupt()
    return (collection, identity), (row["sort_key"], digest)


class Directory:
    """Same-read bucket proofs only; never a cache across transactions or scopes."""

    def __init__(self, repository, scope_key, payload):
        self.repository, self.scope_key = repository, scope_key
        self.epoch = validate(payload)
        self.roots, self.loaded = payload["_buckets"], {}

    def load(self, keys):
        missing = set(keys) - self.loaded.keys()
        if not missing:
            return
        selected = {key: {} for key in missing}
        # The header binds each complete leaf set. Actual bodies are checked on
        # use; Reading alone publishes changed bodies and leaves atomically.
        # Even declared empty buckets are queried: an unexpected leaf is corruption.
        for row in self.repository.open_digest_buckets(self.scope_key, tuple(sorted(missing))):
            (collection, identity), value = _leaf(row, self.epoch)
            key = buckets.key(collection, identity)
            if key not in selected or identity in selected[key]:
                corrupt()
            selected[key][identity] = value
        for key, values in selected.items():
            collection, bucket = key.rsplit(":", 1)
            empty = buckets.describe({}, displays=collection == VOUCHERS)
            if buckets.describe(values, displays=collection == VOUCHERS) != self.roots[
                collection
            ].get(bucket, empty):
                corrupt()
        self.loaded.update(selected)

    def known(self, collection, identities):
        identities = set(identities)
        self.load(buckets.key(collection, identity) for identity in identities)
        return {
            identity
            for identity in identities
            if identity in self.loaded[buckets.key(collection, identity)]
        }

    def check(self, row):
        collection, identity = row["collection_key"], row["item_id"]
        key = buckets.key(collection, identity)
        self.load((key,))
        value = decoded(row, self.epoch)
        digest = loads_json(row["payload_json"])["digest"]
        if self.loaded[key].get(identity) != (row["sort_key"], digest):
            corrupt()
        return value

    def get(self, collection, identities, *, required=False):
        identities = tuple(dict.fromkeys(identities))
        expected = self.known(collection, identities)
        if required and expected != set(identities):
            corrupt()
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


def patch(summary, entries, deletes, directory, state):
    prepared = prepare_items(entries)
    deletes = set(deletes)
    changed = deletes | {(row[0], row[1]) for row in prepared.rows}
    keys = {buckets.key(*identity) for identity in changed}
    directory.load(keys)
    values = {key: dict(directory.loaded[key]) for key in keys}
    for collection, identity in deletes:
        values[buckets.key(collection, identity)].pop(identity, None)
    for row in prepared.rows:
        collection, identity, order = row[:3]
        values[buckets.key(collection, identity)][identity] = order, loads_json(row[-1])["digest"]
    roots = deepcopy(directory.roots)
    for key, selected in values.items():
        collection, bucket = key.rsplit(":", 1)
        if selected:
            roots[collection][bucket] = buckets.describe(selected, displays=collection == VOUCHERS)
        else:
            roots[collection].pop(bucket, None)
    removed = changed
    removed |= {(DIGESTS, leaf_id(*identity)) for identity in removed.copy()}
    return PreparedOpenPatch(
        seal(_summary(summary, roots), (*prepared.rows, *leaves(prepared.rows)), directory.epoch),
        tuple(sorted(removed)),
        state["ready_head"],
        state["content_digest"],
        directory.epoch,
    )
