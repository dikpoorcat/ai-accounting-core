"""Owners can page a shared immutable range without loading unrelated leaves."""

from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.storage.public import Entry, Tree, TreeStore
from tests.modular.storage.test_immutable_tree import NODES, Reader, Summary, assemble, persisted


@pytest.mark.parametrize(
    "first,last,after",
    [
        (None, None, None),
        ("", "a", None),
        ("a", "a\x00", "a"),
        ("a", "甲", "a\x00"),
        (None, None, "absent"),
        ("甲", "🙂", "甲"),
        (None, "", None),
        ("z", "a", None),
        ("a", "a", None),
        (None, None, "🙂"),
    ],
)
def test_scan_pages_match_exact_sorted_bounds_and_strict_after(first, last, after):
    values = [
        Entry(key, {"n": index})
        for index, key in enumerate(
            ("", "a", "a\x00", "a\x00x", "ab", "z", "甲", "甲\x00", "𠮷", "🙂")
        )
    ]
    reader, store, tree = assemble(max_items=2)
    root = tree.full(reversed(values))
    reader.apply(store.finish((), (root.digest,)))
    expected = tuple(
        entry
        for entry in sorted(values, key=lambda entry: entry.key)
        if (first is None or first <= entry.key)
        and (last is None or entry.key < last)
        and (after is None or after < entry.key)
    )
    actual = []
    while True:
        _, _, fresh = assemble(reader, max_items=2)
        page = fresh.scan(root, first=first, last=last, after=after, limit=2)
        actual.extend(page)
        if len(page) < 2:
            break
        after = page[-1].key
    assert tuple(actual) == expected


def test_scan_supports_501_lookahead_and_does_not_depend_on_full_range_iteration(monkeypatch):
    reader, root, values = persisted(1200, max_items=8)

    def no_full_range(*args, **kwargs):
        raise AssertionError("paging must not collect the complete range")

    monkeypatch.setattr(Tree, "entries", no_full_range)
    _, _, tree = assemble(reader, max_items=8)
    assert tree.scan(root, limit=501) == tuple(values[:501])
    assert tree.scan(root, after=values[500].key, limit=501) == tuple(values[501:1002])
    assert tree.scan(root, after=values[-1].key, limit=501) == ()


def test_scan_stops_before_unrelated_corrupt_leaf_but_checks_it_when_requested():
    reader, root, values = persisted(4000, max_items=8)
    for (collection, identity), raw in reader.rows.items():
        if collection == NODES:
            node = loads_json(raw)
            if node["kind"] == "leaf" and node["last"] == values[-1].key:
                reader.rows[collection, identity] += " "
                break
    else:
        pytest.fail("the independent last leaf was not found")
    reader.calls.clear()
    _, _, tree = assemble(reader, max_items=8)
    assert tree.scan(root, limit=7) == tuple(values[:7])
    assert len({key for batch in reader.calls for key in batch}) < 40
    with pytest.raises(ContractError) as error:
        tree.scan(root, after=values[-10].key, limit=11)
    assert error.value.code == "storage_corrupt"
    with pytest.raises(ContractError):
        tree.scan(replace(root, count=root.count + 1), limit=1)


@pytest.mark.parametrize("limit", [None, True, False, 0, -1, 1.0, "1"])
def test_scan_requires_a_positive_integer_even_for_an_empty_tree(limit):
    _, _, tree = assemble()
    with pytest.raises(ValueError):
        tree.scan(None, limit=limit)
    assert tree.scan(None, limit=501) == ()


def test_namespaces_share_only_pure_bytes_never_actual_rows_or_reference_counts():
    reader = Reader()
    alpha = TreeStore(reader, namespace="alpha")
    left = Tree("same-owner-rule/1", Summary(), alpha)
    root = left.full((Entry("key", {"n": 7}),))
    reader.apply(alpha.finish((), (root.digest, root.digest)))
    warm = Tree("same-owner-rule/1", Summary(), TreeStore(reader, namespace="alpha"))
    assert warm.root(root.wire()) == root
    beta = TreeStore(reader, namespace="beta")
    right = Tree(left.domain, Summary(), beta)
    with pytest.raises(ContractError):
        right.root(root.wire())
    other = right.full((Entry("key", {"n": 7}),))
    assert other.digest == root.digest
    reader.apply(beta.finish((), (other.digest,)))
    alpha = TreeStore(reader, namespace="alpha")
    reader.apply(alpha.finish((root.digest, root.digest), ()))
    assert set(collection for collection, _ in reader.rows) == {beta.NODES, beta.REFS}
    assert loads_json(reader.rows[beta.REFS, other.digest])["count"] == 1
    right = Tree(left.domain, Summary(), TreeStore(reader, namespace="beta"))
    assert right.scan(other, limit=1)[0].value == {"n": 7}


@pytest.mark.parametrize("namespace", [None, "", 1, True])
def test_graph_namespace_is_explicit_and_cannot_use_an_implicit_financial_default(namespace):
    with pytest.raises(ValueError):
        TreeStore(Reader(), namespace=namespace)
    with pytest.raises(TypeError):
        TreeStore(Reader())


def test_read_blocks_batches_once_in_the_same_snapshot_and_preserves_duplicates_and_missing():
    keys = tuple(("owner_bodies", str(index)) for index in range(501))
    reader = Reader({key: str(index) for index, key in enumerate(keys)})
    store = TreeStore(reader, namespace="owner")
    requested = (*keys, keys[0], ("owner_bodies", "absent"))
    assert store.read_blocks(requested) == (*map(str, range(501)), "0", None)
    assert [len(batch) for batch in reader.calls] == [500, 2]
    reader.calls.clear()
    assert store.read_blocks((keys[-1], keys[0])) == ("500", "0")
    assert not reader.calls
