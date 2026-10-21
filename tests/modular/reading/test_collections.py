"""Projection collections publish atomically and paginate without decoding other rows."""

import sqlite3
from dataclasses import replace
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    canonical_json,
    content_digest,
    loads_json,
)
from ai_accounting_modular.reading._collections import (
    CollectionFilter,
    IndexItem,
    collection_items,
    collection_page,
    prepare_items,
    prepare_model,
)
from ai_accounting_modular.storage.public import CompanySpec

from ._support import BANK, PARTY, assemble, expense

SCOPE, COMPONENT = "synthetic-month-2026-01", "activity"


def publish(book, items, *, component=COMPONENT, summary=None):
    prepared = prepare_items(items, collections=("entries",))
    model = prepare_model(summary or {}, items=prepared)
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        repository.register(SCOPE, component, unit.head, 1)
        assert repository.publish(SCOPE, component, unit.head, 1, model)


def page(book, *, query=None, request=None, component=COMPONENT, company=None):
    with book.storage.read_unit(company or book.company) as read:
        return collection_page(
            SCOPE, component, "entries", query or CollectionFilter(), request or PageQuery(), read
        )


def item(number, *, collection="entries"):
    return IndexItem(
        collection,
        f"item-{number:04d}",
        f"{number:04d}",
        {"number": number, "amount_fen": "9007199254740993"},
        object_id=PARTY if number % 2 == 0 else BANK,
        category="even" if number % 2 == 0 else "odd",
    )


def test_complete_1001_counts_and_filtered_keyset_pages_keep_exact_money(tmp_path):
    book = assemble(tmp_path)
    publish(book, (item(number) for number in range(1001)))
    query = CollectionFilter(object_id=PARTY, categories=("even",))
    first = page(book, query=query)
    assert (first.total_count, first.filtered_count, len(first.items)) == (1001, 501, 20)
    assert [value["number"] for value in first.items] == list(range(0, 40, 2))
    last = page(book, query=query, request=PageQuery(500, first.next_cursor, first.version))
    assert len(last.items) == 481 and last.next_cursor is None
    assert [value["number"] for value in (*first.items, *last.items)] == list(range(0, 1001, 2))
    assert all(value["amount_fen"] == "9007199254740993" for value in last.items)


def test_index_and_payload_keep_the_exact_canonical_binding_for_nested_unicode_values():
    value = {
        "中文": [None, True, False, {"b": '引号"\\换行\n\u2028', "a": -(2**100)}],
        "amount_fen": "9007199254740993",
    }
    selected = IndexItem("entries", "项目", "顺序", value, object_id=PARTY, category="分类")
    expected_index = {
        "collection": "entries",
        "item_id": "项目",
        "sort_key": "顺序",
        "business_id": None,
        "object_id": PARTY,
        "group_key": None,
        "category": "分类",
    }
    digest = content_digest({"index": expected_index, "value": value})
    prepared = prepare_items([selected])
    assert prepared.row_digests == (digest,)
    assert prepared.rows[0][-1] == canonical_json({"digest": digest, "value": value})
    assert prepared.manifest == (("entries", 1, content_digest([["顺序", "项目", digest]])),)


def test_prepared_digest_metadata_keeps_factory_row_order_and_is_detached_from_mutable_values():
    entries = [item(2, collection="z"), item(1, collection="a"), item(3, collection="z")]
    prepared = prepare_items(entries)
    assert tuple(row[0] for row in prepared.rows) == ("z", "a", "z")
    expected = tuple(loads_json(row[-1])["digest"] for row in prepared.rows)
    assert prepared.row_digests == expected
    assert all(type(digest) is str for digest in prepared.row_digests)
    for entry in entries:
        entry.value.clear()
    assert prepared.row_digests == expected
    assert all(loads_json(row[-1])["value"] for row in prepared.rows)


@pytest.mark.parametrize("bad", [1.0, float("nan"), (1,), "\ud800", object()])
def test_preparing_an_index_does_not_bypass_strict_json_value_validation(bad):
    with pytest.raises((ValueError, ContractError)):
        prepare_items([IndexItem("entries", "item", "order", {"nested": [bad]})])


def test_only_the_requested_page_decodes_payload_and_corruption_is_explicit(tmp_path):
    book = assemble(tmp_path)
    publish(book, (item(number) for number in range(25)))
    with book.storage.maintenance_unit(book.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_item SET payload_json=? WHERE scope_key=? AND item_id=?",
            ('{"digest":"bad","value":{"amount_fen":"0"}}', SCOPE, "item-0024"),
        )
    first = page(book)
    assert len(first.items) == 20
    with pytest.raises(ContractError) as error:
        page(book, request=PageQuery(500, first.next_cursor))
    assert error.value.code == "storage_corrupt"


def test_batch_join_uses_one_bounded_item_query_and_preserves_requested_order(
    tmp_path, monkeypatch
):
    book = assemble(tmp_path)
    publish(book, (item(number) for number in range(501)))
    requested = [f"item-{number:04d}" for number in range(499, -1, -1)]
    with book.storage.read_unit(book.company) as read:
        session = read.repository("reading")._session
        original, calls = type(session).fetchall, []

        def counted(owner, sql, parameters=()):
            if "FROM read_item WHERE" in sql:
                calls.append(sql)
            return original(owner, sql, parameters)

        monkeypatch.setattr(type(session), "fetchall", counted)
        values = collection_items(SCOPE, COMPONENT, "entries", requested, read)
        assert [value["number"] for value in values] == list(range(499, -1, -1))
        assert all(value["amount_fen"] == "9007199254740993" for value in values)
        assert len(calls) == 1 and "item_id IN" in calls[0]
        assert collection_items(SCOPE, COMPONENT, "entries", [], read) == ()
        assert len(calls) == 1
        for invalid in (requested + ["item-0500"], ["item-0000", "item-0000"], [[]]):
            with pytest.raises(ContractError) as error:
                collection_items(SCOPE, COMPONENT, "entries", invalid, read)
            assert error.value.code == "invalid_input"


def test_batch_join_checks_expected_references_and_only_decodes_requested_rows(tmp_path):
    book = assemble(tmp_path)
    publish(book, (item(number) for number in range(3)))
    with book.storage.maintenance_unit(book.company) as unit:
        unit.repository("reading")._session.execute(
            "UPDATE read_item SET payload_json='{}' WHERE item_id='item-0002'"
        )
    with book.storage.read_unit(book.company) as read:
        assert collection_items(SCOPE, COMPONENT, "entries", ["item-0001"], read)[0]["number"] == 1
        for requested in (["item-0002"], ["absent"], ["item-0000", "absent"]):
            with pytest.raises(ContractError) as error:
                collection_items(SCOPE, COMPONENT, "entries", requested, read)
            assert error.value.code == "storage_corrupt"


def test_empty_collection_and_unchanged_collection_version_survive_other_collection_changes(
    tmp_path,
):
    book = assemble(tmp_path)
    publish(book, ())
    empty = page(book)
    assert empty.items == () and empty.total_count == 0
    publish(book, (item(1, collection="other"),), summary={"title": "new"})
    current = page(book, request=PageQuery(expected_version=empty.version))
    assert current.version == empty.version and current.total_count == 0
    publish(book, (item(1),))
    with pytest.raises(ContractError) as error:
        page(book, request=PageQuery(expected_version=empty.version))
    assert error.value.code == "snapshot_changed"


def test_prepared_values_are_detached_and_stale_publication_cannot_delete_current_rows(tmp_path):
    book = assemble(tmp_path)
    mutable = item(1)
    prepared = prepare_items([mutable])
    model = prepare_model({}, items=prepared)
    mutable.value["amount_fen"] = "0"
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        old_head = unit.head
        repository.register(SCOPE, COMPONENT, old_head, 1)
        repository.publish(SCOPE, COMPONENT, old_head, 1, model)
    assert page(book).items[0]["amount_fen"] == "9007199254740993"
    book.submit(expense())
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        repository.mark(SCOPE, COMPONENT, unit.head)
        assert not repository.publish(
            SCOPE, COMPONENT, old_head, 1, prepare_model({}, items=prepare_items(()))
        )
        assert (
            loads_json(repository.payload(SCOPE, COMPONENT)["payload_json"])["_collections"][
                "entries"
            ]["count"]
            == 1
        )
    with pytest.raises(ContractError) as error:
        page(book)
    assert error.value.code == "read_model_not_ready"


def test_failed_other_component_publication_rolls_back_deletion_and_cannot_overwrite(tmp_path):
    book = assemble(tmp_path)
    publish(book, [item(1)])
    publish(book, [item(2)], component="other")
    before = page(book, component="other")
    with pytest.raises(sqlite3.IntegrityError):
        publish(book, [item(1)], component="other")
    assert page(book).items[0]["number"] == 1
    assert page(book, component="other") == before


def test_cursor_binds_company_filter_and_collection_not_global_commit_or_page_size(tmp_path):
    book = assemble(tmp_path)
    prepared_items = [item(number) for number in range(25)]
    publish(book, prepared_items)
    first = page(book)
    book.submit(expense())
    assert len(page(book, request=PageQuery(500, first.next_cursor, first.version)).items) == 5
    with pytest.raises(ContractError) as error:
        page(
            book,
            query=CollectionFilter(categories=("even",)),
            request=PageQuery(cursor=first.next_cursor),
        )
    assert error.value.code == "snapshot_changed"
    second = book.storage.create_company(
        CompanySpec(
            str(uuid4()),
            str(uuid4()),
            "913100000000000002",
            "其他合成公司",
            "2026-01",
            tmp_path / "other",
        )
    )
    with book.storage.maintenance_unit(second.company_id) as unit:
        repository = unit.repository("reading")
        repository.register(SCOPE, COMPONENT, unit.head, 1)
        repository.publish(
            SCOPE, COMPONENT, unit.head, 1, prepare_model({}, items=prepare_items(prepared_items))
        )
    with pytest.raises(ContractError) as error:
        page(book, company=second.company_id, request=PageQuery(cursor=first.next_cursor))
    assert error.value.code == "snapshot_changed"


def test_duplicate_identity_is_not_silently_replaced_or_sum_of_partial_page(tmp_path):
    book = assemble(tmp_path)
    first = item(1)
    with pytest.raises(ContractError, match="重复"):
        publish(book, [first, replace(first, value={"amount_fen": "0"})])


def test_caught_python_preparation_failure_cannot_commit_new_rows_under_old_manifest(tmp_path):
    book = assemble(tmp_path)
    publish(book, [item(1)])
    before = page(book)
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        try:
            model = prepare_model({"bad": object()}, items=prepare_items([item(2)]))
            repository.publish(SCOPE, COMPONENT, unit.head, 1, model)
        except ValueError:
            pass
    assert page(book) == before
