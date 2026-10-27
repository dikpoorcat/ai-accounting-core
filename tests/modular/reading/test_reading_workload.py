"""Observe actual selected work, concurrency and recovery without building historical ledgers."""

from dataclasses import replace

import pytest

from ai_accounting_modular.contracts.public import ContractError, ReadInputs
from ai_accounting_modular.reading import _models, _monthly
from ai_accounting_modular.reading._collections import prepare_model
from ai_accounting_modular.reading._publication_guard import CaptureGuard
from ai_accounting_modular.reading._subscriptions import SCOPE, Directory
from ai_accounting_modular.reading._types import ReadingImpact

from ._support import SOURCE, assemble, expense
from .test_report_exports import report_case as report_case


def selection(book, month="2026-01"):
    return _monthly.normalize(
        {"company_id": book.company, "period_from": month, "period_to": month, "view": "as_posted"}
    )


def rows(repository):
    return tuple(
        tuple(tuple(row) for row in repository._session.fetchall(sql))
        for sql in (
            "SELECT * FROM read_scope_head ORDER BY scope_key,component_key",
            "SELECT * FROM read_component ORDER BY scope_key,component_key",
            "SELECT * FROM read_item ORDER BY scope_key,collection_key,item_id",
        )
    )


def test_pending_index_does_not_visit_added_ready_history(tmp_path, monkeypatch):
    book = assemble(tmp_path, established=False)
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        for month in ("2026-01", "2026-02"):
            repository.register(_monthly.key(selection(book, month)), "funds", unit.head, 1)
        # Compare two populated indexes; an entirely absent ready partition can
        # legitimately save the VM's final next-state comparison instruction.
        repository._session.executemany(
            "INSERT INTO read_scope_head(scope_key,component_key,wanted_head,ready_head,"
            "format_version,state) VALUES(?, 'funds',0,0,1,'ready')",
            ((_monthly.key(selection(book, f"{year}-01")),) for year in range(2090, 2100)),
        )

        def no_scopes(*args, **kwargs):
            raise AssertionError("pending must not enumerate all targets")

        monkeypatch.setattr(type(repository), "scopes", no_scopes)

        def measured():
            steps = 0

            def progress():
                nonlocal steps
                steps += 1

            unit._connection.set_progress_handler(progress, 1)
            try:
                result = tuple(_models._pending_targets(repository, 100))
            finally:
                unit._connection.set_progress_handler(None, 0)
            return result, steps

        small, small_steps = measured()
        repository._session.executemany(
            "INSERT INTO read_scope_head(scope_key,component_key,wanted_head,ready_head,"
            "format_version,state) VALUES(?, 'funds',0,0,1,'ready')",
            ((_monthly.key(selection(book, f"{year}-01")),) for year in range(2100, 3100)),
        )
        large, large_steps = measured()
        assert large == small
        assert large_steps == small_steps
        plans = repository._session.fetchall(
            "EXPLAIN QUERY PLAN SELECT * FROM read_scope_head WHERE state='preparing' "
            "ORDER BY scope_key,component_key LIMIT ?",
            (100,),
        )
        assert any("read_scope_pending" in row[3] for row in plans)


def test_real_mark_targets_does_not_enumerate_or_parse_unrelated_months(tmp_path, monkeypatch):
    book = assemble(tmp_path, established=False)
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        key = _monthly.key(selection(book))
        directory = Directory(repository)
        assert directory.commit(
            directory.plan(key, "funds", inputs=ReadInputs(note_scopes=("selected",)))
        )

        def no_scopes(*args, **kwargs):
            raise AssertionError("a real change must locate subscribers instead of all targets")

        monkeypatch.setattr(type(repository), "scopes", no_scopes)
        observed = []
        original = _models.loads_json

        def decoded(raw):
            observed.append(raw)
            return original(raw)

        monkeypatch.setattr(_models, "loads_json", decoded)
        first = book.reading.mark_targets(ReadingImpact(note_scopes=("not-consumed",)), unit)
        first_reads = tuple(observed)
        repository._session.executemany(
            "INSERT INTO read_scope_head(scope_key,component_key,wanted_head,ready_head,"
            "format_version,state) VALUES(?,'funds',0,0,1,'ready')",
            ((_monthly.key(selection(book, f"{year}-01")),) for year in range(2100, 3100)),
        )
        observed.clear()
        second = book.reading.mark_targets(ReadingImpact(note_scopes=("not-consumed",)), unit)
        assert second == first == ()
        assert tuple(observed) == first_reads == ()


@pytest.mark.parametrize(
    ("periods", "months"),
    [
        (("2025-12",), ()),
        (("2025-12", "2026-02"), ("2026-02", "2026-03")),
        (("2026-02",), ("2026-02", "2026-03")),
        ((None,), ("2026-01", "2026-02", "2026-03")),
    ],
)
def test_issue_changes_exclude_history_outside_the_real_context_range(
    tmp_path, monkeypatch, periods, months
):
    book = assemble(tmp_path, established=False)
    component = "brief_context"
    with book.storage.write_unit(book.company) as unit:
        assert unit.start_period == "2026-01"
        repository = unit.repository("reading")
        for month in ("2026-01", "2026-02", "2026-03"):
            key = _monthly.key(selection(book, month))
            version = _monthly.version(component)
            repository.register(key, component, unit.head, version)
            guard = CaptureGuard.capture(repository.state(key, component), unit.head)
            model = replace(prepare_model({"issues": []}), inputs=ReadInputs())
            assert repository.publish(
                key, component, unit.head, version, model, guard=guard, current_head=unit.head
            )

        def no_scopes(*args, **kwargs):
            raise AssertionError("issue changes must not enumerate historical contexts")

        monkeypatch.setattr(type(repository), "scopes", no_scopes)
        selected = book.reading.mark_targets(ReadingImpact(issue_periods=periods), unit)
        assert set(selected) == {_monthly.key(selection(book, month)) for month in months}
        for month in ("2026-01", "2026-02", "2026-03"):
            state = repository.state(_monthly.key(selection(book, month)), component)
            assert state["state"] == ("preparing" if month in months else "ready")


@pytest.mark.parametrize("mature", [False, True])
def test_note_arriving_in_a_new_dependency_window_cannot_publish_stale_capture(tmp_path, mature):
    book = assemble(tmp_path)
    actual = expense()
    book.submit(actual)
    key, component = _monthly.key(selection(book)), "funds"
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        repository.register(key, component, unit.head, 1)
        if mature:
            guard = CaptureGuard.capture(repository.state(key, component), unit.head)
            previous = replace(prepare_model({"note": "old"}), inputs=ReadInputs())
            assert repository.publish(
                key, component, unit.head, 1, previous, guard=guard, current_head=unit.head
            )
    if mature:
        book.submit(expense(name="second"))
        with book.storage.maintenance_unit(book.company) as unit:
            unit.repository("reading").mark(key, component, unit.head)
    with book.storage.read_unit(book.company) as read:
        guard = CaptureGuard.capture(read.repository("reading").state(key, component), read.head)
        candidate = replace(
            prepare_model({"note": "not provided at capture"}),
            inputs=ReadInputs(note_scopes=("business:" + actual["business_id"],)),
        )
    book.submit(
        notes=[
            {
                "scope": "business:" + actual["business_id"],
                "expected_seq": 0,
                "text": "捕获完成后才补上的真实事项说明。",
                "evidence_ids": [SOURCE],
            }
        ]
    )
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        assert guard.matches(repository.state(key, component))
        before = rows(repository)
        assert not repository.publish(
            key, component, guard.wanted_head, 1, candidate, guard=guard, current_head=unit.head
        )
        assert rows(repository) == before


def test_failure_after_subscription_write_rolls_back_the_whole_publication(tmp_path, monkeypatch):
    book = assemble(tmp_path, established=False)
    key, component = _monthly.key(selection(book)), "funds"
    with book.storage.maintenance_unit(book.company) as unit:
        repository = unit.repository("reading")
        repository.register(key, component, unit.head, 1)
        guard = CaptureGuard.capture(repository.state(key, component), unit.head)
        before = rows(repository)
    commit = Directory.commit

    def fail_after_write(directory, plan):
        assert commit(directory, plan)
        raise RuntimeError("synthetic failure after the derived subscription write")

    monkeypatch.setattr(Directory, "commit", fail_after_write)
    with pytest.raises(RuntimeError, match="synthetic failure"):
        with book.storage.maintenance_unit(book.company) as unit:
            candidate = replace(
                prepare_model({"note": "new"}), inputs=ReadInputs(note_scopes=("new",))
            )
            unit.repository("reading").publish(
                key, component, guard.wanted_head, 1, candidate, guard=guard, current_head=unit.head
            )
    with book.storage.read_unit(book.company) as read:
        assert rows(read.repository("reading")) == before


def test_routing_reset_rejects_an_accepted_old_report_file(report_case):
    case = report_case
    preview = case.preview()
    request = case.request(preview)
    job = case.post(request).body["job_id"]
    assert case.kernel.files.run(case.company.company_id, job)["state"] == "succeeded"
    with case.kernel.storage.maintenance_unit(case.company.company_id) as unit:
        unit.repository("reading")._session.execute(
            "DELETE FROM read_item WHERE scope_key=?", (SCOPE,)
        )
    case.apply(amount="12000")
    status = case.status(job)
    assert status.status == 409 and status.body["code"] == "preview_expired"
    with pytest.raises(ContractError) as error:
        with case.kernel.reading.report_download(case.company.company_id, job):
            pytest.fail("a reset Reading must not deliver the accepted old report")
    assert error.value.code == "preview_expired"
    assert case.post(request).body["code"] == "preview_expired"
