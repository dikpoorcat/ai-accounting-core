"""Checked Activity bytes remain reusable without trusting mutable display dictionaries."""

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.reading import _brief_activity_index as index

from .test_brief_activity import capture as publish_full
from .test_brief_activity_incremental import book as book
from .test_brief_activity_incremental import change, matches_complete, state
from .test_funds import apply, initialize


def test_selected_values_are_detached_and_only_changed_rows_are_prepared(book, monkeypatch):
    initialize(book)
    first, second = apply(book, book.expense("100"), book.expense("200"))
    publish_full(book)
    with book.kernel.storage.read_unit(book.company) as read:
        base = index.base(read.repository("reading"), state(book))
        root = base.get(index.ROOTS, (first["business_id"],), required=True)[first["business_id"]]
        member_id = root["members"][0]
        member = base.get("activity_members", (member_id,), required=True)[member_id]
        member["row"]["amount_fen"] = "0"
        member["scope"]["source"]["business_id"] = second["business_id"]
        assert base.get("activity_members", (member_id,), required=True)[member_id] != member
    first["data"]["amount_fen"] = "120"
    change(book, first)
    original, prepared = index.prepare_items, []

    def changed_only(items, **kwargs):
        items = tuple(items)
        assert all(
            item.collection == "activity" or item.business_id == first["business_id"]
            for item in items
        )
        prepared.extend(items)
        return original(items, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(index, "prepare_items", changed_only)
        assert book.kernel.reading.run_pending(book.company)
    assert any(item.collection == "activity_members" for item in prepared)
    assert not any(item.business_id == second["business_id"] for item in prepared)
    matches_complete(book)


@pytest.mark.parametrize(
    "field,value", [("scope_key", "other"), ("component_key", "other"), ("head", -1)]
)
def test_reusable_rows_still_belong_to_the_exact_scope_component_and_head(
    book, monkeypatch, field, value
):
    initialize(book)
    apply(book, book.expense("100"))
    publish_full(book)
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        base = index.base(repository, state(book))
        root = base.get(index.ROOTS, base.payload["_inputs"]["business_ids"])
        member_id = next(identity for value in root.values() for identity in value["members"])
        fetch = repository.item_get

        def damaged(*args):
            rows = [dict(row) for row in fetch(*args)]
            if args[2] == "activity_members":
                rows[0][field] = value
            return rows

        monkeypatch.setattr(repository, "item_get", damaged)
        with pytest.raises(ContractError) as error:
            base.get("activity_members", (member_id,), required=True)
        assert error.value.code == "storage_corrupt"
