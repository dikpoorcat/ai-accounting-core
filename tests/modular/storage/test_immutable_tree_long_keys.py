"""Indivisible radix boundaries may be oversized without weakening their proof."""

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.storage import _immutable_tree as module
from ai_accounting_modular.storage import _immutable_tree_store as storage
from ai_accounting_modular.storage.public import Entry
from tests.modular.storage.test_immutable_tree import NODES, assemble


def long_entries():
    return tuple(
        Entry(letter + "甲" * 200, {"n": index}) for index, letter in enumerate("abcdefghi")
    )


def test_long_boundary_full_patch_restart_scan_and_shared_root_reclamation():
    reader, store, tree = assemble(max_items=1, max_bytes=1024)
    entries = long_entries()
    original = tree.full(entries)
    raw = tree._loaded[original.digest].payload_json
    node = loads_json(raw)
    assert node["kind"] == "branch" and node["oversize"]
    assert len(node["children"]) == 9 and len(raw.encode("utf-8")) > 1024
    reader.apply(store.finish((), (original.digest, original.digest)))

    _, store, fresh = assemble(reader, max_items=1, max_bytes=1024)
    assert tuple(fresh.entries(original)) == entries
    assert fresh.get_many(original, (entries[0].key, "missing", entries[-1].key)) == (
        entries[0],
        None,
        entries[-1],
    )
    assert fresh.scan(original, after=entries[1].key, last=entries[-1].key, limit=3) == entries[2:5]
    revised = Entry(entries[-1].key, {"n": -9})
    changes = {entry.key: None for entry in entries[:3]}
    changes[revised.key] = revised
    changed = fresh.patch(original, changes)
    expected = (*entries[3:-1], revised)
    assert changed == fresh.full(expected)
    reader.apply(
        store.finish((original.digest, original.digest), (original.digest, changed.digest))
    )

    _, store, restarted = assemble(reader, max_items=1, max_bytes=1024)
    assert tuple(restarted.entries(original)) == entries
    assert tuple(restarted.entries(changed)) == expected
    survivor = restarted.patch(changed, {entry.key: None for entry in expected[:-1]})
    assert survivor == restarted.full((revised,))
    reader.apply(store.finish((original.digest, changed.digest), (survivor.digest,)))
    _, store, final = assemble(reader, max_items=1, max_bytes=1024)
    assert tuple(final.entries(survivor)) == (revised,)
    reader.apply(store.finish((survivor.digest,), ()))
    assert reader.rows == {}


@pytest.mark.parametrize("oversize", [False, True], ids=["missing-marker", "false-marker"])
def test_branch_marker_is_required_exactly_for_its_false_envelope_size(oversize):
    _, store, tree = assemble(max_items=1, max_bytes=65536 if oversize else 1024)
    entries = (Entry("a", {"n": 1}), Entry("b", {"n": 2})) if oversize else long_entries()
    root = tree.full(entries)
    value = loads_json(tree._loaded[root.digest].payload_json) | {"oversize": oversize}
    receipt = module._BuiltNode(value)
    assert not receipt.capacity_ok
    with pytest.raises(ContractError):
        store._stage_built(receipt)
    with pytest.raises(ContractError):
        store.stage_node(receipt.payload_json, receipt.children)
    with pytest.raises(ContractError):
        module.structure(receipt.payload_json, receipt.identity)


@pytest.mark.parametrize("damage", ["fanout", "order", "route", "weight", "identity"])
def test_oversize_branch_keeps_all_strict_child_and_metadata_rules(damage):
    _, _, tree = assemble(max_items=1, max_bytes=1024)
    root = tree.full(long_entries())
    value = loads_json(tree._loaded[root.digest].payload_json)
    if damage == "fanout":
        value["children"] *= 2
    elif damage == "order":
        value["children"].reverse()
    elif damage == "route":
        # Keep intervals disjoint and all outer metadata intact, but let the
        # first child span the next radix symbol before its actual first key.
        value["children"][0][2] = "b"
        assert value["children"][0][2] < value["children"][1][1]
    elif damage == "weight":
        value["weight"] += 1
    else:
        value["children"][0][0] = "x" * 64
    with pytest.raises(ContractError):
        module._BuiltNode(value)
    raw = canonical_json(value)
    with pytest.raises(ContractError):
        module.structure(raw, module.digest(raw))


def test_branch_one_byte_marker_boundary_is_strict_but_never_cross_read_cached(monkeypatch):
    cache = storage._StructureCache()
    monkeypatch.setattr(storage, "_structure_cache", cache)
    entries = (Entry("a", {"n": 1}), Entry("b", {"n": 2}))
    for limit in range(200, 1000):
        reader, store, tree = assemble(max_items=1, max_bytes=limit)
        root = tree.full(entries)
        raw = tree._loaded[root.digest].payload_json
        value = loads_json(raw)
        false_size = len(canonical_json(value | {"oversize": False}).encode("utf-8"))
        if false_size != limit + 1:
            continue
        assert value["kind"] == "branch" and value["oversize"]
        assert len(raw.encode("utf-8")) == limit
        assert module.structure(raw, root.digest)[2]
        reader.apply(store.finish((), (root.digest,)))
        _, first_store, first_tree = assemble(reader, max_items=1, max_bytes=limit)
        assert first_tree.root(root.wire()) == root
        first = first_store.checked_nodes((root.digest,))[0]
        assert first_store.checked_nodes((root.digest,))[0] is first
        _, other_store, _ = assemble(reader, max_items=1, max_bytes=limit)
        assert other_store.checked_nodes((root.digest,))[0] is not first
        assert (root.digest, reader.rows[NODES, root.digest]) not in cache._entries
        break
    else:
        pytest.fail("the one-byte branch marker boundary was not exercised")
