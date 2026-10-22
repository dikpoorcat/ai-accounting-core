"""Actual transactional publication is separate from the pure owner block plan."""

import sqlite3
from collections import Counter
from contextlib import closing
from random import Random

import pytest

from ai_accounting_modular.contracts.public import canonical_json, loads_json
from ai_accounting_modular.storage._immutable_tree import structure
from ai_accounting_modular.storage.public import Entry
from tests.modular.storage.test_immutable_tree import NODES, REFS, Reader, assemble, data


def assert_graph(reader, roots):
    """Independent complete oracle, deliberately not used by the lazy product."""
    nodes = {
        key: structure(raw, key)[2]
        for (collection, key), raw in reader.rows.items()
        if collection == NODES
    }
    expected = Counter(root.digest for root in roots)
    for children in nodes.values():
        expected.update(child.digest for child in children)
    reachable, pending = set(), [root.digest for root in roots]
    while pending:
        identity = pending.pop()
        if identity not in reachable:
            reachable.add(identity)
            pending.extend(child.digest for child in nodes[identity])
    assert reachable == set(nodes) == set(expected)
    actual = {
        key: loads_json(raw)["count"]
        for (collection, key), raw in reader.rows.items()
        if collection == REFS
    }
    assert actual == dict(expected)


def test_multiple_roots_random_replacements_restart_and_gc_match_independent_graph():
    random = Random(20261021)
    reader, store, tree = assemble(max_items=8)
    values = {entry.key: entry for entry in data(80)}
    first = tree.full(values.values())
    roots = [first, first]
    reader.apply(store.finish((), tuple(root.digest for root in roots)))
    for round_number in range(15):
        # New instances prove no in-memory node/refcount state survives a read.
        _, store, tree = assemble(reader, max_items=8)
        old = list(roots)
        selected = random.randrange(len(roots))
        changes = {}
        for _ in range(4):
            key = f"same-prefix-甲{random.randrange(100):06d}"
            changes[key] = None if random.randrange(3) == 0 else Entry(key, {"n": round_number})
        changed = tree.patch(roots[selected], changes)
        roots[selected] = changed
        if round_number % 3 == 0:
            roots.append(changed)
        if round_number % 4 == 0 and len(roots) > 2:
            roots.pop(0)
        reader.apply(
            store.finish(tuple(root.digest for root in old), tuple(r.digest for r in roots))
        )
        assert_graph(reader, roots)
    _, store, _ = assemble(reader, max_items=8)
    reader.calls.clear()
    plan = store.finish(tuple(root.digest for root in roots), ())
    assert max(map(len, reader.calls)) <= 500
    assert len({key for batch in reader.calls for key in batch}) == sum(map(len, reader.calls))
    reader.apply(plan)
    assert reader.rows == {}


class SQLiteReader:
    def __init__(self, path):
        self.path = path
        self.calls = []

    def get_blocks(self, keys):
        self.calls.append(keys)
        with closing(sqlite3.connect(self.path)) as database:
            requested = ",".join("(?,?)" for _ in keys)
            found = dict(
                ((collection, key), payload)
                for collection, key, payload in database.execute(
                    "SELECT collection,key,payload FROM blocks "
                    f"WHERE (collection,key) IN ({requested})",
                    [part for key in keys for part in key],
                )
            )
            return tuple(found.get(key) for key in keys)

    @property
    def rows(self):
        with closing(sqlite3.connect(self.path)) as database:
            return {
                (collection, key): payload
                for collection, key, payload in database.execute("SELECT * FROM blocks")
            }

    def publish(self, plan, *, expected, roots, fail_after=None):
        database = sqlite3.connect(self.path)
        try:
            database.execute("PRAGMA synchronous=FULL")
            database.execute("BEGIN IMMEDIATE")
            actual = database.execute("SELECT revision FROM control").fetchone()[0]
            if actual != expected:
                raise ValueError("stale producer")
            actions = 0
            for collection, key in plan.deletes:
                database.execute(
                    "DELETE FROM blocks WHERE collection=? AND key=?", (collection, key)
                )
                actions += 1
                if actions == fail_after:
                    raise RuntimeError("injected publication failure")
            for write in plan.writes:
                database.execute(
                    "INSERT OR REPLACE INTO blocks VALUES(?,?,?)",
                    (write.collection, write.key, write.payload_json),
                )
                actions += 1
                if actions == fail_after:
                    raise RuntimeError("injected publication failure")
            database.execute(
                "UPDATE control SET revision=?,roots=?", (expected + 1, canonical_json(roots))
            )
            database.commit()
        except BaseException:
            database.rollback()
            raise
        finally:
            database.close()


def test_real_sqlite_restart_batch_reads_and_atomic_cas_failure(tmp_path):
    path = tmp_path / "tree.sqlite3"
    database = sqlite3.connect(path)
    database.execute("PRAGMA journal_mode=WAL")
    database.execute("PRAGMA synchronous=FULL")
    database.execute(
        "CREATE TABLE blocks(collection TEXT,key TEXT,payload TEXT,PRIMARY KEY(collection,key))"
    )
    database.execute("CREATE TABLE control(revision INTEGER,roots TEXT)")
    database.execute("INSERT INTO control VALUES(0,'[]')")
    database.commit()
    database.close()
    reader = SQLiteReader(path)
    _, store, tree = assemble(reader, max_items=8)
    root = tree.full(data(300))
    reader.publish(store.finish((), (root.digest,)), expected=0, roots=[root.wire()])
    before = reader.rows
    _, store, tree = assemble(SQLiteReader(path), max_items=8)
    loaded = tree.root(root.wire())
    changed = tree.patch(loaded, {loaded.first: None, "new": Entry("new", {"n": -7})})
    plan = store.finish((root.digest,), (changed.digest, changed.digest))
    for point in (1, len(plan.deletes) + 1, len(plan.deletes) + len(plan.writes)):
        with pytest.raises(RuntimeError, match="injected"):
            reader.publish(plan, expected=1, roots=[changed.wire()] * 2, fail_after=point)
        assert reader.rows == before
    with pytest.raises(ValueError, match="stale"):
        reader.publish(plan, expected=0, roots=[changed.wire()] * 2)
    assert reader.rows == before
    reader.publish(plan, expected=1, roots=[changed.wire()] * 2)
    assert_graph(reader, [changed, changed])
    fresh = SQLiteReader(path)
    _, store, tree = assemble(fresh, max_items=8)
    assert tree.root(changed.wire()) == changed
    assert tree.get_many(changed, ("new", root.first))[0].value == {"n": -7}
    assert tree.get_many(changed, (root.first,)) == (None,)
    reader.publish(store.finish((changed.digest, changed.digest), ()), expected=2, roots=[])
    assert reader.rows == {}


def test_many_scope_roots_share_real_json_envelopes_and_release_without_full_scan():
    reader, store, tree = assemble(max_items=8)
    roots, root = [], None
    for month in range(1, 121):
        root = tree.patch(root, {f"month{month:03d}": Entry(f"month{month:03d}", {"n": month})})
        roots.append(root)
    roots.extend(roots[2::3])  # Forty quarterly aliases are real root occurrences.
    reader.apply(store.finish((), tuple(root.digest for root in roots)))
    assert len(roots) == 160
    assert_graph(reader, roots)
    actual_bytes = sum(len(value.encode("utf8")) for value in reader.rows.values())
    standalone_bytes = 0
    for root in roots:
        independent = Reader()
        _, single_store, single_tree = assemble(independent, max_items=8)
        loaded = single_tree.full(tree.entries(root))
        independent.apply(single_store.finish((), (loaded.digest,)))
        standalone_bytes += sum(len(value.encode("utf8")) for value in independent.rows.values())
    assert actual_bytes < standalone_bytes // 4
    _, cleanup, _ = assemble(reader, max_items=8)
    reader.apply(cleanup.finish(tuple(root.digest for root in roots), ()))
    assert reader.rows == {}
