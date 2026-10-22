"""Count summaries and publication frontiers use actual strictly checked nodes."""

import sys
from dataclasses import replace
from random import Random

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.storage import _immutable_tree as tree_module
from ai_accounting_modular.storage import _immutable_tree_store as store_module
from ai_accounting_modular.storage.public import Entry, Tree, TreeStore
from ai_accounting_modular.storage.public import block_digest as digest
from tests.modular.storage.test_immutable_tree import NODES, REFS, Reader, Summary
from tests.modular.storage.test_immutable_tree_store import assert_graph


class PlainCount:
    def leaf(self, entries):
        return len(entries)

    def branch(self, children):
        return sum(child.summary for child in children)

    def encode_summary(self, value):
        return self.decode_summary(value)

    def decode_summary(self, value):
        assert type(value) is int and value >= 0
        return value


class Count(PlainCount):
    def summary_from_count(self, count):
        return self.decode_summary(count)


def assemble(reader=None, strategy=None, *, max_items=1):
    reader = Reader() if reader is None else reader
    store = TreeStore(reader, namespace="_test")
    return (
        reader,
        store,
        Tree("test-count/1", Count() if strategy is None else strategy, store, max_items=max_items),
    )


def persisted(*, huge=False, max_items=1):
    values = [
        Entry(key, {"n": 10**700 if huge and key == "b" else n}) for n, key in enumerate("abc")
    ]
    reader, store, tree = assemble(max_items=max_items)
    root = tree.full(values)
    reader.apply(store.finish((), (root.digest,)))
    child = next(child for child in tree._loaded[root.digest].children if child.first == "b")
    return reader, root, child


@pytest.fixture(autouse=True)
def codec(monkeypatch):
    cache = store_module._StructureCache()
    monkeypatch.setattr(store_module, "_structure_cache", cache)
    return cache


def observe(monkeypatch):
    original, decoded = tree_module._structure, []

    def strict(raw, identity):
        decoded.append(identity)
        return original(raw, identity)

    monkeypatch.setattr(tree_module, "_structure", strict)
    return decoded


def test_count_patch_and_publication_share_strict_frontier_proofs(monkeypatch, codec):
    reader, root, child = persisted()
    decoded = observe(monkeypatch)
    _, store, tree = assemble(reader)
    changed = tree.patch(root, {"a": Entry("a", {"n": -1})})
    plan = store.finish((root.digest,), (changed.digest,))
    assert decoded.count(child.digest) == 1
    assert child.digest in tree._loaded and child.digest in store._checked
    assert any(key[0] == child.digest for key in codec._entries)
    assert (NODES, child.digest) in {key for batch in reader.calls for key in batch}
    reader.apply(plan)
    assert tree.get_many(changed, ("b",))[0].value == {"n": 1}
    assert decoded.count(child.digest) == 1
    assert_graph(reader, (changed,))


def test_strategy_without_hook_still_loads_actual_child_summaries(monkeypatch):
    reader, store, tree = assemble(strategy=Summary())
    root = tree.full(Entry(key, {"n": n}) for n, key in enumerate("abc"))
    reader.apply(store.finish((), (root.digest,)))
    children = tuple(tree._loaded[root.digest].children)
    decoded = observe(monkeypatch)
    _, _, tree = assemble(reader, Summary())
    changed = tree.patch(root, {"a": Entry("a", {"n": -1})})
    assert changed.summary == {"sum": 2}
    assert all(child.digest in decoded for child in children)


def test_count_patch_full_bytes_and_reference_graph_remain_identical():
    random = Random(20261022)
    reader, store, tree = assemble(max_items=4)
    values = {f"甲{n:03d}": Entry(f"甲{n:03d}", {"n": n}) for n in range(40)}
    root = tree.full(values.values())
    reader.apply(store.finish((), (root.digest,)))
    for turn in range(8):
        _, store, tree = assemble(reader, max_items=4)
        changes = {}
        for _ in range(5):
            key = f"甲{random.randrange(50):03d}"
            value = None if random.randrange(3) == 0 else Entry(key, {"n": turn})
            changes[key] = value
            if value is None:
                values.pop(key, None)
            else:
                values[key] = value
        changed = tree.patch(root, changes)
        reader.apply(store.finish((root.digest,), (changed.digest,)))
        expected_reader, expected_store, expected = assemble(max_items=4)
        rebuilt = expected.full(values.values())
        expected_reader.apply(expected_store.finish((), (rebuilt.digest,)))
        assert changed == rebuilt and reader.rows == expected_reader.rows
        assert_graph(reader, (changed,))
        root = changed


@pytest.mark.parametrize("hook", [None, False, 1, "count"])
def test_present_count_hook_must_be_callable(hook):
    strategy = PlainCount()
    strategy.summary_from_count = hook
    with pytest.raises(ValueError):
        assemble(strategy=strategy)


def test_count_hook_is_captured_once_and_fold_must_agree():
    strategy = Count()
    _, _, tree = assemble(strategy=strategy)
    strategy.summary_from_count = lambda count: count + 1
    assert tree.full((Entry("a", None),)).summary == 1
    _, store, inconsistent = assemble(strategy=strategy)
    with pytest.raises(ContractError) as error:
        inconsistent.full((Entry("a", None),))
    assert error.value.code == "storage_corrupt" and not store._staged


def test_branch_fold_cannot_mint_a_node_that_disagrees_with_count():
    class WrongBranch(Count):
        def branch(self, children):
            return super().branch(children) + 1

    _, _, tree = assemble(strategy=WrongBranch())
    with pytest.raises(ContractError):
        tree.full(Entry(key, None) for key in "abc")


def rebind_child(reader, root, child, *, field, value):
    """A self-hashed wrong child, with a matching parent link and physical refs."""
    body = loads_json(reader.rows.pop((NODES, child.digest)))
    body[field] = value
    raw = canonical_json(body)
    identity = digest(raw)
    reader.rows.pop((REFS, child.digest))
    reader.rows[NODES, identity] = raw
    reader.rows[REFS, identity] = canonical_json(
        {"format": TreeStore.REF_FORMAT, "node": identity, "count": 1}
    )
    parent = loads_json(reader.rows.pop((NODES, root.digest)))
    next(row for row in parent["children"] if row[0] == child.digest)[0] = identity
    raw = canonical_json(parent)
    reader.rows.pop((REFS, root.digest))
    root = replace(root, digest=digest(raw))
    reader.rows[NODES, root.digest] = raw
    reader.rows[REFS, root.digest] = canonical_json(
        {"format": TreeStore.REF_FORMAT, "node": root.digest, "count": 1}
    )
    return root, identity


@pytest.mark.parametrize(
    "field,value", [("summary", 2), ("domain", "foreign/1"), ("format", "old/1")]
)
def test_patch_frontier_rejects_inconsistent_unmodified_child(field, value):
    reader, root, child = persisted()
    root, _ = rebind_child(reader, root, child, field=field, value=value)
    _, store, tree = assemble(reader)
    before = dict(reader.rows)
    with pytest.raises(ContractError) as error:
        tree.patch(root, {"a": Entry("a", {"n": -1})})
    assert error.value.code == "storage_corrupt"
    assert not store._staged and reader.rows == before


@pytest.mark.parametrize(
    "field,value", [("summary", 2), ("domain", "foreign/1"), ("format", "old/1")]
)
def test_merging_retained_children_reads_their_actual_content(field, value):
    reader, root, child = persisted(max_items=2)
    root, _ = rebind_child(reader, root, child, field=field, value=value)
    _, _, tree = assemble(reader, max_items=2)
    with pytest.raises(ContractError) as error:
        tree.patch(root, {"a": None})
    assert error.value.code == "storage_corrupt"


def test_count_saved_root_summary_must_match_its_actual_count():
    reader, root, _ = persisted()
    value = loads_json(reader.rows[NODES, root.digest])
    value["summary"] += 1
    raw = canonical_json(value)
    reader.rows[NODES, digest(raw)] = raw
    _, _, tree = assemble(reader)
    with pytest.raises(ContractError):
        tree.root(replace(root, digest=digest(raw)).wire())


def stage_parent(reader, store, root):
    """Isolate Store's retained-edge checks from Tree's earlier role checks."""
    value = loads_json(reader.rows[NODES, root.digest])
    value["domain"] = "stored-parent/1"
    return store.stage_node(canonical_json(value), (child[0] for child in value["children"]))


@pytest.mark.parametrize("location", ["child_only", "root_first", "root_last"])
def test_existing_node_is_strict_as_child_or_explicit_root(location):
    reader, root, child = persisted()
    root, identity = rebind_child(reader, root, child, field="format", value="old/1")
    store = TreeStore(reader, namespace="_test")
    parent = stage_parent(reader, store, root)
    roots = (
        (parent,)
        if location == "child_only"
        else (identity, parent)
        if location == "root_first"
        else (parent, identity)
    )
    with pytest.raises(ContractError) as error:
        store.finish((root.digest,), roots)
    assert error.value.code == "storage_corrupt"
    with pytest.raises(ContractError):
        store.stage_node(reader.rows[NODES, identity], ())
    with pytest.raises(ContractError):
        TreeStore(reader, namespace="_test").finish((root.digest,), ())


@pytest.mark.parametrize("damage", ["missing", "hash", "type", "utf8", "staged"])
def test_publication_checks_actual_retained_bytes_and_staged_conflicts(damage, codec):
    reader, root, child = persisted()

    class Text(str):
        pass

    key = NODES, child.digest
    if damage == "missing":
        del reader.rows[key]
    elif damage == "hash":
        reader.rows[key] += " "
    elif damage == "type":
        reader.rows[key] = Text(reader.rows[key])
    elif damage == "utf8":
        reader.rows[key] = "\ud800"
    store = TreeStore(reader, namespace="_test")
    parent = stage_parent(reader, store, root)
    if damage == "staged":
        store._staged[child.digest] = "different staged bytes"
    with pytest.raises(ContractError) as error:
        store.finish((root.digest,), (parent,))
    assert error.value.code == "storage_corrupt"
    if damage == "staged":
        assert store._checked[child.digest].payload_json == reader.rows[key]
    else:
        assert child.digest not in store._checked
        assert all(key[0] != child.digest for key in codec._entries)


def test_current_integer_policy_is_checked_at_patch_publication_read_stage_and_gc():
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(0)
        reader, root, child = persisted(huge=True)
        sys.set_int_max_str_digits(sys.int_info.str_digits_check_threshold)
        _, store, tree = assemble(reader)
        before = dict(reader.rows)
        parent = stage_parent(reader, store, root)
        for operation in (
            lambda: tree.patch(root, {"a": Entry("a", {"n": -1})}),
            lambda: store.finish((root.digest,), (parent,)),
            lambda: tree.get_many(root, ("b",)),
            lambda: TreeStore(reader, namespace="_test").finish((), (child.digest,)),
            lambda: TreeStore(reader, namespace="_test").stage_node(
                reader.rows[NODES, child.digest], ()
            ),
            lambda: TreeStore(reader, namespace="_test").finish((root.digest,), ()),
        ):
            with pytest.raises(ContractError) as error:
                operation()
            assert error.value.code == "storage_corrupt" and reader.rows == before
    finally:
        sys.set_int_max_str_digits(previous)
