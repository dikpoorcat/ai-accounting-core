"""Explicit current-status checks never execute work or change default get behavior."""

from concurrent.futures import ThreadPoolExecutor
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
