"""Only exact immutable bytes share decoding; actual rows and business lifetimes do not."""

import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.storage import _immutable_tree as tree_module
from ai_accounting_modular.storage import _immutable_tree_store as module
from ai_accounting_modular.storage._immutable_tree import structure
from ai_accounting_modular.storage.public import Entry, Tree
from ai_accounting_modular.storage.public import block_digest as digest
from tests.modular.storage.test_immutable_tree import NODES, Reader, Summary, assemble, persisted


@pytest.fixture(autouse=True)
def codec(monkeypatch):
    cache = module._StructureCache()
    monkeypatch.setattr(module, "_structure_cache", cache)
    return cache


def stored_node(value=1, **capacity):
    reader, store, tree = assemble(**capacity)
    root = tree.full((Entry("甲🙂", {"n": value}),))
    reader.apply(store.finish((), (root.digest,)))
    return reader.rows[NODES, root.digest], root


def test_cross_store_hit_still_reads_actual_rows_and_independent_structure_still_decodes(
    monkeypatch,
):
    reader, root, values = persisted(3)
    monkeypatch.setattr(module, "_structure_cache", module._StructureCache())
    original, decoded = tree_module._structure, []

    def observed(raw, identity):
        decoded.append(identity)
        return original(raw, identity)

    monkeypatch.setattr(tree_module, "_structure", observed)
    for _ in range(2):
        reader.calls.clear()
        _, _, tree = assemble(reader)
        actual = tuple(tree.entries(root))
        assert actual == tuple(values) and reader.calls
        actual[0].value["n"] = 999
    assert decoded == [root.digest]
    raw = reader.rows[NODES, root.digest]
    detached, entries, _ = structure(raw, root.digest)
    detached["entries"][0][1]["n"] = 888
    entries[0].value["n"] = 777
    assert decoded == [root.digest, root.digest]
    _, _, fresh = assemble(reader)
    assert tuple(fresh.entries(root)) == tuple(values)
    assert decoded == [root.digest, root.digest]


def test_integer_policy_metadata_is_minted_only_by_strict_immutable_construction():
    raw, root = stored_node()
    checked = tree_module._CheckedStructure(raw, root.digest)
    assert checked.integer_policy_independent is True
    with pytest.raises(TypeError):
        tree_module._CheckedStructure(raw, root.digest, integer_policy_independent=True)
    with pytest.raises(FrozenInstanceError):
        checked.integer_policy_independent = False
    with pytest.raises(ValueError):
        replace(checked, integer_policy_independent=False)
    with pytest.raises(ContractError):
        tree_module._CheckedStructure(raw + " ", root.digest)


def test_policy_classification_runs_once_per_actual_strict_decode(monkeypatch):
    raw, root = stored_node()
    reader = Reader({(NODES, root.digest): raw})
    monkeypatch.setattr(module, "_structure_cache", module._StructureCache())
    original, calls = tree_module._LONG_DIGITS, []

    class Observed:
        def search(self, value):
            calls.append(value)
            return original.search(value)

    monkeypatch.setattr(tree_module, "_LONG_DIGITS", Observed())
    first = module.TreeStore(reader, namespace="_test")
    assert first.checked_nodes((root.digest,))[0].integer_policy_independent
    assert first.checked_nodes((root.digest,))[0].integer_policy_independent
    second = module.TreeStore(reader, namespace="_test")
    assert second.checked_nodes((root.digest,))[0].integer_policy_independent
    assert calls == [raw] and len(reader.calls) == 2
    structure(raw, root.digest)
    assert calls == [raw, raw]


def test_one_read_keeps_proofs_through_global_eviction_staging_publication_and_gc(monkeypatch):
    reader, root, values = persisted(30, max_items=4)
    monkeypatch.setattr(module, "_structure_cache", module._StructureCache(max_nodes=1))
    original, decoded = tree_module._structure, Counter()

    def observed(raw, identity):
        decoded[identity] += 1
        return original(raw, identity)

    monkeypatch.setattr(tree_module, "_structure", observed)
    _, store, tree = assemble(reader, max_items=4)
    assert tuple(tree.entries(root)) == tuple(values)
    # Keep the actual raw staging path under eviction, even though Tree now
    # constructs new nodes without decoding its own canonical output.
    for (collection, identity), raw in reader.rows.items():
        if collection == NODES:
            checked = store.checked_nodes((identity,))[0]
            store.stage_node(raw, (ref.digest for ref in checked.children))
    changed = tree.patch(root, {values[0].key: Entry(values[0].key, {"n": 999})})
    second = Tree(tree.domain, Summary(), store, max_items=4)
    assert second.get_many(changed, (values[0].key,))[0].value == {"n": 999}
    assert store.finish((root.digest,), (changed.digest,)).writes
    assert store.finish((root.digest,), ()).deletes
    assert len(decoded) > 1 and set(decoded.values()) == {1}
    reader.calls.clear()
    _, _, fresh = assemble(reader, max_items=4)
    assert tuple(fresh.entries(root)) == tuple(values)
    assert reader.calls and max(decoded.values()) == 2


def test_ordinary_oversize_proof_is_reused_only_within_its_actual_read(monkeypatch):
    raw, root = stored_node(max_bytes=128)
    reader = Reader({(NODES, root.digest): raw})
    original, decoded = tree_module._structure, []

    def observed(value, identity):
        decoded.append(identity)
        return original(value, identity)

    monkeypatch.setattr(tree_module, "_structure", observed)
    store = module.TreeStore(reader, namespace="_test")
    first = store.checked_nodes((root.digest,))[0]
    assert store.checked_nodes((root.digest,))[0] is first
    assert module.TreeStore(reader, namespace="_test").checked_nodes((root.digest,))[0] is not first
    assert decoded == [root.digest, root.digest]


@pytest.mark.parametrize(
    "damage", ["missing", "different_raw", "raw_subclass", "identity_subclass"]
)
def test_warm_codec_never_supplies_missing_or_changed_actual_bytes(codec, damage):
    raw, root = stored_node()
    assert codec.get(raw, root.digest).payload_json == raw

    class Text(str):
        pass

    if damage == "identity_subclass":
        with pytest.raises(ContractError):
            codec.get(raw, Text(root.digest))
        return
    actual = {NODES: raw}
    if damage == "missing":
        actual = {}
    elif damage == "different_raw":
        actual[NODES] = raw + " "
    else:
        with pytest.raises(ContractError):
            codec.get(Text(raw), root.digest)
        actual[NODES] = Text(raw)
    reader = Reader({(collection, root.digest): value for collection, value in actual.items()})
    _, store, tree = assemble(reader)
    if damage == "missing":
        assert store.checked_nodes((root.digest,)) == (None,)
    with pytest.raises(ContractError) as error:
        tree.root(root.wire())
    assert error.value.code == "storage_corrupt" and reader.calls


def test_codec_reuse_does_not_reuse_role_or_capacity_acceptance(codec):
    raw, root = stored_node()
    codec.get(raw, root.digest)
    reader = Reader({(NODES, root.digest): raw})

    class WrongSummary(Summary):
        def decode_summary(self, value):
            value["sum"] += 1
            return value

    for domain, strategy, capacity in (
        ("different", Summary(), 128),
        ("financial-test-summary/1", WrongSummary(), 128),
        ("financial-test-summary/1", Summary(), 4),
    ):
        tree = Tree(
            domain, strategy, module.TreeStore(reader, namespace="_test"), max_items=capacity
        )
        with pytest.raises(ContractError):
            tree.root(root.wire())
    _, _, correct = assemble(reader)
    assert correct.root(root.wire()) == root


def test_large_integer_nodes_never_reuse_a_more_permissive_parser_policy(codec):
    original = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(0)
        raw, root = stored_node(10**700)
        assert codec.get(raw, root.digest).payload_json == raw
        reader = Reader({(NODES, root.digest): raw})
        store = module.TreeStore(reader, namespace="_test")
        assert store.checked_nodes((root.digest,))[0].payload_json == raw
        sys.set_int_max_str_digits(640)
        with pytest.raises(ContractError) as error:
            store.checked_nodes((root.digest,))
        assert error.value.code == "storage_corrupt" and reader.calls
        with pytest.raises(ContractError):
            module.TreeStore(reader, namespace="_test").checked_nodes((root.digest,))
        sys.set_int_max_str_digits(0)
        assert codec.get(raw, root.digest).payload_json == raw
    finally:
        sys.set_int_max_str_digits(original)


@pytest.mark.parametrize("same_store", [False, True], ids=["new-store", "same-store"])
def test_parser_policy_aba_during_decode_cannot_admit_a_permissive_proof(monkeypatch, same_store):
    original_limit = sys.get_int_max_str_digits()
    threshold = sys.int_info.str_digits_check_threshold
    try:
        sys.set_int_max_str_digits(0)
        raw, root = stored_node(10**threshold)
        reader = Reader({(NODES, root.digest): raw})
        store = module.TreeStore(reader, namespace="_test")
        sys.set_int_max_str_digits(threshold)
        original, calls = tree_module._structure, []

        def switched(value, identity):
            calls.append(identity)
            if len(calls) > 1:
                return original(value, identity)
            sys.set_int_max_str_digits(0)
            try:
                return original(value, identity)
            finally:
                sys.set_int_max_str_digits(threshold)

        monkeypatch.setattr(tree_module, "_structure", switched)
        assert store.checked_nodes((root.digest,))[0].payload_json == raw
        following = store if same_store else module.TreeStore(reader, namespace="_test")
        with pytest.raises(ContractError):
            following.checked_nodes((root.digest,))
        assert calls == [root.digest, root.digest]
    finally:
        sys.set_int_max_str_digits(original_limit)


@pytest.mark.parametrize("digits", [640, 641])
@pytest.mark.parametrize("as_text", [False, True], ids=["integer", "digit-string"])
def test_minimum_integer_policy_is_a_conservative_admission_boundary(codec, digits, as_text):
    original = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(0)
        value = {"n": 1, "probe": "1" * digits if as_text else int("1" * digits)}
        _, store, tree = assemble()
        new_root = tree.full((Entry("甲🙂", value),))
        checked_raw = store._staged[new_root.digest]
        first = codec.get(checked_raw, new_root.digest)
        second = codec.get(checked_raw, new_root.digest)
        assert first.integer_policy_independent is (
            digits == sys.int_info.str_digits_check_threshold
        )
        assert (first is second) == (digits == sys.int_info.str_digits_check_threshold)
    finally:
        sys.set_int_max_str_digits(original)


@pytest.mark.parametrize("limit", ["nodes", "bytes"])
def test_cache_lru_enforces_both_limits_using_original_utf8_bytes(limit):
    nodes = [stored_node(value)[0] for value in range(3)]
    sizes = [len(raw.encode("utf-8")) for raw in nodes]
    capacity = max(sizes) * 2 if limit == "bytes" else 1024 * 1024
    cache = module._StructureCache(max_nodes=2 if limit == "nodes" else 10, max_bytes=capacity)
    first = cache.get(nodes[0], digest(nodes[0]))
    second = cache.get(nodes[1], digest(nodes[1]))
    assert cache.get(nodes[0], digest(nodes[0])) is first
    cache.get(nodes[2], digest(nodes[2]))
    assert cache.get(nodes[0], digest(nodes[0])) is first
    assert cache.get(nodes[1], digest(nodes[1])) is not second
    assert len(cache._entries) <= cache._max_nodes
    assert (
        cache._raw_bytes
        == sum(len(checked.payload_json.encode("utf-8")) for checked, _ in cache._entries.values())
        <= capacity
    )


@pytest.mark.parametrize("oversize", [False, True])
def test_nodes_outside_admission_limits_are_still_strictly_checked_each_time(oversize):
    raw, root = stored_node(1, max_bytes=128 if oversize else 65536)
    cache = module._StructureCache(max_bytes=len(raw.encode("utf-8")) - 1 if not oversize else 4096)
    first, second = cache.get(raw, root.digest), cache.get(raw, root.digest)
    assert first is not second and first.payload_json == second.payload_json == raw
    assert cache._raw_bytes == 0 and not cache._entries
    with pytest.raises(ContractError):
        cache.get(raw + " ", root.digest)


def test_concurrent_misses_decode_without_locking_and_admit_one_shared_proof(monkeypatch):
    raw, root = stored_node()
    cache, workers = module._StructureCache(), 6
    gate, original = Barrier(workers), tree_module._structure

    def decode(value, identity):
        gate.wait(timeout=5)
        return original(value, identity)

    monkeypatch.setattr(tree_module, "_structure", decode)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        results = tuple(executor.map(lambda _: cache.get(raw, root.digest), range(workers)))
    assert all(checked is results[0] for checked in results)
    assert len(cache._entries) == 1 and cache._raw_bytes == len(raw.encode("utf-8"))
    assert cache.get(raw, root.digest) is results[0]
