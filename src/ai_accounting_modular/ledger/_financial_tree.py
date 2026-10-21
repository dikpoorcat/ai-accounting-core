"""Ledger's canonical financial blocks; selection and arithmetic stay with their owner."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass, field, replace
from hashlib import sha256
from os.path import commonprefix
from typing import Any, Protocol

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json

from ._values import fail

FORMAT = "ledger-financial-tree/1"


def corrupt():
    fail("storage_corrupt", "财务派生树的正文、摘要、顺序或引用不完整。")


def digest(raw):
    return sha256(raw.encode("utf-8")).hexdigest()


def _identity(value):
    return type(value) is str and len(value) == 64 and all(c in "0123456789abcdef" for c in value)


def _route(key):
    # A nibble bounds fanout at 16 plus the exact-key terminal, even for Unicode
    # or deliberately clustered IDs. UTF-8 preserves scalar string ordering.
    return str.encode(key, "utf-8").hex()


def _text(value):
    if not isinstance(value, str):
        raise TypeError("financial tree keys require strings")
    str.encode(value, "utf-8")
    return str.__str__(value)


@dataclass(frozen=True, init=False)
class Entry:
    key: str
    _value: Any = field(repr=False)
    _weight: int = field(repr=False)

    def __init__(self, key, value):
        key, value = _text(key), deepcopy(value)
        raw = canonical_json([key, value])
        object.__setattr__(self, "key", key)
        object.__setattr__(self, "_value", value)
        object.__setattr__(self, "_weight", len(raw.encode("utf-8")))

    @property
    def value(self):
        return deepcopy(self._value)


@dataclass(frozen=True)
class TreeChild:
    digest: str
    first: str
    last: str
    count: int
    weight: int
    _summary: Any = field(repr=False)

    @property
    def summary(self):
        return deepcopy(self._summary)

    def wire(self):
        return [self.digest, self.first, self.last, self.count, self.weight]


@dataclass(frozen=True)
class _Ref:
    digest: str
    first: str
    last: str
    count: int
    weight: int

    def wire(self):
        return [self.digest, self.first, self.last, self.count, self.weight]


@dataclass(frozen=True)
class _Node:
    ref: TreeChild
    prefix: str
    entries: tuple[Entry, ...]
    children: tuple[_Ref, ...]
    payload_json: str


class SummaryStrategy(Protocol):
    """Leaf and branch folds must agree for the same ordered entry sequence."""

    def leaf(self, entries: tuple[Entry, ...]) -> Any: ...
    def branch(self, children: tuple[TreeChild, ...]) -> Any: ...
    def encode_summary(self, summary: Any) -> Any: ...
    def decode_summary(self, value: Any) -> Any: ...


def _ref(value):
    if (
        type(value) is not list
        or len(value) != 5
        or not _identity(value[0])
        or any(type(item) is not str for item in value[1:3])
        or value[1] > value[2]
        or type(value[3]) is not int
        or value[3] < 1
        or type(value[4]) is not int
        or value[4] < 0
    ):
        corrupt()
    return _Ref(*value)


def structure(raw, identity):
    """Check actual immutable bytes and graph metadata without financial formulas."""
    try:
        if not _identity(identity) or type(raw) is not str or digest(raw) != identity:
            corrupt()
        value = loads_json(raw)
        common = {
            "format",
            "domain",
            "max_items",
            "max_bytes",
            "kind",
            "prefix",
            "first",
            "last",
            "count",
            "weight",
            "summary",
            "oversize",
        }
        kind = value.get("kind") if type(value) is dict else None
        expected = common | ({"key_prefix", "entries"} if kind == "leaf" else {"children"})
        if (
            kind not in {"leaf", "branch"}
            or set(value) != expected
            or value["format"] != FORMAT
            or type(value["domain"]) is not str
            or not value["domain"]
            or any(
                type(value[key]) is not int or value[key] < 1
                for key in ("max_items", "max_bytes", "count")
            )
            or type(value["weight"]) is not int
            or value["weight"] < 0
            or type(value["first"]) is not str
            or type(value["last"]) is not str
            or value["first"] > value["last"]
            or value["prefix"] != commonprefix((_route(value["first"]), _route(value["last"])))
            or type(value["oversize"]) is not bool
            or canonical_json(value) != raw
        ):
            corrupt()
        too_large = len(raw.encode("utf-8")) > value["max_bytes"]
        if kind == "leaf":
            if (
                type(value["entries"]) is not list
                or len(value["entries"]) != value["count"]
                or value["count"] > value["max_items"]
                or value["key_prefix"] != commonprefix((value["first"], value["last"]))
                or value["oversize"]
                != (
                    len(canonical_json(value | {"oversize": False}).encode("utf-8"))
                    > value["max_bytes"]
                )
                or (too_large and value["count"] != 1)
            ):
                corrupt()
            entries = []
            for entry in value["entries"]:
                if type(entry) is not list or len(entry) != 2 or type(entry[0]) is not str:
                    corrupt()
                entries.append(Entry(value["key_prefix"] + entry[0], entry[1]))
            if (
                entries[0].key != value["first"]
                or entries[-1].key != value["last"]
                or any(
                    left.key >= right.key for left, right in zip(entries, entries[1:], strict=False)
                )
                or sum(entry._weight for entry in entries) != value["weight"]
            ):
                corrupt()
            children = ()
        else:
            if type(value["children"]) is not list or not 2 <= len(value["children"]) <= 17:
                corrupt()
            children = tuple(_ref(child) for child in value["children"])
            symbols = [
                _route(child.first)[len(value["prefix"]) : len(value["prefix"]) + 1]
                for child in children
            ]
            if (
                value["oversize"]
                or too_large
                or children[0].first != value["first"]
                or children[-1].last != value["last"]
                or sum(child.count for child in children) != value["count"]
                or sum(child.weight for child in children) != value["weight"]
                or any(
                    left.last >= right.first
                    for left, right in zip(children, children[1:], strict=False)
                )
                or len(set(symbols)) != len(symbols)
                or any(
                    not _route(child.first).startswith(value["prefix"])
                    or not _route(child.last).startswith(value["prefix"])
                    or _route(child.last)[len(value["prefix"]) : len(value["prefix"]) + 1] != symbol
                    for child, symbol in zip(children, symbols, strict=True)
                )
            ):
                corrupt()
            entries = []
        return value, tuple(entries), children
    except (ContractError, KeyError, TypeError, ValueError, UnicodeError, IndexError) as error:
        raise ContractError(
            "storage_corrupt", "财务派生树的正文、摘要、顺序或引用不完整。"
        ) from error


class Tree:
    """A fixed role's ordered tree, sharing actual content across requested scopes."""

    def __init__(self, domain, strategy: SummaryStrategy, store, *, max_items=128, max_bytes=65536):
        if (
            type(domain) is not str
            or not domain
            or any(type(value) is not int or value < 1 for value in (max_items, max_bytes))
        ):
            raise ValueError("a financial tree needs an explicit domain and positive capacities")
        self.domain, self.strategy, self.store = domain, strategy, store
        self.max_items, self.max_bytes = max_items, max_bytes
        self._loaded = {}

    def _decode(self, raw, identity):
        value, entries, children = structure(raw, identity)
        if (value["domain"], value["max_items"], value["max_bytes"]) != (
            self.domain,
            self.max_items,
            self.max_bytes,
        ):
            corrupt()
        try:
            summary = self.strategy.decode_summary(deepcopy(value["summary"]))
            if canonical_json(self.strategy.encode_summary(summary)) != canonical_json(
                value["summary"]
            ):
                corrupt()
        except (AssertionError, KeyError, TypeError, ValueError):
            corrupt()
        return _Node(
            TreeChild(
                identity,
                value["first"],
                value["last"],
                value["count"],
                value["weight"],
                deepcopy(summary),
            ),
            value["prefix"],
            entries,
            children,
            raw,
        )

    def _load(self, refs):
        refs = tuple(refs)
        needed = tuple(dict.fromkeys(ref.digest for ref in refs if ref.digest not in self._loaded))
        if needed:
            raws = self.store.read_nodes(needed)
            for identity, raw in zip(needed, raws, strict=True):
                if raw is None:
                    corrupt()
                self._loaded[identity] = self._decode(raw, identity)
        result = []
        for ref in refs:
            node = self._loaded[ref.digest]
            if any(
                type(getattr(ref, name)) is not type(getattr(node.ref, name))
                or getattr(ref, name) != getattr(node.ref, name)
                for name in ("digest", "first", "last", "count", "weight")
            ):
                corrupt()
            if isinstance(ref, TreeChild) and canonical_json(
                self.strategy.encode_summary(ref.summary)
            ) != canonical_json(self.strategy.encode_summary(node.ref.summary)):
                corrupt()
            result.append(node)
        return tuple(result)

    def _make(self, entries=(), children=()):
        entries, children = tuple(entries), tuple(children)
        if bool(entries) == bool(children):
            raise ValueError("a nonempty financial node is a leaf or a branch")
        first, last = (
            (entries[0].key, entries[-1].key) if entries else (children[0].first, children[-1].last)
        )
        count = len(entries) if entries else sum(child.count for child in children)
        weight = (
            sum(item._weight for item in entries)
            if entries
            else sum(child.weight for child in children)
        )
        summary = self.strategy.leaf(entries) if entries else self.strategy.branch(children)
        prefix = commonprefix((_route(first), _route(last)))
        value = {
            "format": FORMAT,
            "domain": self.domain,
            "max_items": self.max_items,
            "max_bytes": self.max_bytes,
            "kind": "leaf" if entries else "branch",
            "prefix": prefix,
            "first": first,
            "last": last,
            "count": count,
            "weight": weight,
            "summary": self.strategy.encode_summary(summary),
            "oversize": False,
        }
        if entries:
            key_prefix = commonprefix((first, last))
            value.update(
                key_prefix=key_prefix,
                entries=[[item.key[len(key_prefix) :], item._value] for item in entries],
            )
        else:
            value["children"] = [child.wire() for child in children]
        raw = canonical_json(value)
        if entries and count == 1 and len(raw.encode("utf-8")) > self.max_bytes:
            value["oversize"] = True
            raw = canonical_json(value)
        node = _Node(
            TreeChild(digest(raw), first, last, count, weight, deepcopy(summary)),
            prefix,
            entries,
            tuple(_Ref(*child.wire()) for child in children),
            raw,
        )
        return node

    def _stage(self, node):
        self.store.stage_node(node.payload_json, tuple(child.digest for child in node.children))
        self._loaded[node.ref.digest] = node

    def full(self, entries):
        values = tuple(entries)
        if any(not isinstance(item, Entry) for item in values):
            raise ValueError("financial entries must have unique exact keys")
        values = tuple(sorted(values, key=lambda item: item.key))
        if any(a.key == b.key for a, b in zip(values, values[1:], strict=False)):
            raise ValueError("financial entries must have unique exact keys")
        return self._full(values)

    def _full(self, entries):
        if not entries:
            return None
        if len(entries) <= self.max_items:
            leaf = self._make(entries)
            if len(entries) == 1 or len(leaf.payload_json.encode("utf-8")) <= self.max_bytes:
                self._stage(leaf)
                return leaf.ref
        prefix = commonprefix((_route(entries[0].key), _route(entries[-1].key)))
        groups = defaultdict(list)
        for item in entries:
            groups[_route(item.key)[len(prefix) : len(prefix) + 1]].append(item)
        children = tuple(self._full(values) for _, values in sorted(groups.items()))
        node = self._make(children=children)
        if len(node.payload_json.encode("utf-8")) > self.max_bytes:
            raise ValueError("financial branch metadata exceeds its declared byte capacity")
        self._stage(node)
        return node.ref

    def get_many(self, root, keys):
        keys = tuple(_text(key) for key in keys)
        if root is None or not keys:
            return tuple(None for _ in keys)
        pending = [(root, tuple(dict.fromkeys(keys)))]
        found = {}
        while pending:
            following = []
            for node, (_, wanted) in zip(
                self._load(ref for ref, _ in pending), pending, strict=True
            ):
                if node.entries:
                    actual = {item.key: item for item in node.entries}
                    found.update((key, actual.get(key)) for key in wanted)
                else:
                    grouped = defaultdict(list)
                    by_symbol = {
                        _route(child.first)[len(node.prefix) : len(node.prefix) + 1]: child
                        for child in node.children
                    }
                    for key in wanted:
                        child = by_symbol.get(_route(key)[len(node.prefix) : len(node.prefix) + 1])
                        if child is not None and child.first <= key <= child.last:
                            grouped[child].append(key)
                    following.extend(grouped.items())
            pending = following
        return tuple(found.get(key) for key in keys)

    def root(self, descriptor):
        """Recover a saved locator through the actual immutable root bytes."""
        return None if descriptor is None else self._load((_ref(descriptor),))[0].ref

    @staticmethod
    def _bounds(first, last):
        return (None if first is None else _text(first), None if last is None else _text(last))

    @staticmethod
    def _overlaps(ref, first, last):
        return (first is None or first <= ref.last) and (last is None or ref.first < last)

    def entries(self, root, *, first=None, last=None):
        """Read [first, last), skipping complete disjoint branches before loading."""
        first, last = self._bounds(first, last)
        if root is None or (first is not None and last is not None and first >= last):
            return iter(())
        pending, leaves = (root,), []
        while pending:
            nodes = tuple(
                node for node in self._load(pending) if self._overlaps(node.ref, first, last)
            )
            leaves.extend(node for node in nodes if node.entries)
            pending = tuple(
                child
                for node in nodes
                for child in node.children
                if self._overlaps(child, first, last)
            )
        return (
            item
            for node in sorted(leaves, key=lambda node: node.ref.first)
            for item in node.entries
            if (first is None or first <= item.key) and (last is None or item.key < last)
        )

    def summary(self, root, *, first=None, last=None):
        """Fold an exact range; temporary boundary summaries never become nodes."""
        first, last = self._bounds(first, last)
        if root is None or (first is not None and last is not None and first >= last):
            return deepcopy(self.strategy.leaf(()))
        pending, selected = (root,), []
        while pending:
            following = []
            for node in self._load(pending):
                if not self._overlaps(node.ref, first, last):
                    continue
                if (first is None or first <= node.ref.first) and (
                    last is None or node.ref.last < last
                ):
                    selected.append(node.ref)
                elif node.entries:
                    entries = tuple(
                        item
                        for item in node.entries
                        if (first is None or first <= item.key)
                        and (last is None or item.key < last)
                    )
                    if entries:
                        selected.append(
                            replace(
                                node.ref,
                                first=entries[0].key,
                                last=entries[-1].key,
                                count=len(entries),
                                weight=sum(item._weight for item in entries),
                                _summary=deepcopy(self.strategy.leaf(entries)),
                            )
                        )
                else:
                    following.extend(
                        child for child in node.children if self._overlaps(child, first, last)
                    )
            pending = tuple(following)
        selected.sort(key=lambda ref: ref.first)
        if len(selected) == 1:
            return selected[0].summary
        return deepcopy(
            self.strategy.branch(tuple(selected)) if selected else self.strategy.leaf(())
        )

    def patch(self, root, changes: Mapping[str, Entry | None]):
        if not isinstance(changes, Mapping) or any(
            type(key) is not str
            or (value is not None and (not isinstance(value, Entry) or value.key != key))
            for key, value in changes.items()
        ):
            raise TypeError("financial changes map exact keys to entries or deletion")
        return self._patch(root, dict(changes))

    def _patch(self, root, changes):
        if not changes:
            return root
        if root is None:
            return self.full(value for value in changes.values() if value is not None)
        node = self._load((root,))[0]
        if node.entries:
            values = {item.key: item for item in node.entries}
            for key, value in changes.items():
                if value is None:
                    values.pop(key, None)
                else:
                    values[key] = value
            return self.full(values.values())
        additions = [_route(key) for key, value in changes.items() if value is not None]
        prefix = commonprefix((_route(root.first), _route(root.last), *additions))
        if len(prefix) < len(node.prefix):
            children = {_route(root.first)[len(prefix) : len(prefix) + 1]: root}
        else:
            prefix = node.prefix
            # One frontier read obtains real decoded summaries, not copies in
            # every parent. Unchanged immutable children are reused afterwards.
            children = {
                _route(child.ref.first)[len(prefix) : len(prefix) + 1]: child.ref
                for child in self._load(node.children)
            }
        grouped = defaultdict(dict)
        for key, value in changes.items():
            route = _route(key)
            if route.startswith(prefix):
                grouped[route[len(prefix) : len(prefix) + 1]][key] = value
        for symbol, proposed in grouped.items():
            child = self._patch(children.get(symbol), proposed)
            if child is None:
                children.pop(symbol, None)
            else:
                children[symbol] = child
        ordered = tuple(value for _, value in sorted(children.items()))
        if not ordered:
            return None
        if len(ordered) == 1:
            return ordered[0]
        count = sum(child.count for child in ordered)
        key_prefix = commonprefix((ordered[0].first, ordered[-1].last))
        escaped_prefix = len(canonical_json(key_prefix).encode("utf-8")) - 2
        # Exact bytes of a merged entries array, before its nonempty envelope.
        # This excludes an impossible merge without loading giant body leaves;
        # a possibly fitting array is then resolved by the same full factory.
        entry_bytes = sum(child.weight for child in ordered) - count * escaped_prefix + count + 1
        if count <= self.max_items and entry_bytes <= self.max_bytes:
            return self.full(item for child in ordered for item in self.entries(child))
        result = self._make(children=ordered)
        if len(result.payload_json.encode("utf-8")) > self.max_bytes:
            raise ValueError("financial branch metadata exceeds its declared byte capacity")
        self._stage(result)
        return result.ref
