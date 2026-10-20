from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from threading import Barrier

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.files.public import ExpireTask

from ._support import SyntheticProducer, backup_plan, report_plan, service


@pytest.mark.parametrize("damage", ["missing", "different_bytes"])
def test_restart_never_recaptures_missing_or_changed_inputs(file_case, damage):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    files.close()
    captured = (
        company.company_root / "runtime/tasks" / plan["capture_id"] / "runtime/company.sqlite3"
    )
    if damage == "missing":
        captured.unlink()
    else:
        captured.write_bytes(b"not the captured database")
    replacement_producer = SyntheticProducer()
    restarted = service(storage, replacement_producer)
    try:
        restarted.recover(company.company_id)
        failed = restarted.get(company.company_id, task["task_id"])
        assert failed["state"] == "failed"
        assert failed["error_code"] == (
            "capture_missing" if damage == "missing" else "capture_invalid"
        )
        assert failed["attempts"] == 0
        assert restarted.ensure_current(plan, plan["scope_head"], "creator") == failed
        assert not any(event[0] == "produce" for event in replacement_producer.events)
    finally:
        restarted.close()


def test_all_current_leases_restore_before_any_expired_cleanup(file_case):
    storage, (company, _), producer, files = file_case
    old_plan = backup_plan(storage, company)
    new_plan = report_plan(company)
    old_head = old_plan["scope_head"]
    old = files.ensure_current(old_plan, old_head, "old")
    files.update(old["task_id"], "pending", old_head, ExpireTask(), company_id=company.company_id)
    current = files.ensure_current(new_plan, new_plan["scope_head"], "current")
    files.close()
    restored_producer = SyntheticProducer()
    restarted = service(storage, restored_producer)

    def require_restored(context, keep_capture):
        assert restored_producer.pins[(company.company_id, None)] == 1

    restored_producer.cleanup_hook = require_restored
    try:
        restarted.recover(company.company_id)
        names = [event[0] for event in restored_producer.events]
        assert names.index("lease") < names.index("cleanup")
        assert restarted.get(company.company_id, current["task_id"])["state"] == "pending"
        with pytest.raises(ContractError):
            restarted.get(company.company_id, old["task_id"])
    finally:
        restarted.close()


def test_cleanup_attempt_budget_requires_explicit_maintenance_after_three(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    files.update(
        task["task_id"], "pending", plan["scope_head"], ExpireTask(), company_id=company.company_id
    )
    producer.cleanup_failures = 3
    for attempt in (1, 2, 3):
        files.cleanup(company.company_id)
        assert files.get(company.company_id, task["task_id"])["attempts"] == attempt
    events = len(producer.events)
    files.cleanup(company.company_id)
    assert len(producer.events) == events
    files.cleanup(company.company_id, retry_failed=True)
    with pytest.raises(ContractError):
        files.get(company.company_id, task["task_id"])


def test_shared_capture_handoff_failure_keeps_full_old_plan_and_pin(file_case):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    producer.failures = 3
    old = files.ensure_current(plan, plan["scope_head"], "old")
    for _ in range(3):
        files.run(company.company_id, old["task_id"])
    producer.cleanup_failures = 1
    with pytest.raises(ContractError) as cleanup:
        files.ensure_current(plan, plan["scope_head"], "new")
    assert cleanup.value.code == "file_cleanup_pending"
    with storage.read_unit(company.company_id) as read:
        row = read.repository("files").get(old["task_id"], plan=True)
    assert row["state"] == "failed" and row["plan_json"] is not None
    assert producer.pins[(company.company_id, plan["capture_id"])] == 1
    assert Path(company.company_root / "runtime/tasks" / plan["capture_id"]).exists()


def test_same_creator_concurrent_different_scopes_cannot_both_install(file_case):
    _, (company, _), producer, files = file_case
    barrier = Barrier(2)
    original = producer.recover_lease

    @contextmanager
    def simultaneous(plan, context):
        with original(plan, context):
            barrier.wait(timeout=10)
            yield

    producer.recover_lease = simultaneous

    def create(period):
        plan = report_plan(company, label=period, period=period)
        try:
            return files.ensure_current(plan, plan["scope_head"], "same creator")
        except ContractError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as pool:
        result = list(pool.map(create, ["2026-01", "2026-02"]))
    assert sum(isinstance(item, dict) for item in result) == 1
    assert "conflict" in result


def test_successful_capture_cleanup_failure_keeps_delivery_and_can_resume(file_case):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    producer.cleanup_failures = 1
    done = files.run(company.company_id, task["task_id"])
    capture = company.company_root / "runtime/tasks" / plan["capture_id"]
    assert done["state"] == "succeeded" and capture.exists()
    assert "error_code" not in done
    files.cleanup(company.company_id)
    assert not capture.exists()
    assert files.get(company.company_id, task["task_id"]) == done
    with files.verified_download(company.company_id, task["task_id"]) as download:
        assert download.byte_size == done["byte_size"]
