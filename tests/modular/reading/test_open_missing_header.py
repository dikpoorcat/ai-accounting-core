"""A missing published header cannot discard its shared graph ownership proof."""

from collections import Counter
from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.reading import (
    _brief_open_index as index,
)
from ai_accounting_modular.reading import (
    _brief_open_inputs,
    _brief_open_sources,
    _monthly,
    _open_store,
)
from ai_accounting_modular.storage.public import TreeStore, block_digest

from .test_open_failure_guards import book as book
from .test_open_failure_guards import page, saved, scope


def ready(book):
    result = page(book)
    if result.state == "preparing":
        assert book.kernel.reading.run_pending(book.company)
        result = page(book)
    assert result.state == "ready", result
    return result


def assert_live_pool(book):
    """Count each actual parent edge once, plus every published root occurrence."""
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        pool = _open_store.OpenPool(repository)
        rows = repository._session.fetchall(
            "SELECT * FROM read_item WHERE scope_key=? AND collection_key IN (?,?) "
            "ORDER BY collection_key,item_id",
            (_open_store.SCOPE, _open_store.NODES, _open_store.REFS),
        )
        keys = tuple((row["collection_key"], row["item_id"]) for row in rows)
        assert pool.get_blocks(keys) == tuple(row["payload_json"] for row in rows)
        nodes = {
            row["item_id"]: row["payload_json"]
            for row in rows
            if row["collection_key"] == _open_store.NODES
        }
        refs = {
            row["item_id"]: loads_json(row["payload_json"])
            for row in rows
            if row["collection_key"] == _open_store.REFS
        }
        headers = repository._session.fetchall(
            "SELECT payload_json FROM read_component WHERE component_key=?", (index.COMPONENT,)
        )
        roots = [
            root[0]
            for header in headers
            for root in loads_json(header["payload_json"])["_roots"].values()
            if root is not None
        ]
        expected, seen, pending = Counter(roots), set(), list(roots)
        while pending:
            identity = pending.pop()
            if identity in seen:
                continue
            seen.add(identity)
            raw = nodes[identity]
            assert block_digest(raw) == identity
            children = [child[0] for child in loads_json(raw).get("children", ())]
            expected.update(children)
            pending.extend(children)
        assert set(nodes) == set(refs) == seen, "only the current published graph may remain"
        assert refs == {
            identity: {"format": TreeStore.REF_FORMAT, "node": identity, "count": count}
            for identity, count in expected.items()
        }


def delete_header(book):
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        assert (
            unit.repository("reading")._session.execute(
                "DELETE FROM read_component WHERE scope_key=? AND component_key=?",
                (_monthly.key(scope(book)), index.COMPONENT),
            )
            == 1
        )


def empty_base(book):
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        state = repository.state(_monthly.key(scope(book)), index.COMPONENT)
        assert state["ready_head"] == 0 and state["content_digest"] is None
        assert index.base(repository, state) is None


def test_missing_published_header_fails_notification_then_reset_reclaims_the_old_graph(book):
    fact = book.seed()
    initial = ready(book)
    before = saved(book)
    assert initial.payload["open_items"]["payable_fen"] == "100000"
    delete_header(book)
    revised = deepcopy(fact)
    revised["data"]["amount_fen"] = "90000"
    result = book.app.accounting_apply(
        book.request(mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}])
    )
    assert result["completion"] == "complete", result
    marked = saved(book)
    assert marked[2]["state"] == "failed" and marked[2]["error_code"] == "storage_corrupt"
    formal = book.get(fact["business_id"])
    _monthly.retry(book.kernel.reading, scope(book), (index.COMPONENT,))
    reset = saved(book)
    assert reset[0] == marked[0] and reset[1] != before[1]
    assert reset[2]["revision"] > marked[2]["revision"]
    with book.kernel.storage.read_unit(book.company) as read:
        assert _brief_open_inputs.pending(
            read.repository("reading"), _monthly.key(scope(book))
        ) == (
            True,
            set(),
        )
    rebuilt = ready(book)
    assert rebuilt.payload["open_items"]["payable_fen"] == "90000"
    assert saved(book)[0] == marked[0] and book.get(fact["business_id"]) == formal
    assert_live_pool(book)
    result = book.app.accounting_apply(
        book.request(
            mutations=[{"op": "withdraw", "expected_seq": 2, "business_id": fact["business_id"]}]
        )
    )
    assert result["completion"] == "complete", result
    withdrawn = book.get(fact["business_id"])
    head = saved(book)[0]
    final = ready(book)
    assert final.payload["open_items"]["payable_fen"] == "0"
    assert final.payload["open_items"]["total_count"] == 0
    assert saved(book)[0] == head and book.get(fact["business_id"]) == withdrawn
    assert_live_pool(book)


def test_head_zero_published_empty_header_is_not_an_initial_empty_base(book):
    initial = ready(book)
    before = saved(book)
    assert before[0] == before[2]["ready_head"] == 0
    assert before[2]["content_digest"] is not None
    delete_header(book)
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        with pytest.raises(ContractError) as error:
            index.base(repository, repository.state(_monthly.key(scope(book)), index.COMPONENT))
        assert error.value.code == "storage_corrupt"
    assert page(book).state == "failed"
    _monthly.retry(book.kernel.reading, scope(book), (index.COMPONENT,))
    empty_base(book)
    assert ready(book) == initial
    assert saved(book)[0] == 0
    assert_live_pool(book)


def test_initial_first_failure_and_explicit_reset_can_have_no_published_header(book, monkeypatch):
    book.seed()
    assert page(book).state == "preparing"
    empty_base(book)

    def failed_capture(*args, **kwargs):
        raise OSError("first capture failed without publishing a header")

    with monkeypatch.context() as patch:
        patch.setattr(_brief_open_sources, "capture", failed_capture)
        assert book.kernel.reading.run_pending(book.company) == ()
    assert saved(book)[2]["state"] == "failed"
    empty_base(book)
    _monthly.retry(book.kernel.reading, scope(book), (index.COMPONENT,))
    empty_base(book)
    initial = ready(book)
    assert initial.payload["open_items"]["payable_fen"] == "100000"
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        _open_store.reset(unit.repository("reading"), unit.head, index.FORMAT_VERSION)
    empty_base(book)
    assert ready(book) == initial
    assert_live_pool(book)
