from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from shutil import copyfile
from threading import Barrier

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.files import _paths
from ai_accounting_modular.files._repository import FilesRepository
from ai_accounting_modular.files.public import ExpireTask, ProductionError

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


@pytest.mark.parametrize("kind", ["backup", "report_export"])
@pytest.mark.parametrize("resume", ["cleanup", "request", "restart"])
def test_expired_cleanup_resumes_without_hidden_maintenance(file_case, monkeypatch, kind, resume):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company) if kind == "backup" else report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    files.update(
        task["task_id"], "pending", plan["scope_head"], ExpireTask(), company_id=company.company_id
    )
    obsolete = company.company_root / "runtime/file-tasks" / task["task_id"] / "work/obsolete"
    obsolete.parent.mkdir(parents=True)
    obsolete.write_bytes(b"synthetic obsolete output")
    producer.cleanup_failures = 4
    for attempt in (1, 2, 3, 3):
        before = len([event for event in producer.events if event[0] == "cleanup"])
        files.cleanup(company.company_id)
        assert len([event for event in producer.events if event[0] == "cleanup"]) == before + 1
        current = files.get(company.company_id, task["task_id"])
        assert (current["state"], current["attempts"]) == ("expired", attempt)
        assert current["error_code"] == "file_generation_failed"
        assert "path" not in current and obsolete.exists()
    events = len(producer.events)
    files.get(company.company_id, task["task_id"])
    assert files.run_pending(company.company_id) == ()
    assert len(producer.events) == events
    active, successor = files, None
    try:
        if resume == "request":
            with monkeypatch.context() as patch:
                patch.setattr(
                    FilesRepository,
                    "current",
                    lambda *_args, **_kwargs: pytest.fail("a scope handoff must not scan tasks"),
                )
                successor = active.ensure_current(plan, plan["scope_head"], "next request")
        elif resume == "restart":
            files.close()
            active = service(storage, producer)
            active.recover(company.company_id)
        else:
            active.cleanup(company.company_id)
        assert not obsolete.exists()
        with pytest.raises(ContractError) as removed:
            active.get(company.company_id, task["task_id"])
        assert removed.value.code == "not_found"
        if successor is None:
            if kind == "backup":
                plan = backup_plan(storage, company, label="next capture")
            successor = active.ensure_current(plan, plan["scope_head"], "next request")
        assert successor["task_id"] != task["task_id"]
        assert (successor["state"], successor["attempts"]) == ("pending", 0)
        done = active.run(company.company_id, successor["task_id"])
        assert done["state"] == "succeeded"
        with active.verified_download(company.company_id, successor["task_id"]) as download:
            assert download.byte_size == done["byte_size"] and download.stream.read()
    finally:
        if active is not files:
            active.close()


def test_cleanup_error_remains_explicit_until_the_same_request_can_resume(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    files.update(
        task["task_id"], "pending", plan["scope_head"], ExpireTask(), company_id=company.company_id
    )

    def unavailable(_context, _keep_capture):
        raise ProductionError("unsafe_path", "合成清理位置暂不可用。", retryable=False)

    producer.cleanup_hook = unavailable
    for _ in range(4):
        files.cleanup(company.company_id)
    with pytest.raises(ContractError) as blocked:
        files.ensure_current(plan, plan["scope_head"], "resume request")
    assert blocked.value.code == "file_cleanup_pending"
    current = files.get(company.company_id, task["task_id"])
    assert (current["state"], current["attempts"]) == ("expired", 3)
    assert (current["error_code"], current["message"]) == ("unsafe_path", "合成清理位置暂不可用。")
    assert not any(event[0] == "produce" for event in producer.events)
    producer.cleanup_hook = None
    successor = files.ensure_current(plan, plan["scope_head"], "resume request")
    assert files.run(company.company_id, successor["task_id"])["state"] == "succeeded"


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


@pytest.mark.parametrize("resume", ["cleanup", "restart"])
def test_successful_external_backup_cleanup_retries_locked_work_without_changing_delivery(
    file_case, monkeypatch, resume
):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    task_root = company.company_root / "runtime/file-tasks" / task["task_id"]

    def publish(plan, artifact, context):
        destination = company.backup_directory / producer.describe(plan).file_name
        destination.parent.mkdir(exist_ok=True)
        copyfile(context.task_root / artifact.relative_path, destination)
        return destination

    def locked_work(path):
        assert path == task_root
        raise PermissionError("synthetic candidate file is temporarily locked")

    producer.publish_hook = publish
    with monkeypatch.context() as patch:
        patch.setattr(_paths, "remove_task_root", locked_work)
        done = files.run(company.company_id, task["task_id"])
        assert done["state"] == "succeeded" and "error_code" not in done
        assert task_root.exists()
        assert not (company.company_root / "runtime/tasks" / plan["capture_id"]).exists()
        with files.verified_download(company.company_id, task["task_id"]) as download:
            delivered = download.stream.read()

    active = files
    try:
        if resume == "restart":
            files.close()
            active = service(storage, producer)
            active.recover(company.company_id)
        else:
            active.cleanup(company.company_id)
        assert not task_root.exists()
        assert active.get(company.company_id, task["task_id"]) == done
        with active.verified_download(company.company_id, task["task_id"]) as download:
            assert download.stream.read() == delivered
    finally:
        if active is not files:
            active.close()
