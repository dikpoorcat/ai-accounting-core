"""Immutable Open roots preserve real collection reads and release only unused nodes."""

from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    loads_json,
)
from ai_accounting_modular.reading import _brief_open_index as index
from ai_accounting_modular.reading import _brief_open_tree as blocks
from ai_accounting_modular.reading import _monthly
from ai_accounting_modular.reading._collections import (
    CollectionFilter,
    IndexItem,
    collection_items,
    collection_page,
    prepare_items,
    prepare_model,
)
from ai_accounting_modular.reading._open_store import NODES, REFS, SCOPE
from ai_accounting_modular.reading._publication_guard import CaptureGuard
from tests.modular.application._working_case import case as case

from ._publication import CapturedState
from ._publication import body as stored_body
from ._support import BANK, PARTY


def scope(case, month="2026-01"):
    return _monthly.normalize(
        {"company_id": case.company, "period_from": month, "period_to": month, "view": "as_posted"}
    )


def summary(month="2026-01"):
    return {
        "open_items": {"cutoff_period": month, "current_cutoff_period": month},
    }


def publish(case, entries, month="2026-01"):
    selected = _monthly.key(scope(case, month))
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        repository.register(selected, index.COMPONENT, unit.head, index.FORMAT_VERSION)
        if repository.state(selected, index.COMPONENT)["state"] == "ready":
            # Explicitly schedule a same-head full rebuild of this synthetic
            # projection. Ready candidates must never publish without work.
            repository.open_mark(selected, unit.head, full=True)
            repository._session.execute(
                "UPDATE read_scope_head SET state='preparing' "
                "WHERE scope_key=? AND component_key=?",
                (selected, index.COMPONENT),
            )
        model = index.complete(
            summary(month), entries, unit.head, repository=repository, scope_key=selected
        )
        guard = CaptureGuard.capture(repository.state(selected, index.COMPONENT), unit.head)
        assert repository.publish(
            selected,
            index.COMPONENT,
            unit.head,
            index.FORMAT_VERSION,
            model,
            guard=guard,
            current_head=unit.head,
        )
    return model


def patch(case, entries, deletes, month="2026-01"):
    selected = _monthly.key(scope(case, month))
    # These are synthetic projection-only fixtures. Install ordinary partial
    # work at the same formal head; no business or accounting rows are changed.
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        repository.open_mark(selected, unit.head, (PARTY,))
        repository._session.execute(
            "UPDATE read_scope_head SET state='preparing' WHERE scope_key=? AND component_key=?",
            (selected, index.COMPONENT),
        )
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        state = CapturedState(repository.state(selected, index.COMPONENT), read.head)
        directory = index.base(repository, state)
        model = index.patch(summary(month), entries, deletes, directory, state, scope_key=selected)
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        assert unit.repository("reading").publish(
            selected,
            index.COMPONENT,
            state["wanted_head"],
            index.FORMAT_VERSION,
            model,
            guard=state.guard,
            current_head=unit.head,
        )
    return model


def nodes(case):
    with case.kernel.storage.read_unit(case.company) as read:
        return {
            (row["collection_key"], row["item_id"]): row["payload_json"]
            for row in read.repository("reading")._session.fetchall(
                "SELECT * FROM read_item WHERE scope_key=? AND collection_key IN (?,?)",
                (SCOPE, NODES, REFS),
            )
        }


def assert_live_graph(case):
    actual = nodes(case)
    with case.kernel.storage.read_unit(case.company) as read:
        repository = read.repository("reading")
        rows = [
            stored_body(repository, row["scope_key"], index.COMPONENT)
            for row in repository.scopes()
            if row["component_key"] == index.COMPONENT and row["state"] == "ready"
        ]
    pending = [
        root[0]
        for row in rows
        for root in loads_json(row["payload_json"])["_roots"].values()
        if root
    ]
    visited = set()
    while pending:
        identity = pending.pop()
        if identity in visited:
            continue
        visited.add(identity)
        body = loads_json(actual[NODES, identity])
        pending.extend(child[0] for child in body.get("children", ()))
    assert visited == {identity for collection, identity in actual if collection == NODES}
    assert visited == {identity for collection, identity in actual if collection == REFS}


def test_all_filters_and_same_sort_pages_match_the_actual_sql_collection_reader(case):
    case.seed(expense=False)
    entries = tuple(
        IndexItem(
            "open_members",
            f"member-{n:04d}",
            "same-sort",
            {"number": n},
            business_id=PARTY if n % 7 == 0 else None,
            object_id=PARTY if n % 3 == 0 else None,
            group_key="group" if n % 5 == 0 else None,
            category=None if n % 4 == 3 else "fee" if n % 2 == 0 else "wage",
        )
        for n in range(520)
    )
    publish(case, entries)
    selected, oracle = _monthly.key(scope(case)), "sql-oracle"
    with case.kernel.storage.maintenance_unit(case.company) as unit:
        repository = unit.repository("reading")
        repository.register(selected, oracle, unit.head, 1)
        guard = CaptureGuard.capture(repository.state(selected, oracle), unit.head)
        assert repository.publish(
            selected,
            oracle,
            unit.head,
            1,
            prepare_model({}, items=prepare_items(entries)),
            guard=guard,
            current_head=unit.head,
        )
    queries = (
        CollectionFilter(),
        CollectionFilter(categories=()),
        CollectionFilter(object_id=PARTY),
        CollectionFilter(group_key="group"),
        CollectionFilter(categories=("fee",)),
        CollectionFilter(categories=("wage", "fee")),
        CollectionFilter(object_id=BANK),
        CollectionFilter(categories=("missing",)),
        CollectionFilter(business_id=PARTY),
        CollectionFilter(object_id=PARTY, group_key="group", categories=("fee",)),
        CollectionFilter(business_id=PARTY, categories=("wage", "fee")),
        CollectionFilter(item_id="member-0000", group_key="group", categories=("wage",)),
        CollectionFilter(item_id="member-0000", group_key="group", categories=("fee",)),
    )
    with case.kernel.storage.read_unit(case.company) as read:
        for query in queries:
            cursor, version = (
                {index.COMPONENT: None, oracle: None},
                {index.COMPONENT: None, oracle: None},
            )
            first = True
            while True:
                pages = {
                    component: collection_page(
                        selected,
                        component,
                        "open_members",
                        query,
                        PageQuery(20 if first else 500, cursor[component], version[component]),
                        read,
                    )
                    for component in cursor
                }
                actual, expected = pages[index.COMPONENT], pages[oracle]
                assert (actual.total_count, actual.filtered_count, actual.items) == (
                    expected.total_count,
                    expected.filtered_count,
                    expected.items,
                )
                assert bool(actual.next_cursor) == bool(expected.next_cursor)
                if actual.next_cursor is None:
                    break
                cursor = {component: page.next_cursor for component, page in pages.items()}
                version = {component: page.version for component, page in pages.items()}
                first = False
        assert collection_items(
            selected, index.COMPONENT, "open_members", ("member-0519", "member-0000"), read
        ) == ({"number": 519}, {"number": 0})


def test_split_merge_and_changed_filter_keys_have_the_same_roots_as_a_full_factory(case):
    case.seed(expense=False)
    original = [
        IndexItem(
            "open_members",
            f"member-{n:04d}",
            f"sort-{n:04d}",
            {"number": n},
            group_key="group",
            category="fee",
        )
        for n in range(300)
    ]
    publish(case, original)
    changed = replace(
        original[0],
        sort_key="last",
        object_id=PARTY,
        group_key="other",
        category="wage",
        value={"changed": True},
    )
    added = IndexItem("open_members", "new", "first", {"new": True}, group_key="other")
    removed = [("open_members", value.item_id) for value in original[1:290]]
    result = patch(case, [changed, added], removed)
    final = [changed, *original[290:], added]
    with case.kernel.storage.read_unit(case.company) as read:
        cold = index.complete(
            summary(),
            final,
            result.item_epoch,
            repository=read.repository("reading"),
            scope_key=_monthly.key(scope(case)),
        )
        assert cold.payload_json == result.payload_json
    assert_live_graph(case)
    patch(case, (), [(value.collection, value.item_id) for value in final])
    assert not nodes(case), "the final scope must not retain unreferenced nodes or counters"


def test_two_month_roots_share_nodes_and_patching_one_cannot_change_the_other(case):
    case.seed(expense=False)
    entries = [
        IndexItem(
            "open_members", f"member-{n:04d}", f"sort-{n:04d}", {"number": n}, group_key="group"
        )
        for n in range(260)
    ]
    january = publish(case, entries)
    existing = nodes(case)
    february = publish(case, entries, "2026-02")
    assert loads_json(january.payload_json)["_roots"] == loads_json(february.payload_json)["_roots"]
    assert all(write.collection == REFS for write in february.tree_plan.writes)
    assert {identity: raw for identity, raw in existing.items() if identity[0] == NODES} == {
        identity: raw for identity, raw in nodes(case).items() if identity[0] == NODES
    }
    patch(case, [replace(entries[0], value={"number": -1})], (), "2026-02")
    with case.kernel.storage.read_unit(case.company) as read:
        assert collection_items(
            _monthly.key(scope(case)), index.COMPONENT, "open_members", (entries[0].item_id,), read
        ) == ({"number": 0},)
    assert_live_graph(case)
    patch(case, (), [(value.collection, value.item_id) for value in entries], "2026-01")
    assert_live_graph(case)
    patch(case, (), [(value.collection, value.item_id) for value in entries], "2026-02")
    assert not nodes(case)


def test_private_counts_other_collection_and_full_rebuild_preserve_public_cursor(case):
    case.seed(expense=False)
    groups = [IndexItem("open_items", str(n), str(n), {"number": n}) for n in range(3)]
    original = publish(case, [*groups, index.item(index.VOUCHERS, "display", {"count": 2})])
    selected = _monthly.key(scope(case))
    with case.kernel.storage.read_unit(case.company) as read:
        first = collection_page(
            selected, index.COMPONENT, "open_items", CollectionFilter(), PageQuery(1), read
        )
    changed = patch(
        case,
        [
            index.item(index.VOUCHERS, "display", {"count": 1}),
            IndexItem("open_members", "new", "new", {"new": True}),
        ],
        (),
    )
    assert (
        loads_json(changed.payload_json)["_voucher_display_head"]
        == loads_json(original.payload_json)["_voucher_display_head"]
    )
    assert (
        loads_json(changed.payload_json)["_collections"]["open_items"]
        == loads_json(original.payload_json)["_collections"]["open_items"]
    )
    rebuilt = publish(
        case,
        [
            *groups,
            index.item(index.VOUCHERS, "display", {"count": 1}),
            IndexItem("open_members", "new", "new", {"new": True}),
        ],
    )
    assert rebuilt.payload_json == changed.payload_json
    with case.kernel.storage.read_unit(case.company) as read:
        continued = collection_page(
            selected,
            index.COMPONENT,
            "open_items",
            CollectionFilter(),
            PageQuery(20, first.next_cursor, first.version),
            read,
        )
        assert continued.items == ({"number": 1}, {"number": 2})
        with pytest.raises(ContractError) as failure:
            collection_page(
                _monthly.key(scope(case, "2026-02")),
                index.COMPONENT,
                "open_items",
                CollectionFilter(),
                PageQuery(20, first.next_cursor, first.version),
                read,
            )
        assert failure.value.code in {"read_model_not_ready", "snapshot_changed"}


def test_tuple_keys_keep_nul_unicode_prefixes_and_category_union_in_public_order(case):
    case.seed(expense=False)
    values = [
        (sort, identity) for sort in ("a", "a\0", "a\1", "𠮷🙂") for identity in ("x", "x\0", "x🙂")
    ]
    assert sorted(values) == sorted(values, key=lambda value: blocks.key(*value))
    assert all(blocks.parts(blocks.key(*value)) == value for value in values)
    entries = [
        IndexItem("open_members", str(n), sort, {"number": n}, category="a" if n % 2 else "b")
        for n, (sort, _) in enumerate(reversed(values))
    ]
    publish(case, entries)
    with case.kernel.storage.read_unit(case.company) as read:
        actual = collection_page(
            _monthly.key(scope(case)),
            index.COMPONENT,
            "open_members",
            CollectionFilter(categories=("a", "b")),
            PageQuery(20),
            read,
        )
        assert actual.items == tuple(
            value.value
            for value in sorted(entries, key=lambda value: (value.sort_key, value.item_id))
        )


@pytest.mark.parametrize(
    "query",
    [
        CollectionFilter(categories=(None,)),
        CollectionFilter(group_key=""),
        CollectionFilter(object_id=False),
        CollectionFilter(categories=("fee", "fee")),
    ],
)
def test_invalid_filters_never_become_an_alternate_null_mode(case, query):
    case.seed(expense=False)
    publish(case, ())
    with (
        case.kernel.storage.read_unit(case.company) as read,
        pytest.raises(ContractError) as failure,
    ):
        collection_page(
            _monthly.key(scope(case)), index.COMPONENT, "open_members", query, PageQuery(), read
        )
    assert failure.value.code == "invalid_input"
