"""Strict bytes may share codec proofs; rows and strategies remain independently checked."""

import sys
from collections import Counter
from dataclasses import FrozenInstanceError, replace
from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.storage import _immutable_tree as module
from ai_accounting_modular.storage import _immutable_tree_store as store_module
from ai_accounting_modular.storage._immutable_tree import structure
from ai_accounting_modular.storage.public import Entry, Tree, TreeStore
from ai_accounting_modular.storage.public import block_digest as digest
from tests.modular.storage.test_immutable_tree import NODES, Summary, assemble, persisted


@pytest.fixture(autouse=True)
def isolated_codec(monkeypatch):
    monkeypatch.setattr(store_module, "_structure_cache", store_module._StructureCache())


def test_loading_stored_entries_does_not_revalidate_them_as_external_inputs(monkeypatch):
    reader, root, expected = persisted(30, max_items=4)

    def external_entry(*args):
        raise AssertionError("already checked JSON was passed through the input constructor")

    monkeypatch.setattr(Entry, "__init__", external_entry)
    _, store, tree = assemble(reader, max_items=4)
    assert tuple(tree.entries(tree.root(root.wire()))) == tuple(expected)
    assert store.finish((root.digest,), (root.digest,)).writes == ()


def test_each_actual_node_is_decoded_once_across_tree_reads_staging_and_gc(monkeypatch):
    reader, root, expected = persisted(30, max_items=4)
    original = module._structure
    observed = Counter()
    monkeypatch.setattr(store_module, "_structure_cache", store_module._StructureCache())

    def decode(raw, identity):
        observed[identity] += 1
        return original(raw, identity)

    monkeypatch.setattr(module, "_structure", decode)
    _, store, first = assemble(reader, max_items=4)
    second = Tree(first.domain, Summary(), store, max_items=4)
    assert tuple(first.entries(first.root(root.wire()))) == tuple(expected)
    assert tuple(second.entries(second.root(root.wire()))) == tuple(expected)
    # Re-staging the same real node and the reference-count planner must use
    # the same proof, rather than parse all entries for graph metadata again.
    for (collection, identity), raw in reader.rows.items():
        if collection == NODES:
            checked = store.checked_nodes((identity,))[0]
            store.stage_node(raw, (ref.digest for ref in checked.children))
    assert store.finish((root.digest,), (root.digest,)).writes == ()
    removed = store.finish((root.digest,), ())
    assert removed.deletes and not removed.writes
    assert observed and set(observed.values()) == {1}
    reader.calls.clear()
    _, _, fresh = assemble(reader, max_items=4)
    assert tuple(fresh.entries(root)) == tuple(expected)
    assert reader.calls and set(observed.values()) == {1}


def test_checked_node_cannot_be_supplied_as_unvalidated_fields_or_rebound_to_other_bytes():
    reader, root, _ = persisted(1)
    raw = reader.rows[NODES, root.digest]
    checked = module._CheckedStructure(raw, root.digest)
    with pytest.raises(FrozenInstanceError):
        checked.payload_json = "different"
    with pytest.raises(TypeError):
        Entry._from_structure(SimpleNamespace(_rows=(("a", 999, 0),)), 0)
    with pytest.raises(TypeError):
        replace(checked, _value={"entries": []})
    store = TreeStore(reader, namespace="_test")
    assert store.checked_nodes((root.digest,))[0].payload_json == raw
    with pytest.raises(ContractError, match="不可变"):
        store._check(raw + " ", root.digest)
    with pytest.raises(ContractError, match="不可变"):
        store.stage_node(raw, (root.digest,))


def test_generic_format_full_and_patch_bytes_are_fixed():
    reader, store, tree = assemble(max_items=2, max_bytes=4096)
    values = [
        Entry(key, {"n": number, "nested": [True, None, {"escaped": '甲\\"🙂'}]})
        for key, number in (("a", 1), ("a\x00", 2), ("a甲", 3), ("𠮷", 4), ("🙂", 5))
    ]
    root = tree.full(values)
    plan = store.finish((), (root.digest, root.digest))
    assert root.digest == "d86781d08414ea9fa138d5015eeb05256ec50db0223e5f8a9541d9a0fe0cba79"
    assert (
        digest(canonical_json([[row.collection, row.key, row.payload_json] for row in plan.writes]))
        == "149443f56e09c074de98bd112e986c653cded0956eb2ff52df23068bcea65f8d"
    )
    reader.apply(plan)
    _, store, tree = assemble(reader, max_items=2, max_bytes=4096)
    changed = tree.patch(
        root,
        {"a": None, "a甲": Entry("a甲", {"n": -5, "nested": [True, None, {"escaped": '甲\\"🙂'}]})},
    )
    plan = store.finish((root.digest, root.digest), (changed.digest,))
    assert changed.digest == "633cfaa036c36e076349ff91cf16a4b036252f905489b1421d7d50e3ad7526ac"
    assert (
        digest(
            canonical_json(
                {
                    "writes": [[row.collection, row.key, row.payload_json] for row in plan.writes],
                    "deletes": [list(key) for key in plan.deletes],
                }
            )
        )
        == "86bbc262136ad4b0e4e866ae97afc22678034803ed976efe2e78a5f0bdada175"
    )


@pytest.mark.parametrize("collection", [NODES, "_test_node_refs"])
def test_generic_codec_rejects_former_domain_specific_node_and_reference_formats(collection):
    reader, root, _ = persisted(1)
    key = collection, root.digest
    value = loads_json(reader.rows[key])
    value["format"] = (
        "ledger-financial-tree/1" if collection == NODES else "ledger-financial-tree-ref/1"
    )
    raw = canonical_json(value)
    if collection == NODES:
        with pytest.raises(ContractError):
            structure(raw, digest(raw))
    else:
        reader.rows[key] = raw
        _, store, _ = assemble(reader)
        with pytest.raises(ContractError):
            store.finish((root.digest,), (root.digest,))


@pytest.mark.parametrize(
    "value", [None, False, True, -(2**63), 2**63, 10**200, "甲𠮷🙂\x00\n", [1, {"a": [None]}]]
)
def test_decoded_json_values_have_the_same_canonical_bytes_and_weight(value):
    reader, store, tree = assemble()
    entry = Entry("甲\x00🙂", {"n": 1, "probe": value})
    root = tree.full((entry,))
    reader.apply(store.finish((), (root.digest,)))
    raw = reader.rows[NODES, root.digest]
    actual, entries, children = structure(raw, root.digest)
    assert canonical_json(loads_json(raw)) == canonical_json(actual) == raw
    assert entries == (entry,) and children == ()
    assert entries[0]._weight == len(canonical_json([entry.key, entry.value]).encode("utf8"))


@pytest.mark.parametrize(
    "fragment",
    ["1.0", "-0.0", "1e-400", "NaN", "Infinity", "-Infinity", '{"a":1,"a":2}', '"\\ud800"'],
)
def test_stored_json_rejects_the_same_forbidden_values_as_the_contract(fragment):
    reader, store, tree = assemble()
    root = tree.full((Entry("a", {"n": 1, "probe": None}),))
    reader.apply(store.finish((), (root.digest,)))
    raw = reader.rows[NODES, root.digest].replace('"probe":null', '"probe":' + fragment)
    with pytest.raises((ContractError, ValueError, UnicodeError)):
        canonical_json(loads_json(raw))
    with pytest.raises(ContractError) as error:
        structure(raw, digest(raw))
    assert error.value.code == "storage_corrupt"


def test_structure_never_exposes_a_mutable_alias_into_checked_entries():
    reader, root, _ = persisted(1)
    raw = reader.rows[NODES, root.digest]
    value, entries, _ = structure(raw, root.digest)
    value["entries"][0][1]["n"] = 999
    entries[0].value["n"] = 888
    assert entries[0].value == {"n": 0}
    assert structure(raw, root.digest)[1][0].value == {"n": 0}


def test_shared_structure_never_shares_strategy_acceptance_or_hides_changed_actual_rows():
    reader, root, values = persisted(1)
    _, store, tree = assemble(reader)
    assert tuple(tree.entries(root)) == tuple(values)

    class WrongSummary(Summary):
        def decode_summary(self, value):
            return {"sum": value["sum"] + 1}

    wrong = Tree(tree.domain, WrongSummary(), store)
    with pytest.raises(ContractError, match="不可变"):
        wrong.root(root.wire())
    reader.rows[NODES, root.digest] += " "
    _, _, fresh = assemble(reader)
    with pytest.raises(ContractError, match="不可变"):
        fresh.root(root.wire())


def test_external_entry_input_stays_strict_and_detached():
    class Text(str):
        pass

    source = {"n": 1, "text": Text("甲🙂"), "nested": [False]}
    entry = Entry(Text("a"), source)
    source["nested"].append(True)
    assert type(entry.key) is str and entry.value["nested"] == [False]
    for invalid in (1.0, float("nan"), (1, 2), {1: "not a string key"}, "\ud800"):
        with pytest.raises((ValueError, UnicodeError)):
            Entry("a", invalid)


def test_entry_json_copies_keep_aliases_without_reconstructing_builtin_scalars(monkeypatch):
    expected = [False, None, 1, -(2**63), 10**200, "甲𠮷🙂\x00\n"]
    shared = {"values": list(expected)}
    source = [shared, shared, {"again": shared}]
    entry = Entry("a", source)
    source.append("later")
    shared["values"].append("later")

    def unexpected(*args):
        raise AssertionError("strict builtin JSON needs no generic object reconstruction")

    monkeypatch.setattr(module, "deepcopy", unexpected)
    first, second = entry.value, entry.value
    assert len(first) == 3 and first[0]["values"] == expected
    assert first[0] is first[1] is first[2]["again"]
    assert second[0] is second[1] is second[2]["again"]
    assert first[0] is not second[0] and first[0]["values"] is not second[0]["values"]
    first[0]["values"].append("changed return")
    second[2]["again"]["new"] = True
    assert entry.value[0] == {"values": expected}


def test_entry_string_subclass_copies_keep_attributes_and_cross_container_aliases():
    class Text(str):
        pass

    text = Text("甲𠮷🙂")
    shared = [False]
    source = {"text": text, "shared": shared, "again": shared}
    text.notes = shared
    text.parent = source
    entry = Entry("a", source)
    text.notes.append("changed input")
    source["new"] = True

    first, second = entry.value, entry.value
    assert type(first["text"]) is Text and first["text"] == "甲𠮷🙂"
    assert first["text"].parent is first
    assert first["text"].notes is first["shared"] is first["again"]
    assert first["shared"] == [False] and "new" not in first
    assert first["text"] is not second["text"]
    first["text"].notes.append("changed return")
    first["text"].parent["new"] = True
    assert second["shared"] == [False] and "new" not in second
    assert entry.value["shared"] == [False] and "new" not in entry.value


def test_stored_raw_still_requires_exact_string_type_and_canonical_text():
    reader, root, _ = persisted(1)
    raw = reader.rows[NODES, root.digest]

    class Text(str):
        pass

    for invalid in (Text(raw), raw + " ", raw.replace('"count":1', '"count":1,"count":1')):
        with pytest.raises(ContractError, match="不可变"):
            structure(invalid, digest(invalid))
    _, _, tree = assemble(reader)
    with pytest.raises(ContractError, match="不可变"):
        tree.root(replace(root, weight=True).wire())


def test_integer_digit_limit_and_actual_surrogates_keep_the_same_rejection():
    if (limit := sys.get_int_max_str_digits()) != 0:
        reader, store, tree = assemble()
        root = tree.full((Entry("a", {"n": 1, "probe": None}),))
        reader.apply(store.finish((), (root.digest,)))
        raw = reader.rows[NODES, root.digest].replace(
            '"probe":null', '"probe":' + "1" * (limit + 1)
        )
        with pytest.raises(ContractError):
            loads_json(raw)
        with pytest.raises(ContractError, match="不可变"):
            structure(raw, digest(raw))
    with pytest.raises(ContractError, match="不可变"):
        structure('"\ud800"', "0" * 64)
