"""Immutable trees keep canonical content and bounded exact-path reads."""

from copy import deepcopy
from dataclasses import replace
from random import Random

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.storage._immutable_tree import structure
from ai_accounting_modular.storage.public import Entry, Tree, TreeStore
from ai_accounting_modular.storage.public import block_digest as digest

NODES = "_test_nodes"
REFS = "_test_node_refs"


class Summary:
    def leaf(self, entries):
        return {"sum": sum(item.value["n"] for item in entries)}

    def branch(self, children):
        return {"sum": sum(child.summary["sum"] for child in children)}

    def encode_summary(self, summary):
        return deepcopy(summary)

    def decode_summary(self, value):
        assert type(value) is dict and set(value) == {"sum"} and type(value["sum"]) is int
        return deepcopy(value)


class Reader:
    def __init__(self, rows=None):
        self.rows = {} if rows is None else dict(rows)
        self.calls = []

    def get_blocks(self, keys):
        self.calls.append(keys)
        return tuple(self.rows.get(key) for key in keys)

    def apply(self, plan):
        proposed = dict(self.rows)
        for key in plan.deletes:
            assert key in proposed
            del proposed[key]
        for value in plan.writes:
            proposed[value.collection, value.key] = value.payload_json
        self.rows = proposed


def assemble(reader=None, **capacity):
    reader = Reader() if reader is None else reader
    store = TreeStore(reader, namespace="_test")
    return reader, store, Tree("financial-test-summary/1", Summary(), store, **capacity)


def data(count, *, prefix="same-prefix-甲"):
    return [Entry(prefix + f"{index:06d}", {"n": index}) for index in range(count)]


def persisted(count=1000, **capacity):
    reader, store, tree = assemble(**capacity)
    values = data(count)
    root = tree.full(values)
    reader.apply(store.finish((), (root.digest,)))
    return reader, root, values


def test_full_patch_is_canonical_and_never_assumes_uuid_distribution():
    reader, store, tree = assemble(max_items=8)
    values = data(1000)
    first = tree.full(reversed(values))
    changed = {values[10].key: None, values[900].key: Entry(values[900].key, {"n": -5})}
    changed["a\x00甲"] = Entry("a\x00甲", {"n": 9})
    changed["😄"] = Entry("😄", {"n": 7})
    actual = tree.patch(first, changed)
    expected = {item.key: item for item in values}
    for key, item in changed.items():
        if item is None:
            expected.pop(key, None)
        else:
            expected[key] = item
    rebuilt = tree.full(expected.values())
    assert actual == rebuilt
    assert actual.summary == {"sum": sum(item.value["n"] for item in expected.values())}
    assert [item.key for item in tree.entries(actual)] == sorted(expected)
    assert tree.patch(actual, {"not-in-tree": None}) == actual
    reader.apply(store.finish((), (actual.digest,)))
    assert reader.rows


def test_fresh_loader_get_many_reads_only_exact_frontiers_once_in_batches():
    reader, root, values = persisted(4000, max_items=16)
    reader.calls.clear()
    _, store, tree = assemble(reader, max_items=16)
    wanted = [values[index].key for index in (1, 2, 3, 3998, 1)] + ["absent"]
    actual = tree.get_many(root, wanted)
    assert [item.value if item else None for item in actual] == [
        {"n": value} for value in (1, 2, 3, 3998, 1)
    ] + [None]
    keys = [key for batch in reader.calls for key in batch]
    assert len(keys) == len(set(keys))
    assert max(map(len, reader.calls)) <= 500
    assert len(keys) < 40 and len(keys) < len(reader.rows) // 4
    assert store.finish((root.digest,), (root.digest,)).writes == ()


def test_root_and_loaded_values_are_isolated_from_mutable_aliases():
    reader, store, tree = assemble()
    value = {"n": 5, "details": ["original"]}
    entry = Entry("甲", value)
    value["details"].append("caller changed")
    root = tree.full((entry,))
    selected = tree.get_many(root, ("甲",))[0]
    selected.value["details"].append("returned value changed")
    root.summary["sum"] = 999
    assert tree.get_many(root, ("甲",))[0].value == {"n": 5, "details": ["original"]}
    assert root.summary == {"sum": 5}
    reader.apply(store.finish((), (root.digest,)))
    _, _, fresh = assemble(reader)
    assert fresh.get_many(root, ("甲",))[0].value == selected.value


def test_actual_json_byte_capacity_and_oversize_singleton_preserve_real_values():
    reader, store, tree = assemble(max_items=8, max_bytes=8192)
    values = [Entry(str(index), {"n": index, "text": "甲" * 1800}) for index in range(10)]
    values.append(Entry("huge", {"n": 10, "text": "😄" * 40000}))
    root = tree.full(values)
    reader.apply(store.finish((), (root.digest,)))
    seen_oversize = False
    for (_, _), raw in reader.rows.items():
        node = loads_json(raw)
        if node.get("format") != "ai-accounting-immutable-tree/1":
            continue
        if len(raw.encode("utf8")) > 8192:
            assert node["kind"] == "leaf" and node["count"] == 1 and node["oversize"]
            seen_oversize = True
    assert seen_oversize
    _, _, fresh = assemble(reader, max_items=8, max_bytes=8192)
    assert [item.value for item in fresh.entries(root)] == [
        item.value for item in sorted(values, key=lambda item: item.key)
    ]


@pytest.mark.parametrize("damage", ["missing", "json", "hash", "domain", "capacity", "metadata"])
def test_used_node_damage_is_never_treated_as_empty(damage):
    reader, root, values = persisted()
    key = (NODES, root.digest)
    if damage == "missing":
        del reader.rows[key]
    elif damage == "json":
        reader.rows[key] = "{"
    elif damage == "hash":
        reader.rows[key] = reader.rows[key] + " "
    else:
        node = loads_json(reader.rows[key])
        field = {"domain": "domain", "capacity": "max_items", "metadata": "count"}[damage]
        node[field] = "other" if damage == "domain" else 999
        reader.rows[key] = canonical_json(node)
    _, _, tree = assemble(reader)
    with pytest.raises(ContractError) as error:
        tree.get_many(root, (values[0].key,))
    assert error.value.code == "storage_corrupt"


def test_root_metadata_bool_and_wrong_summary_do_not_bypass_actual_node():
    reader, root, values = persisted()
    _, _, tree = assemble(reader)
    for wrong in (replace(root, count=True), replace(root, first="different")):
        with pytest.raises(ContractError, match="不可变"):
            tree.get_many(wrong, (values[0].key,))
    reader, store, tree = assemble()
    one = tree.full((Entry("a", {"n": 1}),))
    reader.apply(store.finish((), (one.digest,)))
    _, _, tree = assemble(reader)
    with pytest.raises(ContractError):
        tree.get_many(replace(one, _summary={"sum": True}), ("a",))


def test_shared_root_counts_restart_and_finite_reclamation():
    reader, store, tree = assemble(max_items=8)
    first = tree.full(data(300))
    second = tree.patch(first, {"different": Entry("different", {"n": 2})})
    reader.apply(store.finish((), (first.digest, first.digest, second.digest)))
    _, fresh_store, fresh_tree = assemble(reader, max_items=8)
    assert len(list(fresh_tree.entries(first))) == 300
    no_change = fresh_store.finish(
        (first.digest, first.digest, second.digest), (first.digest, first.digest, second.digest)
    )
    assert not no_change.writes and not no_change.deletes
    # Swapping names has the same root multiset. Unlinking two copies must not
    # free a child still referenced by second's physical parent.
    reader.apply(fresh_store.finish((first.digest, first.digest, second.digest), (second.digest,)))
    _, final_store, final_tree = assemble(reader, max_items=8)
    assert len(list(final_tree.entries(second))) == 301
    reader.apply(final_store.finish((second.digest,), ()))
    assert reader.rows == {}


def test_plan_contains_no_unrooted_stages_and_does_not_mutate_its_base():
    reader, root, values = persisted(100)
    before = dict(reader.rows)
    _, store, tree = assemble(reader)
    unused = tree.full(data(100, prefix="never published"))
    changed = tree.patch(root, {values[0].key: Entry(values[0].key, {"n": -1})})
    plan = store.finish((root.digest,), (changed.digest,))
    assert reader.rows == before
    assert (NODES, unused.digest) not in {(row.collection, row.key) for row in plan.writes}
    reader.apply(plan)
    _, cleanup, _ = assemble(reader)
    reader.apply(cleanup.finish((changed.digest,), ()))
    assert reader.rows == {}


def test_missing_reference_counter_is_corrupt_and_failed_plan_leaves_base_unchanged():
    reader, root, _ = persisted(50)
    del reader.rows[REFS, root.digest]
    before = dict(reader.rows)
    _, store, _ = assemble(reader)
    with pytest.raises(ContractError) as error:
        store.finish((root.digest,), ())
    assert error.value.code == "storage_corrupt" and reader.rows == before


def test_range_reads_and_summary_preserve_order_and_do_not_scan_disjoint_bodies():
    reader, root, values = persisted(4000, max_items=16)
    _, store, tree = assemble(reader, max_items=16)
    actual_root = tree.root(root.wire())
    assert actual_root == root and tree.root(None) is None
    reader.calls.clear()
    assert tree.summary(actual_root) == root.summary
    assert reader.calls == []
    bounds = values[101].key, values[135].key
    assert [item.key for item in tree.entries(root, first=bounds[0], last=bounds[1])] == [
        item.key for item in values[101:135]
    ]
    assert tree.summary(root, first=bounds[0], last=bounds[1]) == {"sum": sum(range(101, 135))}
    assert len({key for batch in reader.calls for key in batch}) < 25
    assert tree.summary(root, first="z", last="a") == {"sum": 0}
    assert list(tree.entries(root, first="z", last="a")) == []
    assert store.finish((root.digest,), (root.digest,)).writes == ()


def test_range_summary_uses_stored_subtree_fold_and_only_refolds_boundary_entries():
    reader, root, values = persisted(4000, max_items=16)

    class Observed(Summary):
        def __init__(self):
            self.refolded = []

        def leaf(self, entries):
            self.refolded.extend(item.key for item in entries)
            return super().leaf(entries)

    strategy = Observed()
    tree = Tree(
        "financial-test-summary/1", strategy, TreeStore(reader, namespace="_test"), max_items=16
    )
    assert tree.summary(root, first=values[101].key, last=values[3901].key) == {
        "sum": sum(range(101, 3901))
    }
    assert len(strategy.refolded) <= 32
    assert len(reader.calls) < 15
    assert tree.summary(None) == {"sum": 0}


def rehash_root(reader, root, mutate):
    value = loads_json(reader.rows[NODES, root.digest])
    mutate(value)
    raw = canonical_json(value)
    identity = digest(raw)
    reader.rows[NODES, identity] = raw
    return replace(
        root,
        digest=identity,
        first=value["first"],
        last=value["last"],
        count=value["count"],
        weight=value["weight"],
    )


@pytest.mark.parametrize(
    "mutate",
    [
        lambda node: node.update(domain="different-schema/2"),
        lambda node: node.update(max_items=127),
        lambda node: node.update(count=node["count"] + 1),
        lambda node: node.update(weight=node["weight"] + 1),
        lambda node: node.update(summary={"sum": True}),
        lambda node: node["children"].reverse(),
        lambda node: node["children"].append(node["children"][0]),
        lambda node: node.update(oversize=True),
    ],
)
def test_rehashed_metadata_and_summary_still_require_the_actual_tree_contract(mutate):
    reader, root, _ = persisted(1000)
    wrong = rehash_root(reader, root, mutate)
    _, _, tree = assemble(reader)
    with pytest.raises(ContractError) as error:
        tree.root(wrong.wire())
    assert error.value.code == "storage_corrupt"


def test_branch_child_cannot_span_another_radix_symbol_even_with_matching_outer_metadata():
    reader, root, _ = persisted(1000, max_items=8)
    raw = loads_json(reader.rows[NODES, root.digest])
    assert len(raw["children"]) > 2
    # Keep children disjoint and the parent's count/first/last intact, but make
    # the first child's advertised interval span an unused next radix symbol.
    raw["children"][0][2] = raw["children"][1][1][:-1] + "/"
    assert raw["children"][0][2] < raw["children"][1][1]
    changed = canonical_json(raw)
    with pytest.raises(ContractError):
        structure(changed, digest(changed))


def test_byte_splits_merges_and_oversize_singleton_have_identical_full_and_patch_roots():
    reader, store, tree = assemble(max_items=8, max_bytes=8192)
    prefix = '共享前缀"\\\n' * 20
    original = [Entry(prefix + str(index), {"n": index, "text": "甲" * 4000}) for index in range(6)]
    root = tree.full(original)
    reader.apply(store.finish((), (root.digest,)))
    _, store, tree = assemble(reader, max_items=8, max_bytes=8192)
    wanted = {item.key: item for item in original}
    for index in range(6):
        item = Entry(prefix + str(index), {"n": index, "text": "小" * index})
        root = tree.patch(root, {item.key: item})
        wanted[item.key] = item
        assert root == tree.full(wanted.values())
    huge = Entry(prefix + "0", {"n": 0, "text": "巨" * 10000})
    root = tree.patch(root, {huge.key: huge})
    wanted[huge.key] = huge
    assert root == tree.full(wanted.values())
    root = tree.patch(root, {key: None for key in tuple(wanted) if key != huge.key})
    assert root == tree.full((huge,))
    assert tree.patch(root, {huge.key: None}) is None


def test_impossible_giant_merge_never_adds_a_body_enumeration_pass():
    reader, store, tree = assemble(max_items=8, max_bytes=8192)
    values = [Entry(str(index), {"n": index, "text": "甲" * 20000}) for index in range(6)]
    root = tree.full(values)
    reader.apply(store.finish((), (root.digest,)))
    _, _, tree = assemble(reader, max_items=8, max_bytes=8192)
    # Child summaries are required once. Their enormous values must not be
    # repeatedly loaded by an attempted leaf merge at each ancestor.
    visited = []
    original_entries = tree.entries

    def observe(*args, **kwargs):
        visited.append(args[0].digest)
        return original_entries(*args, **kwargs)

    tree.entries = observe
    changed = tree.patch(root, {"0": Entry("0", {"n": -1, "text": "小"})})
    assert visited == []
    wanted = [Entry("0", {"n": -1, "text": "小"}), *values[1:]]
    assert changed == tree.full(wanted)


def test_json_null_is_an_entry_and_never_confused_with_deletion():
    class Nullable(Summary):
        def leaf(self, entries):
            return {"sum": sum(item.value is None for item in entries)}

    reader = Reader()
    store = TreeStore(reader, namespace="_test")
    tree = Tree("nullable/1", Nullable(), store)
    root = tree.full((Entry("a", None), Entry("b", {"n": 2})))
    assert tree.get_many(root, ("a", "missing"))[0].value is None
    assert tree.get_many(root, ("a", "missing"))[1] is None
    actual = tree.patch(root, {"b": None})
    assert actual == tree.full((Entry("a", None),))


def test_one_byte_oversize_marker_boundary_round_trips_without_a_circular_size_rule():
    entry = Entry("x", {"n": 1, "text": "甲" * 100})
    for limit in range(500, 1000):
        _, store, candidate = assemble(max_bytes=limit)
        root = candidate.full((entry,))
        raw = candidate._loaded[root.digest].payload_json
        value = loads_json(raw)
        normal_size = len(canonical_json(value | {"oversize": False}).encode("utf8"))
        if normal_size == limit + 1:
            assert value["oversize"] is True
            assert len(raw.encode("utf8")) == limit
            reader = Reader()
            reader.apply(store.finish((), (root.digest,)))
            _, _, fresh = assemble(reader, max_bytes=limit)
            assert fresh.root(root.wire()) == root
            break
    else:
        pytest.fail("the one-byte literal boundary was not exercised")


def test_random_byte_boundary_replacements_shrinks_and_deletions_match_cold_factory():
    random = Random(4112026)
    _, _, tree = assemble(max_items=8, max_bytes=8192)
    current, root = {}, None
    keys = ["", "a", "a\x00", "a\x00甲", 'a\\"', "甲", "😄"]
    keys.extend(f"共同前缀{index:03d}" for index in range(30))
    for round_number in range(100):
        key = random.choice(keys)
        item = (
            None
            if random.randrange(4) == 0
            else Entry(key, {"n": round_number, "text": "甲" * random.choice((0, 1, 600, 3000))})
        )
        root = tree.patch(root, {key: item})
        if item is None:
            current.pop(key, None)
        else:
            current[key] = item
        assert root == tree.full(current.values())


def test_disjoint_range_still_checks_the_actual_root_before_using_its_metadata():
    reader, root, _ = persisted(100)
    del reader.rows[NODES, root.digest]
    _, _, tree = assemble(reader)
    with pytest.raises(ContractError):
        list(tree.entries(root, first="z"))
    with pytest.raises(ContractError):
        tree.summary(root, first="z")
