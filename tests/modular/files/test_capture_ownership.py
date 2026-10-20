"""Prepared captures transfer to durable tasks or return to their sole owner."""

from __future__ import annotations

from contextlib import contextmanager
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.files.public import CaptureContext, ExpireTask

from ._support import backup_plan, service


def test_same_head_discards_only_the_unaccepted_capture_and_preserves_outputs(file_case):
    storage, (company, _), producer, files = file_case
    original = backup_plan(storage, company)
    task = files.ensure_current(original, original["scope_head"], "creator")
    newer = backup_plan(storage, company)
    original_root = company.company_root / "runtime/tasks" / original["capture_id"]
    newer_root = company.company_root / "runtime/tasks" / newer["capture_id"]
    output_root = company.company_root / "runtime/file-tasks" / task["task_id"]
    output_root.mkdir(parents=True)
    output = output_root / "keep-output.zip"
    output.write_bytes(b"task output is not capture input")

    def check_context(context):
        assert isinstance(context, CaptureContext)
        assert not hasattr(context, "task_id")
        assert context.capture_root == newer_root

    producer.discard_hook = check_context
    assert files.ensure_current(newer, newer["scope_head"], "reuse") == task
    assert original_root.exists() and not newer_root.exists()
    assert output.read_bytes() == b"task output is not capture input"
    assert files.discard_unaccepted_capture(company.company_id, newer["capture_id"]) is True
    assert files.discard_unaccepted_capture(company.company_id, original["capture_id"]) is False
    assert files.run(company.company_id, task["task_id"])["state"] == "succeeded"


@pytest.mark.parametrize("state", ["pending", "running", "failed", "expired", "succeeded"])
def test_current_capture_is_retained_in_every_task_state(file_case, state):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    capture = company.company_root / "runtime/tasks" / plan["capture_id"]
    if state == "succeeded":
        files.run(company.company_id, task["task_id"])
        capture.mkdir(parents=True)
    elif state == "expired":
        files.update(
            task["task_id"],
            "pending",
            plan["scope_head"],
            ExpireTask(),
            company_id=company.company_id,
        )
    elif state != "pending":
        with storage.maintenance_unit(company.company_id) as unit:
            repository = unit.repository("files")
            repository.replace(
                repository.get(task["task_id"]),
                state=state,
                error=("synthetic_failure", "合成失败") if state == "failed" else None,
                attempts=1,
            )
    before = len(producer.events)
    assert files.discard_unaccepted_capture(company.company_id, plan["capture_id"]) is False
    assert capture.exists()
    assert not any(event[0] == "discard" for event in producer.events[before:])


def test_live_unaccepted_pin_also_prevents_capture_discard(file_case):
    storage, (company, _), _, files = file_case
    plan = backup_plan(storage, company)
    metadata = files._prepare(company.company_id)
    task_id = str(uuid4())
    files._acquire_lease(metadata, task_id, plan)
    assert files.discard_unaccepted_capture(company.company_id, plan["capture_id"]) is False
    assert (company.company_root / "runtime/tasks" / plan["capture_id"]).exists()
    files._live(metadata).release(task_id)
    assert files.discard_unaccepted_capture(company.company_id, plan["capture_id"]) is True


def test_discard_after_restart_does_not_clean_expired_tasks_or_their_outputs(file_case):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    files.update(
        task["task_id"],
        "pending",
        plan["scope_head"],
        ExpireTask(),
        company_id=company.company_id,
    )
    output = company.company_root / "runtime/file-tasks" / task["task_id"] / "keep.zip"
    output.parent.mkdir(parents=True)
    output.write_bytes(b"expired task still owns this output")
    files.close()
    restarted = service(storage, producer)
    try:
        assert restarted.discard_unaccepted_capture(company.company_id, plan["capture_id"]) is False
        assert output.read_bytes() == b"expired task still owns this output"
        assert restarted.get(company.company_id, task["task_id"])["state"] == "expired"
    finally:
        restarted.close()


@pytest.mark.parametrize("silent_failure", [False, True])
def test_discard_failure_is_explicit_and_retry_uses_same_locator(
    file_case, monkeypatch, silent_failure
):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    unused = backup_plan(storage, company)
    capture = company.company_root / "runtime/tasks" / unused["capture_id"]
    with monkeypatch.context() as patch:
        if silent_failure:
            patch.setattr(producer, "discard_capture", lambda context: None)
        else:
            producer.cleanup_failures = 1
        with pytest.raises(ContractError) as failed:
            files.ensure_current(unused, unused["scope_head"], "reuse")
    assert failed.value.code == "file_cleanup_pending" and capture.exists()
    assert files.get(company.company_id, task["task_id"]) == task
    assert files.discard_unaccepted_capture(company.company_id, unused["capture_id"]) is True
    assert not capture.exists()


@pytest.mark.parametrize("handoff", [False, True])
def test_committed_task_keeps_its_pin_when_context_exit_loses_the_reply(
    file_case, monkeypatch, handoff
):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    old = None
    if handoff:
        old = files.ensure_current(plan, plan["scope_head"], "old")
        producer.failures = 3
        for _ in range(3):
            files.run(company.company_id, old["task_id"])
    original_unit = storage.maintenance_unit
    lost_reply = RuntimeError("commit succeeded but function exit lost the reply")

    @contextmanager
    def committed_then_exit_raises(company_id):
        with original_unit(company_id) as unit:
            yield unit
        raise lost_reply

    with monkeypatch.context() as patch:
        patch.setattr(storage, "maintenance_unit", committed_then_exit_raises)
        with pytest.raises(RuntimeError) as lost:
            files.ensure_current(plan, plan["scope_head"], "new")
    assert lost.value is lost_reply
    with storage.read_unit(company.company_id) as read:
        current = read.repository("files").scope("backup", "company")
    assert current["state"] == "pending"
    if handoff:
        assert current["task_id"] != old["task_id"]
    assert producer.pins[(company.company_id, plan["capture_id"])] == 1
    assert files.discard_unaccepted_capture(company.company_id, plan["capture_id"]) is False
    receipt = files.ensure_current(plan, plan["scope_head"], "new")
    assert receipt["task_id"] == current["task_id"]
    assert files.run(company.company_id, receipt["task_id"])["state"] == "succeeded"


@pytest.mark.parametrize("read_uncertain", [False, True])
def test_receipt_failure_rolls_back_and_only_confirmed_absence_releases_pin(
    file_case, monkeypatch, read_uncertain
):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    receipt_failure = RuntimeError("strict receipt validation interrupted")

    def invalid_receipt(*args):
        raise receipt_failure

    def uncertain_row(*args, **kwargs):
        raise OSError("cannot establish whether commit survived")

    with monkeypatch.context() as patch:
        patch.setattr(files, "_receipt", invalid_receipt)
        if read_uncertain:
            patch.setattr(files, "_row", uncertain_row)
        with pytest.raises(RuntimeError) as failed:
            files.ensure_current(plan, plan["scope_head"], "creator")
    assert failed.value is receipt_failure
    with storage.read_unit(company.company_id) as read:
        assert read.repository("files").scope("backup", "company") is None
    assert producer.pins[(company.company_id, plan["capture_id"])] == int(read_uncertain)
