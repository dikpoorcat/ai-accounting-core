"""An ended Open read releases its views without waiting for cyclic collection."""

import gc
import weakref

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.reading import _brief_open_index as index
from ai_accounting_modular.reading import _component_view, _monthly, _open_store
from ai_accounting_modular.reading._collections import CollectionFilter, collection_page

from .test_open_failure_guards import book as book
from .test_open_failure_guards import page, scope


def prepare(book):
    book.seed()
    assert page(book).state == "preparing"
    assert book.kernel.reading.run_pending(book.company)
    assert page(book).state == "ready"


def test_ended_open_read_releases_repository_and_directory_without_gc(book):
    enabled = gc.isenabled()
    gc.disable()
    try:
        prepare(book)
        with book.kernel.storage.read_unit(book.company) as read:
            repository = read.repository("reading")
            key = _monthly.key(scope(book))
            view = _component_view.ready(key, index.COMPONENT, read)
            assert _component_view.ready(key, index.COMPONENT, read) is view
            assert view.directory is not None
            result = collection_page(
                key, index.COMPONENT, "open_items", CollectionFilter(), PageQuery(), read
            )
            assert result.total_count == len(result.items) == 1
            repository_ref = weakref.ref(repository)
            directory_ref = weakref.ref(view.directory)
        # Retain the closed context deliberately, but no local repository/view.
        del repository, view
        assert repository_ref() is None
        assert directory_ref() is None
        assert read.head > 0
    finally:
        if enabled:
            gc.enable()


def test_retained_open_pool_cannot_read_after_its_transaction_ends(book):
    prepare(book)
    with book.kernel.storage.read_unit(book.company) as read:
        view = _component_view.ready(_monthly.key(scope(book)), index.COMPONENT, read)
        pool = view.directory.pool
        key = (_open_store.NODES, view.directory.roots["rows"].digest)
        assert pool.get_blocks((key,))[0] is not None
    with pytest.raises(ContractError) as ended:
        pool.get_blocks((key,))
    assert ended.value.code == "invalid_operation"
