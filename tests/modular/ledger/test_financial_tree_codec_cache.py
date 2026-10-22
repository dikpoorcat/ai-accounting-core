"""Only exact immutable bytes share decoding; actual rows and business lifetimes do not."""

import sys
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError, replace
from threading import Barrier

import _financial_snapshot_review_support as support
import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json
from ai_accounting_modular.ledger import _financial_tree as tree_module
from ai_accounting_modular.ledger import _financial_tree_store as module
from ai_accounting_modular.ledger._financial_projection_model import Model
from ai_accounting_modular.ledger._financial_tree import Entry, Tree, digest, structure
from ai_accounting_modular.ledger.public import FormalChange
from tests.modular.acceptance.performance.ledger_variant import MODES, repository_factory
from tests.modular.ledger.test_financial_projection_candidates import assert_snapshot
from tests.modular.ledger.test_financial_projection_model import Blocks, scope
from tests.modular.ledger.test_financial_projection_ranges import close, corrected
from tests.modular.ledger.test_financial_tree import Reader, Summary, assemble, persisted


@pytest.fixture(autouse=True)
def codec(monkeypatch):
    cache = module._StructureCache()
    monkeypatch.setattr(module, "_structure_cache", cache)
    return cache


def stored_node(value=1, **capacity):
    reader, store, tree = assemble(**capacity)
    root = tree.full((Entry("甲🙂", {"n": value}),))
    reader.apply(store.finish((), (root.digest,)))
    return reader.rows[module.TreeStore.NODES, root.digest], root


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
    raw = reader.rows[module.TreeStore.NODES, root.digest]
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
    reader = Reader({(module.TreeStore.NODES, root.digest): raw})
    monkeypatch.setattr(module, "_structure_cache", module._StructureCache())
    original, calls = tree_module._LONG_DIGITS, []

    class Observed:
        def search(self, value):
            calls.append(value)
            return original.search(value)

    monkeypatch.setattr(tree_module, "_LONG_DIGITS", Observed())
    first = module.TreeStore(reader)
    assert first.checked_nodes((root.digest,))[0].integer_policy_independent
    assert first.checked_nodes((root.digest,))[0].integer_policy_independent
    second = module.TreeStore(reader)
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
    reader = Reader({(module.TreeStore.NODES, root.digest): raw})
    original, decoded = tree_module._structure, []

    def observed(value, identity):
        decoded.append(identity)
        return original(value, identity)

    monkeypatch.setattr(tree_module, "_structure", observed)
    store = module.TreeStore(reader)
    first = store.checked_nodes((root.digest,))[0]
    assert store.checked_nodes((root.digest,))[0] is first
    assert module.TreeStore(reader).checked_nodes((root.digest,))[0] is not first
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
    actual = {module.TreeStore.NODES: raw}
    if damage == "missing":
        actual = {}
    elif damage == "different_raw":
        actual[module.TreeStore.NODES] = raw + " "
    else:
        with pytest.raises(ContractError):
            codec.get(Text(raw), root.digest)
        actual[module.TreeStore.NODES] = Text(raw)
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
    reader = Reader({(module.TreeStore.NODES, root.digest): raw})

    class WrongSummary(Summary):
        def decode_summary(self, value):
            value["sum"] += 1
            return value

    for domain, strategy, capacity in (
        ("different", Summary(), 128),
        ("financial-test-summary/1", WrongSummary(), 128),
        ("financial-test-summary/1", Summary(), 4),
    ):
        tree = Tree(domain, strategy, module.TreeStore(reader), max_items=capacity)
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
        reader = Reader({(module.TreeStore.NODES, root.digest): raw})
        store = module.TreeStore(reader)
        assert store.checked_nodes((root.digest,))[0].payload_json == raw
        sys.set_int_max_str_digits(640)
        with pytest.raises(ContractError) as error:
            store.checked_nodes((root.digest,))
        assert error.value.code == "storage_corrupt" and reader.calls
        with pytest.raises(ContractError):
            module.TreeStore(reader).checked_nodes((root.digest,))
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
        reader = Reader({(module.TreeStore.NODES, root.digest): raw})
        store = module.TreeStore(reader)
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
        following = store if same_store else module.TreeStore(reader)
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


@pytest.mark.parametrize("mode", MODES)
@pytest.mark.parametrize("frozen", [False, True], ids=["open", "closed"])
def test_real_replacement_keeps_only_actual_sources_and_same_full_statements(
    tmp_path, monkeypatch, mode, frozen
):
    monkeypatch.setattr(support, "ledger_repository", lambda: repository_factory(mode))
    book, blocks = support.assemble(tmp_path), Blocks()
    cost = support.expense("codec-replacement", "7")
    book.submit(support.opening(cash="1000"), cost)
    selected_scope = scope(book) | {"period_to": "2026-03", "view": "current"}
    with book.app.storage.read_unit(book.company) as read:
        request = book.app.ledger.financial_projection_request("snapshot", selected_scope, read)
        model = Model(read)
        previous = model.advance((request,), ())
        old_atom = next(
            head["atom"]
            for chain in model._old_chains().values()
            for head in chain
            if head["business_id"] == cost["business_id"]
        )
        old_body = model.bodies.get_many((old_atom,))[0]
        blocks.publish(model.plan(previous))
    if frozen:
        close(book, "2026-01")
        with book.app.storage.read_unit(book.company) as read:
            report = book.app.ledger.get_closed_report(scope(book), read)
            saved = canonical_json({key: value for key, value in report.items() if key != "head"})
    book.replace(corrected(cost, "9"), correction_period="2026-02" if frozen else None)
    with book.app.storage.read_unit(book.company) as read:
        change = FormalChange(
            cost["business_id"],
            "main",
            ("2026-01",),
            ("2026-02" if frozen else "2026-01",),
            ("operations.expense",),
        )
        blocks.publish(
            book.app.ledger.prepare_financial_projection(
                (request,), (change,), (), read, base=blocks
            )
        )
        current = Model(read, blocks)
        if frozen:
            assert current.bodies.get_many((old_atom,))[0] == old_body
            report = book.app.ledger.get_closed_report(scope(book), read)
            # Only the envelope's live read head advances; all three statements
            # and the complete frozen tax basis retain their actual bytes.
            assert (
                canonical_json({key: value for key, value in report.items() if key != "head"})
                == saved
            )
        else:
            with pytest.raises(ContractError):
                current.bodies.get_many((old_atom,))
        assert_snapshot(book, request, read, blocks)
        cached = book.app.ledger.read_financial_projection(request, read, blocks).value
        assert cached["profit_statement"]["32"]["current"]["amount_fen"] == "-9"
        used = [
            value
            for value in book.app.ledger.financial_summary(request.scope, read).sources
            if value["business_id"] == cost["business_id"]
        ]
        assert len(used) == 1 and used[0] != old_body["source"]
        with monkeypatch.context() as patch:
            patch.setattr(module, "_checked_structure", tree_module._CheckedStructure)
            assert book.app.ledger.read_financial_projection(request, read, blocks).value == cached
