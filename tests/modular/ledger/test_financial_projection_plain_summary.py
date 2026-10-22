"""Constant directory summaries preserve the same physical financial graph."""

from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json, loads_json
from ai_accounting_modular.ledger._financial_projection_indexes import (
    CashBalanceStrategy,
    FlowStrategy,
    JournalStrategy,
    PlainStrategy,
)
from ai_accounting_modular.storage import _immutable_tree as tree_module
from ai_accounting_modular.storage import _immutable_tree_store as store_module
from ai_accounting_modular.storage.public import Entry, Tree, TreeStore
from ai_accounting_modular.storage.public import block_digest as digest
from tests.modular.ledger.test_financial_projection_indexes import Rows


def tree(rows, domain, strategy):
    store = TreeStore(rows, namespace="_financial")
    return store, Tree(domain, strategy, store, max_items=1)


@pytest.mark.parametrize(
    "domain",
    [
        "financial-adoption-chains/1",
        "financial-source-bodies/2",
        "financial-gl-dimensions/2",
        "financial-controlled-resources/2",
    ],
)
def test_plain_patch_checks_actual_frontier_and_matches_cold_full_graph(monkeypatch, domain):
    monkeypatch.setattr(store_module, "_structure_cache", store_module._StructureCache())
    rows = Rows()
    values = [Entry(key, {"value": n}) for n, key in enumerate("abc")]
    store, original = tree(rows, domain, PlainStrategy())
    root = original.full(values)
    # Two consumers initially share one root. Replacing just one must preserve
    # all old content and count every shared physical parent edge exactly once.
    rows.publish(store.finish((), (root.digest, root.digest)))
    unused = next(child for child in original._loaded[root.digest].children if child.first == "b")
    decode, decoded = tree_module._structure, []

    def observed(raw, identity):
        decoded.append(identity)
        return decode(raw, identity)

    monkeypatch.setattr(tree_module, "_structure", observed)
    store, changed_tree = tree(rows, domain, PlainStrategy())
    update = Entry("a", {"value": -1})
    changed = changed_tree.patch(root, {"a": update})
    rows.publish(store.finish((root.digest,), (changed.digest,)))
    assert decoded.count(unused.digest) == 1
    assert unused.digest in changed_tree._loaded and unused.digest in store._checked
    assert changed.summary is None
    assert changed_tree.get_many(changed, ("b",))[0].value == {"value": 1}
    assert decoded.count(unused.digest) == 1

    expected = Rows()
    strict_store, strict = tree(expected, domain, PlainStrategy())
    old_full = strict.full(values)
    new_full = strict.full([update, *values[1:]])
    expected.publish(strict_store.finish((), (old_full.digest, new_full.digest)))
    assert root == old_full and changed == new_full and rows.values == expected.values

    # Reusing a locator never exempts a later actual read from checking bytes.
    rows.values[store.NODES, unused.digest] += " "
    _, fresh = tree(rows, domain, PlainStrategy())
    with pytest.raises(ContractError) as error:
        fresh.get_many(changed, ("b",))
    assert error.value.code == "storage_corrupt"


def test_plain_actual_root_rejects_a_self_hashed_nonempty_summary():
    rows = Rows()
    store, original = tree(rows, "financial-source-bodies/2", PlainStrategy())
    root = original.full((Entry("a", "body locator"),))
    rows.publish(store.finish((), (root.digest,)))
    value = loads_json(rows.values[store.NODES, root.digest])
    value["summary"] = 1
    raw = canonical_json(value)
    rows.values[store.NODES, digest(raw)] = raw
    _, fresh = tree(rows, original.domain, PlainStrategy())
    with pytest.raises(ContractError) as error:
        fresh.root(replace(root, digest=digest(raw)).wire())
    assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize(
    "strategy", [PlainStrategy, FlowStrategy, JournalStrategy, CashBalanceStrategy]
)
def test_financial_folds_use_actual_child_summaries(strategy):
    assert not hasattr(strategy, "summary_from_count")
