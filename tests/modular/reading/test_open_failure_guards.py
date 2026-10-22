"""A stale Open failure must not damage a same-head replacement graph or scope."""

from contextlib import contextmanager

import pytest

from ai_accounting_modular.contracts.public import ContractError, loads_json
from ai_accounting_modular.reading import (
    _brief_open,
    _brief_open_inputs,
    _brief_open_sources,
    _monthly,
    _open_store,
)
from ai_accounting_modular.reading import (
    _brief_open_index as index,
)
from tests.modular.acceptance.performance.ledger_variant import MODES, install_ledger_implementation
from tests.modular.application._working_case import case as working_case


@pytest.fixture(params=MODES)
def book(tmp_path, request):
    with install_ledger_implementation(request.param):
        fixture = working_case.__wrapped__(tmp_path)
        value = next(fixture)
        try:
            yield value
        finally:
            fixture.close()


def scope(book, month="2026-01"):
    return _monthly.normalize(
        {"company_id": book.company, "period_from": month, "period_to": month, "view": "as_posted"}
    )


def page(book, month="2026-01"):
    return _monthly.deliver(
        book.kernel.reading,
        scope(book, month),
        (index.COMPONENT,),
        lambda snapshot, read: _brief_open.page(snapshot, read, {}),
    )


def saved(book):
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        pool = _open_store.OpenPool(repository)
        state = repository.state(_monthly.key(scope(book)), index.COMPONENT)
        rows = tuple(
            tuple(tuple(row) for row in repository._session.fetchall(sql))
            for sql in (
                "SELECT * FROM read_scope_head ORDER BY scope_key,component_key",
                "SELECT * FROM read_component ORDER BY scope_key,component_key",
                "SELECT * FROM read_item ORDER BY scope_key,collection_key,item_id",
            )
        )
        return read.head, pool.token, state, rows


def reset(book, maintenance):
    with maintenance(book.company) as unit:
        _open_store.reset(unit.repository("reading"), unit.head, index.FORMAT_VERSION)


def before_next_maintenance(book, monkeypatch, intervene):
    """The actual failure transaction starts only after the competing writer commits."""
    original = book.kernel.storage.maintenance_unit
    calls = []

    @contextmanager
    def interposed(company):
        assert company == book.company
        if not calls:
            calls.append(company)
            intervene(original)
        with original(company) as unit:
            yield unit

    monkeypatch.setattr(book.kernel.storage, "maintenance_unit", interposed)
    return calls


@pytest.mark.parametrize("replace_pool", (False, True), ids=("same-pool", "reset-pool"))
def test_prepare_failure_cannot_fail_a_same_head_reset_task(book, monkeypatch, replace_pool):
    book.seed()
    assert page(book).state == "preparing"
    reset(book, book.kernel.storage.maintenance_unit)
    before = saved(book)
    failed_reads, replacement = [], []

    def failed_capture(reader, selected, read, **kwargs):
        failed_reads.append(read)
        raise OSError("the old Open capture failed before producing a model")

    def intervene(maintenance):
        assert len(failed_reads) == 1
        with pytest.raises(ContractError) as error:
            failed_reads[0].repository("reading")
        assert error.value.code == "invalid_operation", "the old read must already be closed"
        if replace_pool:
            reset(book, maintenance)
        replacement.append(saved(book))
        assert replacement[0][0] == before[0]
        assert {key: value for key, value in replacement[0][2].items() if key != "revision"} == {
            key: value for key, value in before[2].items() if key != "revision"
        }, "wanted/state/format alone cannot detect reset"
        if replace_pool:
            assert replacement[0][2]["revision"] > before[2]["revision"]
        else:
            assert replacement[0][2]["revision"] == before[2]["revision"]
        assert (replacement[0][1] != before[1]) is replace_pool

    with monkeypatch.context() as patch:
        patch.setattr(_brief_open_sources, "capture", failed_capture)
        calls = before_next_maintenance(book, patch, intervene)
        assert book.kernel.reading.run_pending(book.company) == ()
    assert calls == [book.company] and len(replacement) == 1
    after = saved(book)
    if not replace_pool:
        assert after[0:2] == before[0:2]
        assert after[2]["state"] == "failed"
        assert after[2]["error_code"] == "production_failed"
        assert after[2]["revision"] > before[2]["revision"]
        return
    assert after == replacement[0], "an old failure must leave the new full task untouched"
    with book.kernel.storage.read_unit(book.company) as read:
        full, dirty = _brief_open_inputs.pending(
            read.repository("reading"), _monthly.key(scope(book))
        )
        assert full and not dirty
    assert book.kernel.reading.run_pending(book.company)
    ready = page(book)
    assert ready.state == "ready"
    assert ready.payload["open_items"]["payable_fen"] == "100000"
    assert saved(book)[0] == before[0]


def test_late_corrupt_page_failure_cannot_fail_a_rebuilt_same_head_ready_model(book, monkeypatch):
    book.seed()
    assert page(book).state == "preparing"
    assert book.kernel.reading.run_pending(book.company)
    initial = page(book)
    assert initial.state == "ready" and initial.payload["open_items"]["payable_fen"] == "100000"
    before = saved(book)
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        payload = repository.payload(_monthly.key(scope(book)), index.COMPONENT)
        root = loads_json(payload["payload_json"])["_roots"]["rows"][0]
        assert (
            repository._session.execute(
                "UPDATE read_item SET payload_json='{}' "
                "WHERE scope_key=? AND collection_key=? AND item_id=?",
                (_open_store.SCOPE, _open_store.NODES, root),
            )
            == 1
        )
    replacement = []

    def intervene(maintenance):
        reset(book, maintenance)
        assert book.kernel.reading.run_pending(book.company)
        rebuilt = page(book)
        assert rebuilt.state == "ready" and rebuilt.version == initial.version
        assert rebuilt.payload == initial.payload
        replacement.append(saved(book))
        assert replacement[0][0] == before[0]
        assert replacement[0][1] != before[1]
        assert replacement[0][2]["state"] == "ready"
        assert replacement[0][2]["wanted_head"] == before[2]["wanted_head"]
        assert replacement[0][2]["revision"] > before[2]["revision"]

    with monkeypatch.context() as patch:
        calls = before_next_maintenance(book, patch, intervene)
        stale = page(book)
    assert calls == [book.company] and len(replacement) == 1
    assert stale.state == "failed" and stale.reason == "corrupt"
    assert saved(book) == replacement[0], "a stale failure cannot overwrite the rebuilt revision"
    assert page(book) == initial


def test_full_candidate_cannot_be_published_to_another_same_head_scope(book):
    book.seed()
    assert page(book).state == page(book, "2026-02").state == "preparing"
    with book.kernel.storage.read_unit(book.company) as read:
        candidate = _brief_open.prepare(book.kernel.reading, scope(book), read)
        january = read.repository("reading").state(_monthly.key(scope(book)), index.COMPONENT)
        february = read.repository("reading").state(
            _monthly.key(scope(book, "2026-02")), index.COMPONENT
        )
        assert january["wanted_head"] == february["wanted_head"] == read.head
    assert candidate.scope_key == _monthly.key(scope(book))
    before = saved(book)
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        assert not unit.repository("reading").publish(
            _monthly.key(scope(book, "2026-02")),
            index.COMPONENT,
            february["wanted_head"],
            index.FORMAT_VERSION,
            candidate,
        )
    assert saved(book) == before
