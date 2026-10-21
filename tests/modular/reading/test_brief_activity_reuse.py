"""Checked Activity bytes remain reusable without trusting mutable display dictionaries."""

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.reading import _brief_activity as activity
from ai_accounting_modular.reading import _brief_activity_index as index
from ai_accounting_modular.reading import _monthly

from .test_brief_activity import capture as publish_full
from .test_brief_activity_incremental import book as book
from .test_brief_activity_incremental import change, matches_complete, state
from .test_funds import apply, initialize, selection


def test_unchanged_values_are_read_only_and_only_changed_rows_are_prepared(book, monkeypatch):
    initialize(book)
    first, second = apply(book, book.expense("100"), book.expense("200"))
    publish_full(book)
    with book.kernel.storage.read_unit(book.company) as read:
        base = index.base(read.repository("reading"), state(book))
    member = next(item for item in base if item.collection == "activity_members")
    with pytest.raises(TypeError):
        member.value["row"]["amount_fen"] = "0"
    with pytest.raises(TypeError):
        member.value["scope"]["source"]["business_id"] = first["business_id"]
    first["data"]["amount_fen"] = "120"
    change(book, first)
    original, prepared = activity.prepare_items, []

    def changed_only(items, **kwargs):
        items = tuple(items)
        assert all(
            item.collection == "activity" or item.business_id == first["business_id"]
            for item in items
        )
        prepared.extend(items)
        return original(items, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(activity, "prepare_items", changed_only)
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
        actual = repository.activity_rows(_monthly.key(selection(book)))
        rows = [dict(row) for row in actual]
        rows[0][field] = value
        monkeypatch.setattr(repository, "activity_rows", lambda key: rows)
        with pytest.raises(ContractError) as error:
            index.base(repository, state(book))
        assert error.value.code == "storage_corrupt"
