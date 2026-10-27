"""Open ID locators and ordered bodies prove each other's exact logical identity."""

from dataclasses import replace
from uuid import UUID, uuid5

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery, loads_json
from ai_accounting_modular.reading import _brief_open_index as index
from ai_accounting_modular.reading import _brief_open_tree as blocks
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading._collections import (
    CollectionFilter,
    IndexItem,
    _bound,
    _index,
    collection_page,
)
from ai_accounting_modular.reading._funds_items import key
from ai_accounting_modular.reading._open_store import NODES
from ai_accounting_modular.reading._publication_guard import CaptureGuard
from ai_accounting_modular.storage.public import Entry, Tree
from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import case as case

from .test_brief_open_tree import nodes, patch, publish, scope, summary
from .test_open_missing_header import assert_live_pool


@pytest.fixture(params=MODES)
def ownership_case(tmp_path, request):
    with install_ledger_implementation(request.param):
        fixture = case.__wrapped__(tmp_path)
        try:
            yield next(fixture)
        finally:
            fixture.close()


def test_only_the_public_order_tree_owns_the_complete_body(case):
    case.seed(expense=False)
    items = (
        IndexItem("open_members", "member", "2026-01/member", {"amount_fen": "1000"}),
        IndexItem("open_items", "group", "supplier", {"member_count": 1}),
        index.item(index.BUSINESSES, "business", {"members": ["member"]}),
    )
    publish(case, items)
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        directory = index.base(
            repository, repository.state(_monthly.key(scope(case)), index.COMPONENT)
        )
        assert set(directory.roots) == set(blocks.ROLES)
        for item in items[:2]:
            locator = directory.trees.trees["rows"].get_many(
                directory.roots["rows"], (blocks.key(item.collection, item.item_id),)
            )[0]
            assert locator.value == item.sort_key
            body = directory.trees.trees[item.collection].get_many(
                directory.roots[item.collection], (blocks.key(item.sort_key, item.item_id),)
            )[0]
            saved = body.value
            assert set(saved) == {"index", "value", "digest"}
            assert saved["index"]["item_id"] == item.item_id
            assert saved["index"]["sort_key"] == item.sort_key
            assert saved["value"] == item.value
            assert directory.get(item.collection, (item.item_id,), required=True) == {
                item.item_id: item.value
            }
        private = directory.trees.trees["rows"].get_many(
            directory.roots["rows"], (blocks.key(index.BUSINESSES, "business"),)
        )[0]
        assert private.value == items[2].value


def example_items():
    return (
        IndexItem("open_members", "member", "2026-01/1", {"number": 1}, category="fee"),
        IndexItem("open_members", "second", "2026-01/2", {"number": 2}, category="wage"),
    )


def forge(case, defect):
    """Reseal individually valid trees, keeping counts and every physical SHA consistent."""
    item = example_items()[0]
    selected = _monthly.key(scope(case))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        repository.open_mark(selected, unit.head, full=True)
        repository._session.execute(
            "UPDATE read_scope_head SET state='preparing' WHERE scope_key=? AND component_key=?",
            (selected, index.COMPONENT),
        )
        directory = index.base(repository, repository.state(selected, index.COMPONENT))
        trees, roots = directory.trees, dict(directory.roots)
        locator_key, order_key = (
            blocks.key(item.collection, item.item_id),
            blocks.key(item.sort_key, item.item_id),
        )
        changes = {}
        if defect == "missing_locator":
            changes["rows"] = {
                locator_key: None,
                blocks.key(item.collection, "replacement"): Entry(
                    blocks.key(item.collection, "replacement"), item.sort_key
                ),
            }
        elif defect == "wrong_sort":
            changes["rows"] = {locator_key: Entry(locator_key, "another-sort")}
        elif defect in {"body_key", "body_identity"}:
            body_item = replace(item, item_id="impostor") if defect == "body_identity" else item
            fields = _index(body_item)
            _, digest = _bound(fields, body_item.value)
            saved = {"index": fields, "value": body_item.value, "digest": digest}
            changed_key = (
                blocks.key("another-sort", item.item_id) if defect == "body_key" else order_key
            )
            changes[item.collection] = {order_key: None, changed_key: Entry(changed_key, saved)}
            if defect == "body_key":
                changes["rows"] = {locator_key: Entry(locator_key, "another-sort")}
        elif defect == "filter_digest":
            filter_key = blocks.key(item.collection, "category", "fee", item.sort_key, item.item_id)
            assert _bound(_index(item), item.value)[1] != "f" * 64
            changes["filters"] = {filter_key: Entry(filter_key, "f" * 64)}
        else:
            raise AssertionError(defect)
        for role, changed in changes.items():
            roots[role] = trees.trees[role].patch(roots[role], changed)
        candidate = index.PreparedOpen(
            index.seal(index._summary(directory.payload, roots), (), directory.epoch),
            directory.pool.token,
            trees.store.finish(blocks.digests(directory.roots), blocks.digests(roots)),
            selected,
        )
        assert repository.publish(
            selected,
            index.COMPONENT,
            unit.head,
            index.FORMAT_VERSION,
            candidate,
            guard=CaptureGuard.capture(repository.state(selected, index.COMPONENT), unit.head),
            current_head=unit.head,
        )
    assert_live_pool(case)


@pytest.mark.parametrize("defect", ["missing_locator", "wrong_sort", "body_key", "body_identity"])
def test_individually_valid_locator_and_body_trees_must_agree_in_both_directions(case, defect):
    case.seed(expense=False)
    publish(case, example_items())
    forge(case, defect)
    with case.kernel.storage.read_unit(case.company) as read:
        selected = _monthly.key(scope(case))
        repository = read.repository("reading")
        directory = index.base(repository, repository.state(selected, index.COMPONENT))
        for operation in (
            lambda: directory.get("open_members", ("member",), required=True),
            lambda: collection_page(
                selected, index.COMPONENT, "open_members", CollectionFilter(), PageQuery(20), read
            ),
        ):
            with pytest.raises(ContractError) as error:
                operation()
            assert error.value.code == "storage_corrupt"


def test_a_filter_entry_cannot_substitute_its_own_digest_for_the_bound_order_body(case):
    case.seed(expense=False)
    publish(case, example_items())
    forge(case, "filter_digest")
    with case.kernel.storage.read_unit(case.company) as read:
        selected = _monthly.key(scope(case))
        repository = read.repository("reading")
        directory = index.base(repository, repository.state(selected, index.COMPONENT))
        assert directory.get("open_members", ("member",), required=True) == {
            "member": {"number": 1}
        }
        with pytest.raises(ContractError) as error:
            collection_page(
                selected,
                index.COMPONENT,
                "open_members",
                CollectionFilter(categories=("fee",)),
                PageQuery(1),
                read,
            )
        assert error.value.code == "storage_corrupt"


def period_items(period, count):
    namespace = UUID("63fc2e91-424f-4eee-a666-c724ad965bb0")
    result = []
    for n in range(count):
        identity = str(uuid5(namespace, f"{period}/{n}")) + ":primary"
        result.append(
            IndexItem(
                "open_members",
                identity,
                key("0", period, "ordinary", identity),
                {"period": period, "number": n, "amount_fen": "1000"},
                group_key="group",
                category="fee",
            )
        )
    return tuple(result)


def test_a_period_sort_move_patch_matches_cold_roots_and_last_delete_reclaims_the_graph(
    ownership_case,
):
    case = ownership_case
    case.seed(expense=False)
    original = period_items("2026-01", 40)
    publish(case, original)
    moved = replace(
        original[0],
        sort_key=key("0", "2026-02", "ordinary", original[0].item_id),
        value={"period": "2026-02", "number": 0, "amount_fen": "1200"},
    )
    result = patch(case, (moved,), ((original[1].collection, original[1].item_id),))
    final = (moved, *original[2:])
    with case.kernel.storage.read_unit(case.company) as read:
        selected = _monthly.key(scope(case))
        repository = read.repository("reading")
        directory = index.base(repository, repository.state(selected, index.COMPONENT))
        cold = index.complete(
            summary(), final, directory.epoch, repository=repository, scope_key=selected
        )
        assert result.payload_json == cold.payload_json
        assert result.content_digest == cold.content_digest
        assert directory.get("open_members", (moved.item_id,), required=True) == {
            moved.item_id: moved.value
        }
        assert directory.get("open_members", (original[1].item_id,)) == {}
        order = directory.trees.trees["open_members"]
        assert (
            order.get_many(
                directory.roots["open_members"], (blocks.key(original[0].sort_key, moved.item_id),)
            )[0]
            is None
        )
        public = collection_page(
            selected, index.COMPONENT, "open_members", CollectionFilter(), PageQuery(500), read
        )
        assert public.items == tuple(
            item.value for item in sorted(final, key=lambda item: (item.sort_key, item.item_id))
        )
    assert_live_pool(case)
    patch(case, (), tuple((item.collection, item.item_id) for item in final))
    assert not nodes(case)


def test_unrelated_private_changes_keep_the_public_continuation_cursor(ownership_case, monkeypatch):
    case = ownership_case
    case.seed(expense=False)
    original = period_items("2026-01", 25)
    published = publish(case, (*original, index.item(index.VOUCHERS, "display", {"count": 1})))
    selected = _monthly.key(scope(case))
    scan_limits = []
    original_scan = Tree.scan

    def scan(self, *args, **kwargs):
        if self.domain == "reading-open-open_members/2":
            scan_limits.append(kwargs["limit"])
        return original_scan(self, *args, **kwargs)

    with case.kernel.storage.read_unit(case.company) as read, monkeypatch.context() as observed:
        observed.setattr(Tree, "scan", scan)
        first = collection_page(
            selected, index.COMPONENT, "open_members", CollectionFilter(), PageQuery(20), read
        )
    assert scan_limits == [21], "A 20-row page must not request 128 full order bodies"
    changed = patch(case, (index.item(index.VOUCHERS, "display", {"count": 2}),), ())
    assert (
        loads_json(published.payload_json)["_collections"]
        == loads_json(changed.payload_json)["_collections"]
    )
    with case.kernel.storage.read_unit(case.company) as read:
        rest = collection_page(
            selected,
            index.COMPONENT,
            "open_members",
            CollectionFilter(),
            PageQuery(500, first.next_cursor, first.version),
            read,
        )
        expected = tuple(
            item.value for item in sorted(original, key=lambda item: (item.sort_key, item.item_id))
        )
        assert first.items + rest.items == expected
        assert rest.version == first.version


def test_new_period_random_ids_preserve_old_period_body_leaf_bytes(ownership_case):
    case = ownership_case
    case.seed(expense=False)
    january, february = period_items("2026-01", 64), period_items("2026-02", 32)
    publish(case, january)
    old = nodes(case)
    leaves = {
        identity: raw
        for (collection, identity), raw in old.items()
        if collection == NODES
        and (body := loads_json(raw))["domain"] == "reading-open-open_members/2"
        and "entries" in body
    }
    assert len(leaves) >= 4
    result = patch(case, february, ())
    after = nodes(case)
    retired = set(leaves) - {identity for collection, identity in after if collection == NODES}
    assert len(retired) <= 1, (
        "Appending a new period may replace a boundary leaf, not old-period bodies"
    )
    for identity in set(leaves) - retired:
        assert after[NODES, identity] == leaves[identity]
    writes = {write.key for write in result.tree_plan.writes if write.collection == NODES}
    assert not writes.intersection(set(leaves) - retired)
    assert_live_pool(case)
