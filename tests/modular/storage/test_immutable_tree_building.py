"""Locally built bytes share strict envelope rules, never an Entry value graph."""

import sys
from collections import Counter
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields, replace
from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.storage import _immutable_tree as module
from ai_accounting_modular.storage import _immutable_tree_store as storage
from ai_accounting_modular.storage._immutable_tree import structure
from ai_accounting_modular.storage.public import Entry, Tree
from ai_accounting_modular.storage.public import block_digest as digest
from tests.modular.storage.test_immutable_tree import NODES, Summary, assemble, data


@pytest.fixture(autouse=True)
def isolated_codec(monkeypatch):
    cache = storage._StructureCache()
    monkeypatch.setattr(storage, "_structure_cache", cache)
    return cache


def test_build_patch_and_new_graph_publication_do_not_decode_their_own_bytes(
    monkeypatch, isolated_codec
):
    def decode(*args):
        raise AssertionError("new canonical nodes must not create a second decoded value graph")

    monkeypatch.setattr(module, "_structure", decode)
    reader, store, tree = assemble(max_items=4)
    root = tree.full(data(30))
    changed = tree.patch(root, {root.first: Entry(root.first, {"n": -10})})
    assert tree.get_many(changed, (root.first,))[0].value == {"n": -10}
    plan = store.finish((), (changed.digest,))
    assert plan.writes and not plan.deletes and reader.rows == {}
    assert not store._checked and not isolated_codec._entries


def test_receipt_is_normal_immutable_construction_without_a_retained_value_graph():
    _, _, tree = assemble()
    node = tree._make((Entry("a", {"n": 1, "nested": [False]}),))
    value = loads_json(node.payload_json)
    receipt = module._BuiltNode(value)
    assert {field.name for field in fields(receipt)} == {
        "payload_json",
        "identity",
        "children",
        "capacity_ok",
    }
    value["entries"][0][1]["nested"].append("changed input")
    assert receipt.payload_json == node.payload_json and receipt.identity == node.ref.digest
    with pytest.raises(FrozenInstanceError):
        receipt.capacity_ok = True
    with pytest.raises(TypeError):
        module._BuiltNode(value, capacity_ok=True)
    with pytest.raises(TypeError):
        replace(receipt, capacity_ok=True)
    _, store, _ = assemble()
    with pytest.raises(ContractError):
        store._stage_built(SimpleNamespace(**receipt.__dict__))


@pytest.mark.parametrize(
    "mutate",
    [
        lambda value: value.update(weight=value["weight"] + 1),
        lambda value: value.update(count=True),
        lambda value: value["entries"].reverse(),
        lambda value: value["entries"].append(value["entries"][0]),
        lambda value: value.update(prefix="wrong"),
        lambda value: value.update(max_items=True),
    ],
)
def test_builder_and_raw_share_the_same_metadata_weight_and_order_rules(mutate):
    _, _, tree = assemble()
    node = tree._make((Entry("a", {"n": 1}), Entry("b", {"n": 2})))
    value = loads_json(node.payload_json)
    mutate(value)
    raw = canonical_json(value)
    with pytest.raises(ContractError) as built_error:
        module._BuiltNode(value)
    with pytest.raises(ContractError) as raw_error:
        structure(raw, digest(raw))
    assert built_error.value.code == raw_error.value.code == "storage_corrupt"


def test_builder_does_not_trust_an_entries_declared_weight():
    _, _, tree = assemble()
    entry = Entry("a", {"n": 1})
    object.__setattr__(entry, "_weight", entry._weight + 1)
    with pytest.raises(ContractError) as error:
        tree.full((entry,))
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("field", ["format", "kind", "prefix", "key_prefix"])
def test_structural_metadata_cannot_use_string_subclass_equality_to_hide_wrong_wire(field):
    class PretendingText(str):
        def __eq__(self, other):
            return True

        def __ne__(self, other):
            return False

    _, _, tree = assemble()
    value = loads_json(tree._make((Entry("a", {"n": 1}),)).payload_json)
    value[field] = PretendingText("not the declared wire value")
    with pytest.raises(ContractError):
        module._BuiltNode(value)
    raw = canonical_json(value)
    with pytest.raises(ContractError):
        structure(raw, digest(raw))


@pytest.mark.parametrize("damage", ["order", "count", "identity"])
def test_built_branches_use_the_same_strict_child_rules(damage):
    _, _, tree = assemble(max_items=1)
    root = tree.full(data(3))
    value = loads_json(tree._loaded[root.digest].payload_json)
    if damage == "order":
        value["children"].reverse()
    elif damage == "count":
        value["children"][0][3] = True
    else:
        value["children"][0][0] = "x" * 64
    with pytest.raises(ContractError):
        module._BuiltNode(value)
    raw = canonical_json(value)
    with pytest.raises(ContractError):
        structure(raw, digest(raw))


def test_over_capacity_candidate_is_splittable_but_never_stageable_or_readable():
    reader, store, tree = assemble(max_bytes=2000)
    entries = tuple(Entry(key, {"n": 1, "text": "甲" * 400}) for key in ("a", "b"))
    candidate = tree._make(entries)
    assert not candidate.built.capacity_ok
    with pytest.raises(ContractError):
        tree._stage(candidate)
    with pytest.raises(ContractError):
        store.stage_node(candidate.payload_json, ())
    with pytest.raises(ContractError):
        structure(candidate.payload_json, candidate.ref.digest)
    root = tree.full(entries)
    reader.apply(store.finish((), (root.digest,)))
    _, _, fresh = assemble(reader, max_bytes=2000)
    assert tuple(fresh.entries(root)) == entries


@pytest.mark.parametrize("change", [{"max_items": 1}, {"max_bytes": 1}, {"oversize": True}])
def test_self_computed_capacity_cannot_be_bypassed_by_either_staging_path(change):
    _, store, tree = assemble()
    node = tree._make((Entry("a", {"n": 1}), Entry("b", {"n": 2})))
    value = loads_json(node.payload_json) | change
    receipt = module._BuiltNode(value)
    assert not receipt.capacity_ok
    with pytest.raises(ContractError):
        store._stage_built(receipt)
    with pytest.raises(ContractError):
        store.stage_node(receipt.payload_json, receipt.children)


def test_runtime_node_cannot_rebind_a_receipt_to_other_raw_or_children():
    _, _, tree = assemble()
    first = tree._make((Entry("a", {"n": 1}),))
    other = tree._make((Entry("b", {"n": 2}),))
    for changed in (
        replace(first, built=other.built),
        replace(first, payload_json=other.payload_json),
        replace(first, ref=other.ref),
        replace(first, children=(module._Ref(*other.ref.wire()),)),
        replace(first, built=None),
    ):
        with pytest.raises(ContractError):
            tree._stage(changed)


def test_receipt_keeps_the_singleton_true_envelope_one_byte_boundary():
    entry = Entry("x", {"n": 1, "text": "甲" * 100})
    for limit in range(500, 1000):
        _, store, tree = assemble(max_bytes=limit)
        node = tree._make((entry,))
        value = loads_json(node.payload_json)
        if len(canonical_json(value | {"oversize": False}).encode("utf8")) == limit + 1:
            assert value["oversize"] and len(node.payload_json.encode("utf8")) == limit
            assert node.built.capacity_ok
            tree._stage(node)
            assert structure(node.payload_json, node.ref.digest)[1] == (entry,)
            assert store.finish((), (node.ref.digest,)).writes
            break
    else:
        pytest.fail("the one-byte singleton marker boundary was not reached")


def test_own_tree_keeps_entry_aliases_but_other_tree_decodes_staged_actual_bytes(monkeypatch):
    class Text(str):
        pass

    shared = [False]
    source = {"n": 1, "text": Text("甲🙂"), "shared": shared, "again": shared}
    source["text"].notes = shared
    source["text"].parent = source
    entry = Entry("a", source)
    _, store, own = assemble()
    original, decoded = module._structure, []

    def observe(raw, identity):
        decoded.append(identity)
        return original(raw, identity)

    monkeypatch.setattr(module, "_structure", observe)
    root = own.full((entry,))
    local = own.get_many(root, (entry.key,))[0].value
    assert local["shared"] is local["again"] is local["text"].notes
    assert local["text"].parent is local and not decoded and not store._checked
    other = Tree(own.domain, Summary(), store)
    read_back = other.get_many(root, (entry.key,))[0].value
    assert decoded == [root.digest] and type(read_back["text"]) is str
    assert read_back["shared"] is not read_back["again"]
    local["shared"].append("changed return")
    read_back["shared"].append("changed other return")
    assert own.get_many(root, (entry.key,))[0].value["shared"] == [False]
    assert other.get_many(root, (entry.key,))[0].value["shared"] == [False]


def test_built_receipt_cannot_hide_existing_database_damage_or_gc_reads(monkeypatch):
    reader, store, tree = assemble()
    entry = Entry("a", {"n": 1})
    root = tree.full((entry,))
    reader.apply(store.finish((), (root.digest,)))
    original, decoded = module._structure, Counter()

    def observe(raw, identity):
        decoded[identity] += 1
        return original(raw, identity)

    monkeypatch.setattr(module, "_structure", observe)
    _, store, tree = assemble(reader)
    same = tree.full((entry,))
    assert not decoded and store.finish((root.digest,), (same.digest,)).writes == ()
    assert decoded[root.digest] == 1
    assert store.finish((root.digest,), ()).deletes and decoded[root.digest] == 1
    reader.rows[NODES, root.digest] += " "
    _, store, tree = assemble(reader)
    same = tree.full((entry,))
    with pytest.raises(ContractError):
        store.finish((root.digest,), (same.digest,))
    assert decoded[root.digest] == 2


def test_raw_stage_keeps_strict_parsing_and_exact_child_bindings(monkeypatch):
    _, _, tree = assemble(max_items=1)
    root = tree.full(data(3))
    node = tree._loaded[root.digest]
    original, decoded = module._structure, []

    def observe(raw, identity):
        decoded.append(identity)
        return original(raw, identity)

    monkeypatch.setattr(module, "_structure", observe)
    _, store, _ = assemble()
    with pytest.raises(ContractError):
        store.stage_node(node.payload_json, ())
    assert decoded == [root.digest]
    with pytest.raises(ContractError):
        store.stage_node(node.payload_json + " ", node.built.children)
    assert len(decoded) == 2


def test_built_long_integer_still_obeys_the_parser_policy_at_staging():
    previous = sys.get_int_max_str_digits()
    try:
        sys.set_int_max_str_digits(0)
        _, store, tree = assemble()
        node = tree._make((Entry("a", {"n": 10**700}),))
        sys.set_int_max_str_digits(sys.int_info.str_digits_check_threshold)
        with pytest.raises(ContractError) as error:
            tree._stage(node)
        assert error.value.code == "storage_corrupt" and not store._staged
        sys.set_int_max_str_digits(0)
        tree._stage(node)
        assert not store._checked and node.ref.digest in store._staged
    finally:
        sys.set_int_max_str_digits(previous)


def test_receipt_rejects_bad_json_instead_of_normalizing_it():
    _, _, tree = assemble()
    original = loads_json(tree._make((Entry("a", {"n": 1}),)).payload_json)
    for invalid in (1.0, float("nan"), (1, 2), {1: "wrong key"}, "\ud800"):
        value = deepcopy(original)
        value["entries"][0][1]["n"] = invalid
        with pytest.raises((ValueError, UnicodeError)):
            module._BuiltNode(value)
