"""Explicit current-status checks never execute work or change default get behavior."""

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event

import pytest

from ai_accounting_modular.contracts.public import ContractError

from ._support import backup_plan, report_plan


@pytest.mark.parametrize("completed", [False, True])
def test_status_fences_pending_and_succeeded_without_production(file_case, completed):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    if completed:
        files.run(company.company_id, task["task_id"])
    count = len(producer.events)
    producer.current_heads = set()
    assert files.get(company.company_id, task["task_id"])["state"] == (
        "succeeded" if completed else "pending"
    )
    expired = files.get(company.company_id, task["task_id"], check_current=True)
    assert expired["state"] == "expired" and "path" not in expired
    assert files.get(company.company_id, task["task_id"], check_current=True) == expired
    assert len(producer.events) == count


def test_running_status_can_expire_before_old_worker_confirms(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    entered, proceed = Event(), Event()

    def pause(*args):
        entered.set()
        assert proceed.wait(10)

    producer.produce_hook = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(files.run, company.company_id, task["task_id"])
        try:
            assert entered.wait(10)
            assert files.get(company.company_id, task["task_id"])["state"] == "running"
            producer.current_heads = set()
            assert (
                files.get(company.company_id, task["task_id"], check_current=True)["state"]
                == "expired"
            )
        finally:
            proceed.set()
        assert future.result(timeout=10)["state"] == "expired"


def test_default_backup_get_and_replaced_task_identity_are_preserved(file_case):
    storage, (company, _), producer, files = file_case
    first = backup_plan(storage, company)
    old = files.ensure_current(first, first["scope_head"], "first")
    producer.current_heads = set()
    assert files.get(company.company_id, old["task_id"]) == old
    assert files.get(company.company_id, old["task_id"], check_current=True)["state"] == "expired"
    newer = backup_plan(storage, company, label="newer")
    producer.current_heads = {newer["scope_head"]}
    new = files.ensure_current(newer, newer["scope_head"], "newer")
    with pytest.raises(ContractError) as error:
        files.get(company.company_id, old["task_id"], check_current=True)
    assert error.value.code == "not_found"
    assert files.get(company.company_id, new["task_id"], check_current=True) == new


def test_current_scope_returns_isolated_plan_and_checks_actual_delivery(file_case):
    _, (company, other), producer, files = file_case
    plan = report_plan(company)
    key = producer.describe(plan).key
    assert files.current_plan(company.company_id, "report_export", key) is None
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    current = files.current_plan(company.company_id, "report_export", key)
    assert current.plan == plan and current.receipt == task
    assert files.current_plan(other.company_id, "report_export", key) is None
    current.plan["scope_head"] = "f" * 64
    assert files.current_plan(company.company_id, "report_export", key).plan == plan
    done = files.run(company.company_id, task["task_id"])
    assert files.current_plan(company.company_id, "report_export", key).receipt == done
    producer.current_heads = set()
    expired = files.current_plan(company.company_id, "report_export", key)
    assert expired.plan is None and expired.receipt["state"] == "expired"


@pytest.mark.parametrize("damage", ["missing", "changed"])
@pytest.mark.parametrize("query", ["scope", "task"])
def test_checked_status_cannot_report_unavailable_or_changed_file_ready(file_case, damage, query):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    done = files.run(company.company_id, task["task_id"])
    path = Path(done["path"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"changed output bytes")
    assert files.get(company.company_id, task["task_id"])["state"] == "succeeded"
    if query == "scope":
        state = files.current_plan(company.company_id, "report_export", producer.describe(plan).key)
        assert state.plan == plan
        receipt = state.receipt
    else:
        receipt = files.get(company.company_id, task["task_id"], check_current=True)
    assert receipt["state"] == "failed" and receipt["error_code"] == "file_invalid"
    assert "path" not in receipt


def test_scope_status_preserves_success_on_temporary_file_lock(file_case, monkeypatch):
    from ai_accounting_modular.files import _paths

    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    done = files.run(company.company_id, task["task_id"])
    with monkeypatch.context() as patch:

        def unavailable(path):
            raise PermissionError("temporary synthetic lock")

        patch.setattr(_paths, "snapshot_file", unavailable)
        with pytest.raises(ContractError) as error:
            files.current_plan(company.company_id, "report_export", producer.describe(plan).key)
        assert error.value.code == "temporary_failure"
    assert files.get(company.company_id, task["task_id"]) == done


def test_producer_validation_error_cannot_be_swallowed_as_success(file_case, monkeypatch):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    files.run(company.company_id, task["task_id"])

    def invalid(*args):
        raise ContractError("file_invalid", "合成生产者无法核实当前依据。")

    monkeypatch.setattr(producer, "is_current", invalid)
    with pytest.raises(ContractError) as error:
        files.current_plan(company.company_id, "report_export", producer.describe(plan).key)
    assert error.value.code == "file_invalid"


@pytest.mark.parametrize("damage", ["missing", "changed"])
@pytest.mark.parametrize("same_request", [False, True])
def test_ensure_checks_saved_success_before_reusing_or_recreating(file_case, damage, same_request):
    _, (company, _), _, files = file_case
    plan = report_plan(company)
    first = files.ensure_current(plan, plan["scope_head"], "creator")
    completed = files.run(company.company_id, first["task_id"])
    path = Path(completed["path"])
    if damage == "missing":
        path.unlink()
    else:
        path.write_bytes(b"not the originally verified output")
    receipt = files.ensure_current(
        plan, plan["scope_head"], "creator" if same_request else "explicit retry"
    )
    if same_request:
        assert receipt["task_id"] == first["task_id"]
        assert receipt["state"] == "failed" and receipt["error_code"] == "file_invalid"
    else:
        assert receipt["task_id"] != first["task_id"] and receipt["state"] == "pending"
        assert files.run(company.company_id, receipt["task_id"])["state"] == "succeeded"
