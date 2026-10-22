"""Same-read Activity bucket proofs, checked only when their values are actually used."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import ContractError

from . import _brief_activity_dependencies as dependencies
from . import _brief_activity_index as index
from . import _brief_activity_totals as totals
from . import _item_buckets as buckets
from ._collections import CollectionFilter, _read_item


class Directory:
    def __init__(self, repository, scope_key, payload):
        self.epoch = index.validate(payload, scope_key)
        self._bind(repository, scope_key, payload)

    @classmethod
    def from_row(cls, repository, state, row):
        from ._component_view import summary

        payload, epoch = summary(state["scope_key"], index.COMPONENT, state, row)
        directory = cls.__new__(cls)
        directory.epoch = epoch
        directory._bind(repository, state["scope_key"], payload)
        return directory

    def _bind(self, repository, scope_key, payload):
        self.repository, self.scope_key, self.payload = repository, scope_key, payload
        self.roots, self.loaded = payload["_buckets"], {}
        if repository.activity_bucket_count(scope_key) != sum(
            len(value) for value in self.roots.values()
        ):
            index.corrupt()
        for collection in index.MIRRORS:
            total, _ = repository.item_counts(
                scope_key, index.COMPONENT, collection, CollectionFilter()
            )
            if total != payload["_collections"][collection]["count"]:
                index.corrupt()

    def load(self, keys):
        missing = set(keys) - self.loaded.keys()
        if not missing:
            return
        selected = {}
        for row in self.repository.activity_bucket_rows(self.scope_key, tuple(sorted(missing))):
            key = row["item_id"]
            if (
                key not in missing
                or key in selected
                or row["scope_key"] != self.scope_key
                or row["component_key"] != index.COMPONENT
                or row["collection_key"] != index.BUCKETS
                or row["sort_key"] != key
                or row["head"] != self.epoch
                or any(
                    row[field] is not None
                    for field in ("business_id", "object_id", "group_key", "category")
                )
            ):
                index.corrupt()
            collection, bucket = key.rsplit(":", 1)
            expected = self.roots.get(collection, {}).get(bucket)
            if expected is None:
                index.corrupt()
            selected[key] = buckets.read(
                row["payload_json"],
                collection,
                bucket,
                expected,
                namespace=index.NAMESPACE,
                private=collection not in index.MIRRORS,
                keyed=collection in index.KEYS,
            )
        for key in missing - selected.keys():
            collection, bucket = key.rsplit(":", 1)
            if collection not in self.roots or bucket in self.roots[collection]:
                index.corrupt()
            selected[key] = {}
        self.loaded.update(selected)

    def get(self, collection, identities, *, required=False):
        try:
            return self._get(collection, identities, required=required)
        except (ContractError, KeyError, TypeError, ValueError) as error:
            raise ContractError(
                "storage_corrupt", "业务活动所选索引的内容或归属不完整。"
            ) from error

    def _get(self, collection, identities, *, required):
        identities = tuple(dict.fromkeys(identities))
        if collection not in self.roots:
            index.corrupt()
        self.load(buckets.key(collection, identity) for identity in identities)
        expected = {
            identity
            for identity in identities
            if identity in self.loaded[buckets.key(collection, identity)]
        }
        if required and expected != set(identities):
            index.corrupt()
        if collection in index.MIRRORS:
            return {
                item.item_id: item.value
                for item in self.items(collection, identities, expected=expected)
            }
        result = {
            identity: deepcopy(self.loaded[buckets.key(collection, identity)][identity])
            for identity in identities
            if identity in expected
        }
        for identity, value in result.items():
            if collection == index.ROOTS:
                dependencies.root(value)
                if value["business_id"] != identity:
                    index.corrupt()
            elif collection in index.KEYS:
                if type(value) is not dict or set(value) != {"roots"} or not value["roots"]:
                    index.corrupt()
                dependencies.ids(value["roots"], uuid=True)
            elif collection == totals.FOLDS:
                totals.validate_fold(value)
        return result

    def items(self, collection, identities, *, expected=None):
        identities = tuple(identities)
        if expected is None:
            self.load(buckets.key(collection, identity) for identity in identities)
            expected = {
                identity
                for identity in identities
                if identity in self.loaded[buckets.key(collection, identity)]
            }
        rows = (
            self.repository.item_get(self.scope_key, index.COMPONENT, collection, identities)
            if identities
            else ()
        )
        if {row["item_id"] for row in rows} != expected:
            index.corrupt()
        return self.checked(rows)

    def checked(self, rows):
        try:
            return self._checked(rows)
        except (ContractError, KeyError, TypeError, ValueError) as error:
            raise ContractError("storage_corrupt", "业务活动所选展示与其索引不一致。") from error

    def _checked(self, rows):
        self.load(buckets.key(row["collection_key"], row["item_id"]) for row in rows)
        items = []
        for row in rows:
            collection, identity = row["collection_key"], row["item_id"]
            if (
                collection not in index.MIRRORS
                or row["scope_key"] != self.scope_key
                or row["component_key"] != index.COMPONENT
                or row["head"] != self.epoch
            ):
                index.corrupt()
            item, _, digest = _read_item(row)
            if self.loaded[buckets.key(collection, identity)].get(identity) != (
                item.sort_key,
                digest,
            ):
                index.corrupt()
            items.append(item)
        owned = [
            item
            for item in items
            if item.collection in {"activity_members", "brief_vouchers", totals.CANDIDATES}
        ]
        roots = self.get(index.ROOTS, {item.business_id for item in owned}, required=True)
        for item in owned:
            if item.collection == totals.CANDIDATES:
                if (
                    item.item_id != totals.candidate_id(item.business_id, item.group_key)
                    or set(item.value) != {"member_id"}
                    or item.value["member_id"] not in roots[item.business_id]["members"]
                ):
                    index.corrupt()
            else:
                dependencies.owned(item, roots)
                if item.collection == "activity_members" and totals.member_item(item.value) != item:
                    index.corrupt()
        tokens = {
            item.value["row"]["voucher_version_id"]
            for item in items
            if item.collection == "activity_members" and item.value["row"]["voucher_version_id"]
        }
        if tokens:
            descriptors = {item.item_id: item for item in self.items("brief_vouchers", tokens)}
            if descriptors.keys() != tokens or any(
                descriptors[item.value["row"]["voucher_version_id"]].business_id != item.business_id
                for item in items
                if item.collection == "activity_members" and item.value["row"]["voucher_version_id"]
            ):
                index.corrupt()
        groups = [item for item in items if item.collection == "activity"]
        if groups:
            self._check_groups(groups)
        return tuple(items)

    def _check_groups(self, groups):
        folds = self.get(totals.FOLDS, (item.item_id for item in groups), required=True)
        candidates = {
            item.item_id: item
            for item in self.items(totals.CANDIDATES, {fold["first"][1] for fold in folds.values()})
        }
        members = self.get(
            "activity_members",
            {item.value["member_id"] for item in candidates.values()}
            | {fold["order"][1] for fold in folds.values()},
            required=True,
        )
        for item in groups:
            fold = folds[item.item_id]
            candidate = candidates.get(fold["first"][1])
            if candidate is None:
                index.corrupt()
            first, ordered = members[candidate.value["member_id"]], members[fold["order"][1]]
            if (
                [candidate.sort_key, candidate.item_id] != fold["first"]
                or candidate.group_key != item.item_id
                or totals.candidates((first,))[0] != candidate
                or ordered["row"]["group_key"] != item.item_id
                or [totals.member_item(ordered).sort_key, ordered["row"]["key"]] != fold["order"]
                or totals.group(first, fold, fold["order"][0]) != item
            ):
                index.corrupt()

    def check_many(self, rows):
        return tuple(item.value for item in self.checked(rows))

    def dependents(self, collection, identities):
        values = self.get(collection, identities)
        roots = self.get(
            index.ROOTS,
            {owner for value in values.values() for owner in value["roots"]},
            required=True,
        )
        for identity, value in values.items():
            for owner in value["roots"]:
                actual = (
                    {source["business_id"] for source in roots[owner]["sources"]}
                    if collection == index.BUSINESSES
                    else roots[owner]["object_ids"]
                )
                if identity not in actual:
                    index.corrupt()
        return set(roots)
