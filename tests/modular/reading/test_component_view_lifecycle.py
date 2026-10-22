"""Transaction-owned Activity views release cycles without invalidating detached data."""

import gc
from contextlib import contextmanager, nullcontext
from copy import deepcopy
from weakref import ref

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.reading import _brief_activity_index as activity
from ai_accounting_modular.reading import _component_view, _monthly
from ai_accounting_modular.reading._collections import CollectionFilter, collection_page

from .test_brief_activity import capture as publish_activity
from .test_brief_activity_incremental import book as book
from .test_funds import selection


@contextmanager
def disabled_gc():
    enabled = gc.isenabled()
    gc.disable()
    try:
        yield
    finally:
        if enabled:
            gc.enable()


@pytest.mark.parametrize("exceptional", [False, True])
def test_closed_activity_read_releases_view_directory_and_repository_without_gc(book, exceptional):
    book.seed()
    publish_activity(book)
    key = _monthly.key(selection(book))
    with disabled_gc():
        ending = (
            pytest.raises(RuntimeError, match="synthetic read exit")
            if exceptional
            else nullcontext()
        )
        with ending:
            with book.kernel.storage.read_unit(book.company) as read:
                repository = read.repository("reading")
                view = _component_view.ready(key, activity.COMPONENT, read)
                assert _component_view.ready(key, activity.COMPONENT, read) is view
                directory = view.directory
                assert directory.repository is repository
                data = collection_page(
                    key, activity.COMPONENT, "activity", CollectionFilter(), PageQuery(), read
                )
                assert data.items and data.total_count == 1
                saved_data = deepcopy(data)
                references = ref(view), ref(directory), ref(repository)
                if exceptional:
                    raise RuntimeError("synthetic read exit")
        # Keep the closed context itself alive. Its finished cache must not retain
        # the Repo -> View -> Directory -> Repo cycle until a later GC pass.
        assert read._active is False
        del view, directory, repository
        assert all(reference() is None for reference in references)
        assert data == saved_data and data.items[0]["amount_fen"] == "100000"


def test_a_retained_activity_view_keeps_pure_data_but_cannot_reopen_closed_sql(book):
    book.seed()
    publish_activity(book)
    key = _monthly.key(selection(book))
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        view = _component_view.ready(key, activity.COMPONENT, read)
        directory = view.directory
        pure_summary, pure_manifest = view.monthly_summary(), view.manifest("activity")
        data = collection_page(
            key, activity.COMPONENT, "activity", CollectionFilter(), PageQuery(), read
        )
        unloaded = next(
            collection + ":" + bucket
            for collection, buckets in directory.roots.items()
            for bucket in buckets
            if collection + ":" + bucket not in directory.loaded
        )
    assert repository._component_views == {}
    assert view.monthly_summary() == pure_summary and view.manifest("activity") == pure_manifest
    assert data.total_count == len(data.items) == 1
    with pytest.raises(ContractError) as repository_error:
        repository.state(key, activity.COMPONENT)
    assert repository_error.value.code == "invalid_operation"
    with pytest.raises(ContractError) as directory_error:
        directory.load((unloaded,))  # A real uncached bucket requires the closed SQL capability.
    assert directory_error.value.code == "invalid_operation"
    with pytest.raises(ContractError) as read_error:
        _component_view.ready(key, activity.COMPONENT, read)
    assert read_error.value.code == "invalid_operation"
