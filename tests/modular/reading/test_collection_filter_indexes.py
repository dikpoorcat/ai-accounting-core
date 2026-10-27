"""Nullable filter indexes omit no eligible rows or unfiltered collection members."""

from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.reading._collections import (
    CollectionFilter,
    IndexItem,
    collection_items,
    collection_page,
    prepare_items,
    prepare_model,
)

from ._support import BANK, PARTY, assemble, rebuild_guard

SCOPE, COMPONENT = "collection-filter-indexes", "filter-indexes"
INDEXES = {
    "object_id": "read_item_object",
    "group_key": "read_item_group",
    "category": "read_item_category",
}


def publish(book, entries):
    model = prepare_model({}, items=prepare_items(entries, collections=("entries", "private")))
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        repository.register(SCOPE, COMPONENT, unit.head, 1)
        assert repository.publish(
            SCOPE,
            COMPONENT,
            unit.head,
            1,
            model,
            guard=rebuild_guard(repository, SCOPE, COMPONENT, unit.head),
            current_head=unit.head,
        )


@pytest.fixture
def collection_case(tmp_path):
    book = assemble(tmp_path)
    entries = tuple(
        IndexItem(
            "entries",
            f"row-{number:04d}",
            "same-sort",
            {"number": number},
            object_id=PARTY if number % 3 == 0 else None,
            group_key="group" if number % 5 == 0 else None,
            category=None if number % 4 == 3 else "fee" if number % 2 == 0 else "wage",
        )
        for number in range(520)
    ) + (IndexItem("private", "opaque", "opaque", {"text": "私有空筛选字段"}),)
    publish(book, entries)
    return book, entries


def test_filter_index_predicates_are_in_the_actual_created_schema(collection_case):
    book, _ = collection_case
    with book.storage.read_unit(book.company) as read:
        for column, name in INDEXES.items():
            sql = read._connection.execute(
                "SELECT sql FROM sqlite_schema WHERE type='index' AND name=?", (name,)
            ).fetchone()[0]
            assert " ".join(sql.upper().split()).partition(" WHERE ")[2] == (
                column.upper() + " IS NOT NULL"
            )


def test_actual_count_page_and_get_keep_nulls_combinations_and_same_sort_continuations(
    collection_case,
):
    book, _ = collection_case
    selections = (
        (CollectionFilter(object_id=None, group_key=None, categories=()), list(range(520))),
        (CollectionFilter(object_id=PARTY), list(range(0, 520, 3))),
        (CollectionFilter(group_key="group"), list(range(0, 520, 5))),
        (CollectionFilter(categories=("fee",)), list(range(0, 520, 2))),
        (CollectionFilter(categories=("fee", "wage")), [n for n in range(520) if n % 4 != 3]),
        (
            CollectionFilter(object_id=PARTY, categories=("wage",)),
            [n for n in range(520) if n % 3 == 0 and n % 4 == 1],
        ),
        (
            CollectionFilter(object_id=PARTY, group_key="group", categories=("fee",)),
            list(range(0, 520, 30)),
        ),
        (CollectionFilter(object_id=BANK), []),
        (CollectionFilter(categories=("missing",)), []),
    )
    with book.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        for query, expected in selections:
            assert repository.item_counts(SCOPE, COMPONENT, "entries", query) == (
                520,
                len(expected),
            )
            raw = repository.item_page(SCOPE, COMPONENT, "entries", query, None, 21)
            assert [row["item_id"] for row in raw] == [f"row-{n:04d}" for n in expected[:21]]
            first = collection_page(SCOPE, COMPONENT, "entries", query, PageQuery(20), read)
            values = list(first.items)
            if first.next_cursor is not None:
                last = collection_page(
                    SCOPE,
                    COMPONENT,
                    "entries",
                    query,
                    PageQuery(500, first.next_cursor, first.version),
                    read,
                )
                assert last.next_cursor is None
                values.extend(last.items)
            assert (first.total_count, first.filtered_count) == (520, len(expected))
            assert [value["number"] for value in values] == expected
            requested = tuple(f"row-{n:04d}" for n in reversed(expected[:3]))
            exact = repository.item_get(SCOPE, COMPONENT, "entries", requested)
            assert {row["item_id"] for row in exact} == set(requested)
            assert [
                value["number"]
                for value in collection_items(SCOPE, COMPONENT, "entries", requested, read)
            ] == list(reversed(expected[:3]))
        assert repository.item_counts(SCOPE, COMPONENT, "private", CollectionFilter()) == (1, 1)
        private = repository.item_get(SCOPE, COMPONENT, "private", ("opaque",))[0]
        assert all(private[column] is None for column in INDEXES)
        assert collection_items(SCOPE, COMPONENT, "private", ("opaque",), read) == (
            {"text": "私有空筛选字段"},
        )


@pytest.mark.parametrize(
    ("column", "query"),
    (
        ("object_id", CollectionFilter(object_id=PARTY)),
        ("group_key", CollectionFilter(group_key="group")),
        ("category", CollectionFilter(categories=("fee", "wage"))),
    ),
)
def test_actual_owner_filtered_counts_can_use_partial_indexes_without_hints(
    collection_case, monkeypatch, column, query
):
    book, _ = collection_case
    with book.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        session, statements = repository._session, []
        original = session.fetchone

        def observed(sql, parameters=()):
            statements.append((sql, parameters))
            return original(sql, parameters)

        monkeypatch.setattr(session, "fetchone", observed)
        repository.item_counts(SCOPE, COMPONENT, "entries", query)
        assert len(statements) == 2
        full, filtered = (
            " ".join(
                row[3] for row in read._connection.execute("EXPLAIN QUERY PLAN " + sql, parameters)
            )
            for sql, parameters in statements
        )
        assert all(name not in full for name in INDEXES.values())
        assert INDEXES[column] in filtered


def test_prepared_publication_moves_rows_into_and_out_of_all_three_indexes(collection_case):
    book, entries = collection_case
    original = entries[7]
    assert all(getattr(original, column) is None for column in INDEXES)
    query = CollectionFilter(object_id=PARTY, group_key="group", categories=("fee",))

    def selected():
        with book.storage.read_unit(book.company) as read:
            result = collection_page(SCOPE, COMPONENT, "entries", query, PageQuery(500), read)
            return result.version, [value["number"] for value in result.items]

    before = selected()
    changed = list(entries)
    changed[7] = replace(original, object_id=PARTY, group_key="group", category="fee")
    publish(book, changed)
    version, values = selected()
    assert version != before[0] and values == sorted([*before[1], 7])
    publish(book, entries)
    assert selected() == before


@pytest.mark.parametrize(
    "query",
    (
        CollectionFilter(categories=(None,)),
        CollectionFilter(group_key=""),
        CollectionFilter(object_id=False),
    ),
)
def test_null_filter_values_are_not_a_second_null_selection_mode(collection_case, query):
    book, _ = collection_case
    with book.storage.read_unit(book.company) as read:
        with pytest.raises(ContractError) as error:
            collection_page(SCOPE, COMPONENT, "entries", query, PageQuery(), read)
        assert error.value.code == "invalid_input"
