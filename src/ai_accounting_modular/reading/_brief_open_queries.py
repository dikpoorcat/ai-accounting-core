"""Bounded range pages and exact filtering over Open's immutable indexes."""

from heapq import merge
from itertools import islice

from . import _brief_open_tree as blocks
from ._collections import _filters


class Queries:
    def _plan(self, collection, query):
        if collection not in blocks.PUBLIC:
            blocks.corrupt()
        _filters(query)
        total = self.payload["_collections"][collection]["count"]
        if query.item_id is not None:
            return total, "point", (), 1, {"item_id"}
        candidates = [(total, collection, ((),), set())]
        for field in blocks.FILTERS:
            values = query.categories if field == "category" else (getattr(query, field),)
            if not values or values == (None,):
                continue
            prefixes = tuple((collection, field, value) for value in values)
            count = sum(
                self.trees.trees["filters"].summary(self.roots["filters"], first=first, last=last)
                for first, last in (blocks.bounds(*prefix) for prefix in prefixes)
            )
            candidates.append((count, "filters", prefixes, {field}))
        count, role, prefixes, covered = min(candidates, key=lambda candidate: candidate[0])
        return total, role, prefixes, count, covered

    @staticmethod
    def _matches(row, query):
        return all(
            getattr(query, field) is None or row[field] == getattr(query, field)
            for field in ("business_id", "object_id", "group_key", "item_id")
        ) and (not query.categories or row["category"] in query.categories)

    @staticmethod
    def _fields(query):
        return {
            field
            for field in ("business_id", "object_id", "group_key")
            if getattr(query, field) is not None
        } | ({"category"} if query.categories else set())

    def _scan(self, role, prefix, after, batch_size):
        tree, root = self.trees.trees[role], self.roots[role]
        first, last = blocks.bounds(*prefix) if prefix else (None, None)
        cursor = blocks.key(*prefix, *after) if after is not None else None
        while True:
            selected = tree.scan(root, first=first, last=last, after=cursor, limit=batch_size)
            for entry in selected:
                values = blocks.parts(entry.key)
                if values[: len(prefix)] != prefix or len(values) != len(prefix) + 2:
                    blocks.corrupt()
                yield (values[-2], values[-1]), entry, prefix
            if len(selected) < batch_size:
                return
            cursor = selected[-1].key

    def _matching(self, collection, query, after, batch_size):
        _, role, prefixes, count, _ = self._plan(collection, query)
        if role == "point":
            for row in self.item_get(collection, (query.item_id,)):
                if self._matches(row, query) and (
                    after is None or (row["sort_key"], row["item_id"]) > after
                ):
                    yield row
            return
        if not count:
            return
        stream = merge(
            *(self._scan(role, prefix, after, batch_size) for prefix in prefixes),
            key=lambda value: value[0],
        )
        while True:
            candidates = tuple(islice(stream, batch_size))
            if not candidates:
                break
            entries = (
                tuple(entry for _, entry, _ in candidates)
                if role == collection
                else self.trees.trees[collection].get_many(
                    self.roots[collection],
                    tuple(blocks.key(*order) for order, _, _ in candidates),
                )
            )
            values = self._ordered(collection, entries)
            for (order, entry, prefix), (row, _, actual_digest) in zip(
                candidates, values, strict=True
            ):
                if (
                    (row["sort_key"], row["item_id"]) != order
                    or role == "filters"
                    and actual_digest != entry.value
                    or prefix
                    and row[prefix[1]] != prefix[2]
                ):
                    blocks.corrupt()
                if self._matches(row, query):
                    yield row

    def item_counts(self, collection, query):
        total, role, _, count, covered = self._plan(collection, query)
        if role != "point" and not (self._fields(query) - covered):
            return total, count
        return total, sum(1 for _ in self._matching(collection, query, None, 128))

    def item_page(self, collection, query, after, limit):
        return tuple(islice(self._matching(collection, query, after, min(limit, 128)), limit))
