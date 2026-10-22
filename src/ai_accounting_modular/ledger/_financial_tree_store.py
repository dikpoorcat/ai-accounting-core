"""Plan finite financial block ownership changes; Reading performs the outer atomic CAS."""

from __future__ import annotations

import re
import sys
from collections import Counter, OrderedDict
from dataclasses import dataclass
from threading import Lock

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json

from ._financial_tree import _CheckedStructure, _identity, corrupt, digest

_LONG_DIGITS = re.compile(rf"[0-9]{{{sys.int_info.str_digits_check_threshold + 1},}}")


class _StructureCache:
    """Bound pure decoding by node count and original UTF-8 bytes, not RAM size."""

    def __init__(self, *, max_nodes=4096, max_bytes=16 * 1024 * 1024):
        self._max_nodes, self._max_bytes = max_nodes, max_bytes
        self._entries = OrderedDict()
        self._raw_bytes = 0
        self._lock = Lock()

    def get(self, raw, identity):
        if type(raw) is not str or not _identity(identity):
            corrupt()
        key = identity, raw
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                self._entries.move_to_end(key)
                return existing[0]
        checked = _CheckedStructure(raw, identity)
        size = len(raw.encode("utf-8"))
        # All admissible integers fit every legal interpreter digit policy.
        # A long digit string is conservatively left uncached as well.
        if checked._value["oversize"] or size > self._max_bytes or _LONG_DIGITS.search(raw):
            return checked
        with self._lock:
            existing = self._entries.get(key)
            if existing is not None:
                self._entries.move_to_end(key)
                return existing[0]
            self._entries[key] = checked, size
            self._raw_bytes += size
            while len(self._entries) > self._max_nodes or self._raw_bytes > self._max_bytes:
                _, (_, removed_size) = self._entries.popitem(last=False)
                self._raw_bytes -= removed_size
        return checked


_structure_cache = _StructureCache()


def _checked_structure(raw, identity):
    return _structure_cache.get(raw, identity)


@dataclass(frozen=True)
class BlockWrite:
    collection: str
    key: str
    payload_json: str


@dataclass(frozen=True)
class TreePlan:
    writes: tuple[BlockWrite, ...]
    deletes: tuple[tuple[str, str], ...]


class TreeStore:
    """One read's immutable-node staging and isolated reference-count plan.

    Root lists are multisets. Body-directory roots belong here just once; domain
    code maintains thin logical Source references rather than physical body IDs.
    Actual rows and selection stay within this read. Only strict parsing of the
    same immutable bytes may be reused across reads; role checks remain local.
    No SQL, persisted work log or scan of unrelated nodes exists.
    """

    NODES = "_financial_nodes"
    REFS = "_financial_node_refs"
    REF_FORMAT = "ledger-financial-tree-ref/1"

    def __init__(self, reader):
        self.reader = reader
        self._base, self._staged, self._edges = {}, {}, {}
        self._checked = {}

    def _read(self, keys):
        keys = tuple(keys)
        needed = tuple(dict.fromkeys(key for key in keys if key not in self._base))
        for offset in range(0, len(needed), 500):
            requested = needed[offset : offset + 500]
            actual = self.reader.get_blocks(requested)
            if len(actual) != len(requested) or any(
                value is not None and type(value) is not str for value in actual
            ):
                corrupt()
            self._base.update(zip(requested, actual, strict=True))
        return tuple(self._base[key] for key in keys)

    def checked_nodes(self, identities):
        identities = tuple(identities)
        if any(not _identity(identity) for identity in identities):
            corrupt()
        missing = tuple(identity for identity in identities if identity not in self._staged)
        actual = dict(
            zip(missing, self._read((self.NODES, identity) for identity in missing), strict=True)
        )
        result = []
        for identity in identities:
            raw = self._staged[identity] if identity in self._staged else actual[identity]
            result.append(None if raw is None else self._check(raw, identity))
        return tuple(result)

    def stage_node(self, raw, children):
        identity = digest(raw)
        children = tuple(children)
        checked = self._check(raw, identity)
        if children != tuple(ref.digest for ref in checked.children):
            corrupt()
        if identity in self._staged and self._staged[identity] != raw:
            corrupt()
        self._staged[identity], self._edges[identity] = raw, children
        return identity

    def _check(self, raw, identity):
        if type(raw) is not str or not _identity(identity):
            corrupt()
        checked = self._checked.get(identity)
        if checked is not None:
            if checked.payload_json != raw:
                corrupt()
            return checked
        checked = _checked_structure(raw, identity)
        # A read's staging and GC must not depend on cross-read LRU capacity.
        # Long integer tokens still cannot reuse a more permissive parser policy.
        if not _LONG_DIGITS.search(raw):
            self._checked[identity] = checked
        return checked

    def _node(self, identity):
        raw = self._read(((self.NODES, identity),))[0]
        if raw is None:
            corrupt()
        return raw, tuple(child.digest for child in self._check(raw, identity).children)

    def _new_nodes(self, roots):
        pending, result, visited = tuple(roots), set(), set()
        while pending:
            wanted = tuple(
                dict.fromkeys(identity for identity in pending if identity not in visited)
            )
            visited.update(wanted)
            actual = self._read((self.NODES, identity) for identity in wanted)
            following = []
            for identity, raw in zip(wanted, actual, strict=True):
                if raw is not None:
                    self._check(raw, identity)
                    if identity in self._staged and self._staged[identity] != raw:
                        corrupt()
                else:
                    if identity not in self._staged:
                        corrupt()
                    result.add(identity)
                    following.extend(self._edges[identity])
            pending = tuple(following)
        return result

    def _counter(self, identity, *, new):
        raw = self._read(((self.REFS, identity),))[0]
        if new:
            if raw is not None:
                corrupt()
            return 0
        if raw is None:
            corrupt()
        try:
            value = loads_json(raw)
        except ContractError as error:
            raise ContractError("storage_corrupt", "财务派生树的引用计数正文不完整。") from error
        if (
            type(value) is not dict
            or set(value) != {"format", "node", "count"}
            or value["format"] != self.REF_FORMAT
            or value["node"] != identity
            or type(value["count"]) is not int
            or value["count"] < 1
            or canonical_json(value) != raw
        ):
            corrupt()
        return value["count"]

    def finish(self, old_roots, new_roots):
        """Attach the complete new root multiset before releasing any old root."""
        old_roots, new_roots = tuple(old_roots), tuple(new_roots)
        if any(not _identity(identity) for identity in (*old_roots, *new_roots)):
            corrupt()
        created = self._new_nodes(new_roots)
        self._read((self.NODES, identity) for identity in set(old_roots))
        for identity in set(old_roots):
            self._node(identity)
        touched = created | set(old_roots) | set(new_roots)
        touched.update(child for identity in created for child in self._edges[identity])
        self._read((self.REFS, identity) for identity in touched)
        initial, counts, removed = {}, {}, set()

        def count(identity):
            if identity not in counts:
                initial[identity] = self._counter(identity, new=identity in created)
                counts[identity] = initial[identity]
            return counts[identity]

        def increment(identity):
            counts[identity] = count(identity) + 1

        for identity, amount in Counter(old_roots).items():
            if count(identity) < amount:
                corrupt()
        # An immutable parent owns its child edges once, when first inserted.
        # Reusing that node from another parent or root never reattaches its children.
        for identity in created:
            count(identity)
            for child in self._edges[identity]:
                increment(child)
        for identity in new_roots:
            increment(identity)
        pending = tuple(old_roots)
        while pending:
            batch = Counter(pending)
            self._read((self.REFS, identity) for identity in batch)
            zero = []
            for identity, amount in batch.items():
                current = count(identity)
                if current < amount or identity in removed:
                    corrupt()
                counts[identity] = current - amount
                if current == amount:
                    removed.add(identity)
                    zero.append(identity)
            self._read((self.NODES, identity) for identity in zero)
            pending = tuple(child for identity in zero for child in self._node(identity)[1])
        if any(count(identity) < 1 or identity in removed for identity in created):
            corrupt()
        writes, deletes = [], []
        for identity in sorted(counts):
            final = counts[identity]
            if identity in created:
                writes.append(BlockWrite(self.NODES, identity, self._staged[identity]))
            if final:
                if final != initial[identity]:
                    writes.append(
                        BlockWrite(
                            self.REFS,
                            identity,
                            canonical_json(
                                {
                                    "format": self.REF_FORMAT,
                                    "node": identity,
                                    "count": final,
                                }
                            ),
                        )
                    )
            else:
                deletes.extend(((self.NODES, identity), (self.REFS, identity)))
        return TreePlan(tuple(writes), tuple(sorted(deletes)))
