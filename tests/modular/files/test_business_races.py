"""File scope locks must not masquerade as business transaction locks."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from threading import Event, local

from ai_accounting_modular.files import _paths

from ._support import report_plan, save_business_object


def test_business_commit_after_last_plain_check_cannot_publish_stale_success(
    file_case, monkeypatch
):
    storage, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    with storage.read_unit(company.company_id) as read:
        expected = read.head
    verified, ordinary_checked, business_done = Event(), Event(), Event()
    verification_count = 0
    paused = False

    snapshot = _paths.snapshot_file

    def hashed_published_file(path):
        nonlocal verification_count
        result = snapshot(path)
        verification_count += 1
        if verification_count == 2:
            verified.set()
        return result

    def current(plan, context):
        nonlocal paused
        with storage.read_unit(company.company_id) as read:
            valid_at_read = read.head == expected
        if verified.is_set() and not paused:
            paused = True
            ordinary_checked.set()
            assert business_done.wait(10)
        return valid_at_read

    monkeypatch.setattr(_paths, "snapshot_file", hashed_published_file)
    producer.is_current = current
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(files.run, company.company_id, task["task_id"])
        assert ordinary_checked.wait(10)
        save_business_object(storage, company)
        business_done.set()
        result = future.result(timeout=10)
    assert result["state"] == "expired"
    with storage.read_unit(company.company_id) as read:
        assert read.head == expected + 1
        row = read.repository("files").get(task["task_id"], plan=True)
    assert row["plan_json"] is None and row["output_path"] is None


def test_final_guard_and_success_cas_hold_actual_sqlite_writer_lock(file_case, monkeypatch):
    storage, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    state = local()
    verified = Event()
    writer_started, writer_done = Event(), Event()
    guard_observed = Event()
    original_maintenance = storage.maintenance_unit
    verification_count = 0
    future = None

    @contextmanager
    def observe_maintenance(company_id):
        with original_maintenance(company_id) as unit:
            state.final_write = verified.is_set()
            try:
                yield unit
            finally:
                state.final_write = False

    storage.maintenance_unit = observe_maintenance

    snapshot = _paths.snapshot_file

    def hashed_published_file(path):
        nonlocal verification_count
        result = snapshot(path)
        verification_count += 1
        if verification_count == 2:
            verified.set()
        return result

    def business():
        writer_started.set()
        save_business_object(storage, company)
        writer_done.set()

    with ThreadPoolExecutor(max_workers=1) as pool:

        def current(plan, context):
            nonlocal future
            if getattr(state, "final_write", False):
                guard_observed.set()
                future = pool.submit(business)
                assert writer_started.wait(10)
                assert not writer_done.wait(0.1)
            return True

        monkeypatch.setattr(_paths, "snapshot_file", hashed_published_file)
        producer.is_current = current
        result = files.run(company.company_id, task["task_id"])
        assert guard_observed.is_set()
        assert result["state"] == "succeeded"
        future.result(timeout=10)
    assert writer_done.is_set()
