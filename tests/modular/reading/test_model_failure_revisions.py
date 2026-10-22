"""Only failures of the captured lifecycle revision may change a derived target."""

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.reading import _brief_activity, _brief_groups, _models, _monthly

from .test_open_failure_guards import before_next_maintenance, scope
from .test_open_failure_guards import book as book


def quarter_scope(book):
    return {
        "company_id": book.company,
        "period_from": "2026-01",
        "period_to": "2026-03",
        "view": "as_posted",
    }


def target(book, kind):
    if kind == "quarterly":
        return _models.key(_models.normalize(quarter_scope(book))), _models.FINANCIAL
    return _monthly.key(scope(book)), _monthly.BRIEF_ACTIVITY


def page(book, kind="activity"):
    if kind == "quarterly":
        return book.kernel.reading.page("quarterly_report", quarter_scope(book), {})
    return _monthly.deliver(
        book.kernel.reading,
        scope(book),
        (_monthly.BRIEF_ACTIVITY,),
        lambda snapshot, read: {"activity": _brief_groups.activity_page(snapshot, read, {})},
    )


def ready(book, kind="activity"):
    for _ in range(4):
        result = page(book, kind)
        if result.state == "ready":
            if kind == "quarterly":
                assert result.payload["snapshot"]["profit_statement"]["32"]["current"] == {
                    "state": "known",
                    "amount_fen": "-100000",
                }
            else:
                groups = result.payload["activity"]["items"]
                assert len(groups) == 1 and groups[0]["amount_fen"] == "100000"
            return result
        assert result.state == "preparing", result
        assert book.kernel.reading.run_pending(book.company)
    pytest.fail("the real target did not become ready")


def saved(book, kind="activity"):
    with book.kernel.storage.read_unit(book.company) as read:
        repository = read.repository("reading")
        rows = tuple(
            tuple(tuple(row) for row in repository._session.fetchall(sql))
            for sql in (
                "SELECT * FROM read_scope_head ORDER BY scope_key,component_key",
                "SELECT * FROM read_component ORDER BY scope_key,component_key",
                "SELECT * FROM read_item ORDER BY scope_key,collection_key,item_id",
            )
        )
        return read.head, repository.state(*target(book, kind)), rows


def fail(repository, state):
    assert repository.fail(
        state["scope_key"],
        state["component_key"],
        state["wanted_head"],
        "storage_corrupt",
        expected_revision=state["revision"],
        expected_state=state["state"],
        expected_format=state["format_version"],
    )


@pytest.mark.parametrize("renew", (False, True), ids=("same-revision", "renewed-revision"))
def test_activity_prepare_failure_cannot_fail_a_same_head_retried_task(book, monkeypatch, renew):
    book.seed()
    assert page(book).state == "preparing"
    before, failed_reads, replacement = saved(book), [], []

    def failed_capture(reader, selected, read, **kwargs):
        failed_reads.append(read)
        raise OSError("an old Activity capture failed before returning its prepared model")

    def intervene(maintenance):
        assert len(failed_reads) == 1
        with pytest.raises(ContractError) as error:
            failed_reads[0].repository("reading")
        assert error.value.code == "invalid_operation"
        if renew:
            with maintenance(book.company) as unit:
                repository = unit.repository("reading")
                current = repository.state(*target(book, "activity"))
                fail(repository, current)
                repository.retry(
                    current["scope_key"],
                    current["component_key"],
                    format_version=current["format_version"],
                )
        replacement.append(saved(book))
        assert replacement[0][0] == before[0]
        assert {key: value for key, value in replacement[0][1].items() if key != "revision"} == {
            key: value for key, value in before[1].items() if key != "revision"
        }
        if renew:
            assert replacement[0][1]["revision"] > before[1]["revision"]
        else:
            assert replacement[0][1] == before[1]

    with monkeypatch.context() as patch:
        patch.setattr(_brief_activity, "capture", failed_capture)
        calls = before_next_maintenance(book, patch, intervene)
        assert book.kernel.reading.run_pending(book.company) == ()
    assert calls == [book.company] and len(replacement) == 1
    after = saved(book)
    if not renew:
        assert after[0] == before[0]
        assert after[1]["state"] == "failed"
        assert after[1]["error_code"] == "production_failed"
        assert after[1]["revision"] > before[1]["revision"]
        return
    assert after == replacement[0], "the failed/retried task is another lifecycle revision"
    ready(book)
    assert saved(book)[0] == before[0]


@pytest.mark.parametrize("kind", ("activity", "quarterly"))
@pytest.mark.parametrize("renew", (False, True), ids=("same-revision", "rebuilt-revision"))
def test_late_page_failure_cannot_fail_a_same_head_rebuilt_ready_target(
    book, monkeypatch, kind, renew
):
    book.seed()
    initial = ready(book, kind)
    before = saved(book, kind)
    with book.kernel.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        key, component = target(book, kind)
        if kind == "quarterly":
            changed = repository._session.execute(
                "UPDATE read_component SET payload_json='{}' WHERE scope_key=? AND component_key=?",
                (key, component),
            )
        else:
            row = repository._session.fetchone(
                "SELECT item_id FROM read_item WHERE scope_key=? AND component_key=? "
                "AND collection_key='activity' ORDER BY sort_key,item_id LIMIT 1",
                (key, component),
            )
            assert row is not None
            changed = repository._session.execute(
                "UPDATE read_item SET payload_json='{}' WHERE scope_key=? AND component_key=? "
                "AND collection_key='activity' AND item_id=?",
                (key, component, row["item_id"]),
            )
        assert changed == 1
    replacement = []

    def intervene(maintenance):
        if renew:
            with maintenance(book.company) as unit:
                repository = unit.repository("reading")
                fail(repository, repository.state(*target(book, kind)))
            if kind == "quarterly":
                book.kernel.reading.retry("quarterly_report", quarter_scope(book))
            else:
                _monthly.retry(book.kernel.reading, scope(book), (_monthly.BRIEF_ACTIVITY,))
            rebuilt = ready(book, kind)
            assert rebuilt == initial, "technical revision must not change the public version"
        replacement.append(saved(book, kind))
        assert replacement[0][0] == before[0]
        assert replacement[0][1]["wanted_head"] == before[1]["wanted_head"]
        assert replacement[0][1]["state"] == "ready"
        if renew:
            assert replacement[0][1]["revision"] > before[1]["revision"]
        else:
            assert replacement[0][1] == before[1]

    with monkeypatch.context() as patch:
        calls = before_next_maintenance(book, patch, intervene)
        stale = page(book, kind)
    assert calls == [book.company] and len(replacement) == 1
    assert stale.state == "failed" and stale.reason == "corrupt"
    after = saved(book, kind)
    if not renew:
        assert after[0] == before[0]
        assert after[1]["state"] == "failed"
        assert after[1]["error_code"] == "storage_corrupt"
        assert after[1]["revision"] > before[1]["revision"]
        return
    assert after == replacement[0], "the stale failure must not write into the rebuilt revision"
    assert page(book, kind) == initial
