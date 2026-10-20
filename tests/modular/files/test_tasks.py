from __future__ import annotations

import hashlib
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from threading import Event

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.files.public import (
    ExpireTask,
    FileCompletion,
    FileFailure,
    RunClaim,
)

from ._support import backup_plan, report_plan, service


@pytest.mark.parametrize("kind", ["backup", "report_export"])
def test_real_bytes_success_idempotency_and_no_business_head_change(file_case, kind):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company) if kind == "backup" else report_plan(company)
    with storage.read_unit(company.company_id) as read:
        before = read.head
    first = files.ensure_current(plan, plan["scope_head"], "creator opaque")
    duplicate = files.ensure_current(plan, plan["scope_head"], "another client")
    assert first == duplicate
    assert first["state"] == "pending" and first["attempts"] == 0
    with pytest.raises(ContractError) as not_ready:
        with files.verified_download(company.company_id, first["task_id"]):
            pass
    assert not_ready.value.code == "file_not_ready"
    done = files.run(company.company_id, first["task_id"])
    assert done["state"] == "succeeded" and done["attempts"] == 1
    if kind == "backup":
        assert not (company.company_root / "runtime/tasks" / plan["capture_id"]).exists()
    assert files.ensure_current(plan, plan["scope_head"], "third client") == done
    with files.verified_download(company.company_id, first["task_id"]) as download:
        data = download.stream.read()
        assert hashlib.sha256(data).hexdigest() == done["sha256"] == download.sha256
        assert len(data) == done["byte_size"] == download.byte_size
        assert download.file_name == producer.describe(plan).file_name
    validate_contract("core.schema.json", "TaskReceipt", done)
    with storage.read_unit(company.company_id) as read:
        assert read.head == before


def test_same_scope_concurrent_clients_share_one_current_task(file_case):
    _, (company, _), _, files = file_case
    plan = report_plan(company)
    with ThreadPoolExecutor(max_workers=6) as pool:
        results = list(
            pool.map(lambda n: files.ensure_current(plan, plan["scope_head"], f"r{n}"), range(12))
        )
    assert len({result["task_id"] for result in results}) == 1


def test_creator_conflict_is_current_only_and_does_not_replace(file_case):
    _, (company, _), _, files = file_case
    first = report_plan(company)
    receipt = files.ensure_current(first, first["scope_head"], "same creator")
    second = report_plan(company, label="second")
    with pytest.raises(ContractError) as conflict:
        files.ensure_current(second, second["scope_head"], "same creator")
    assert conflict.value.code == "conflict"
    assert files.get(company.company_id, receipt["task_id"]) == receipt


def test_three_failures_creator_retry_and_new_request_share_capture(file_case):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    producer.failures = 3
    first = files.ensure_current(plan, plan["scope_head"], "creator")
    for attempt in (1, 2, 3):
        current = files.run(company.company_id, first["task_id"])
        assert current["attempts"] == attempt
        assert current["state"] == ("failed" if attempt == 3 else "pending")
    assert files.ensure_current(plan, plan["scope_head"], "creator") == current
    # A reused client key is not an ever-growing alias of the creator key.
    successor = files.ensure_current(plan, plan["scope_head"], "explicit new request")
    assert successor["task_id"] != first["task_id"]
    assert (company.company_root / "runtime/tasks" / plan["capture_id"]).exists()
    assert ("cleanup", first["task_id"], True) in producer.events
    assert files.run(company.company_id, successor["task_id"])["state"] == "succeeded"
    with pytest.raises(ContractError) as old:
        files.get(company.company_id, first["task_id"])
    assert old.value.code == "not_found"


def test_expired_cleanup_failure_retains_only_locator_then_resumes(file_case):
    storage, (company, _), producer, files = file_case
    first = backup_plan(storage, company)
    old = files.ensure_current(first, first["scope_head"], "first")
    producer.cleanup_failures = 1
    second = backup_plan(storage, company, label="second")
    with pytest.raises(ContractError) as cleanup:
        files.ensure_current(second, second["scope_head"], "second")
    assert cleanup.value.code == "file_cleanup_pending"
    with storage.read_unit(company.company_id) as read:
        row = read.repository("files").get(old["task_id"], plan=True)
    assert row["state"] == "expired"
    assert row["plan_json"] is None and row["plan_schema"] is None
    assert row["capture_id"] == first["capture_id"]
    assert row["output_path"] is None
    replacement = files.ensure_current(second, second["scope_head"], "second")
    assert replacement["state"] == "pending"
    assert not (company.company_root / "runtime/tasks" / first["capture_id"]).exists()


def test_late_worker_cannot_publish_after_expiration(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    entered, release = Event(), Event()

    def pause(plan, context):
        entered.set()
        assert release.wait(10)

    producer.produce_hook = pause
    with ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(files.run, company.company_id, task["task_id"])
        assert entered.wait(10)
        expired = files.update(
            task["task_id"],
            "running",
            plan["scope_head"],
            ExpireTask(),
            company_id=company.company_id,
        )
        assert expired["state"] == "expired"
        release.set()
        assert future.result(timeout=10)["state"] == "expired"
    assert not any(event[0] == "publish" for event in producer.events)
    files.cleanup(company.company_id)
    with pytest.raises(ContractError):
        files.get(company.company_id, task["task_id"])


def test_only_one_worker_produces_a_scope(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    entered, release = Event(), Event()
    producer.produce_hook = lambda plan, context: (entered.set(), release.wait(10))
    with ThreadPoolExecutor(max_workers=2) as pool:
        future = pool.submit(files.run, company.company_id, task["task_id"])
        assert entered.wait(10)
        assert files.run(company.company_id, task["task_id"])["state"] == "running"
        release.set()
        assert future.result(timeout=10)["state"] == "succeeded"
    assert sum(event[0] == "produce" for event in producer.events) == 1


def test_generation_and_verification_do_not_hold_business_write_transaction(file_case):
    storage, (company, _), producer, files = file_case

    def open_independent_write(*args):
        with storage.write_unit(company.company_id):
            pass

    producer.produce_hook = open_independent_write
    producer.verify_hook = open_independent_write
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    assert files.run(company.company_id, task["task_id"])["state"] == "succeeded"


def test_forged_late_completion_cannot_change_new_attempt(file_case):
    storage, (company, _), producer, files = file_case
    producer.failures = 1
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    assert files.run(company.company_id, task["task_id"])["state"] == "pending"
    with storage.maintenance_unit(company.company_id) as uow:
        repo = uow.repository("files")
        row = repo.get(task["task_id"], plan=True)
        repo.replace(row, state="running", attempts=2)
    old = RunClaim(company.company_id, task["task_id"], plan["scope_head"], 1)
    with pytest.raises(ContractError) as changed:
        files.update(
            task["task_id"],
            "running",
            plan["scope_head"],
            FileCompletion(old, failure=FileFailure("old", "旧执行")),
            company_id=company.company_id,
        )
    assert changed.value.code == "task_changed"
    assert files.get(company.company_id, task["task_id"])["attempts"] == 2


def test_no_cross_company_task_lookup_or_plan_mismatch(file_case):
    _, (company, other), _, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    with pytest.raises(ContractError) as missing:
        files.get(other.company_id, task["task_id"])
    assert missing.value.code == "not_found"
    wrong = deepcopy(plan)
    wrong["database_id"] = other.database_id
    with pytest.raises(ContractError) as mismatch:
        files.ensure_current(wrong, wrong["scope_head"], "wrong database")
    assert mismatch.value.code == "conflict"


def test_services_share_live_leases_until_last_service_closes(file_case):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    second = service(storage, producer)
    try:
        second.recover(company.company_id)
        key = (company.company_id, plan["capture_id"])
        assert producer.pins[key] == 1
        second.close()
        assert producer.pins[key] == 1
        assert files.get(company.company_id, task["task_id"])["state"] == "pending"
    finally:
        second.close()


def test_idle_service_can_close_while_another_service_is_producing(file_case):
    storage, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    second = service(storage, producer)
    second.get(company.company_id, task["task_id"])
    entered, release = Event(), Event()
    producer.produce_hook = lambda plan, context: (entered.set(), release.wait(10))
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(files.run, company.company_id, task["task_id"])
            assert entered.wait(10)
            second.close()
            with pytest.raises(ContractError) as busy:
                files.close()
            assert busy.value.code == "busy"
            release.set()
            assert future.result(timeout=10)["state"] == "succeeded"
    finally:
        release.set()
        second.close()


def test_same_semantic_backup_reuses_original_capture_despite_new_capture_bytes(file_case):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    newer_capture = backup_plan(storage, company)
    assert newer_capture["capture_id"] != plan["capture_id"]
    # A database snapshot includes transient file_task state. Those bytes must
    # not manufacture another semantic backup plan or replace its capture.
    assert newer_capture["inputs"][0]["sha256"] != plan["inputs"][0]["sha256"]
    assert files.ensure_current(newer_capture, newer_capture["scope_head"], "query reuse") == task
    assert producer.pins[(company.company_id, newer_capture["capture_id"])] == 0
    assert files.run(company.company_id, task["task_id"])["state"] == "succeeded"


@pytest.mark.parametrize("ready", [False, True])
def test_expired_plan_is_not_reused_or_produced(file_case, ready):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    if ready:
        files.run(company.company_id, task["task_id"])
    producer.current_heads = set()
    if ready:
        with pytest.raises(ContractError) as expired:
            files.ensure_current(plan, plan["scope_head"], "new query")
        assert expired.value.code == "plan_expired"
    else:
        assert files.run(company.company_id, task["task_id"])["state"] == "expired"
        assert not any(event[0] == "produce" for event in producer.events)
    assert files.get(company.company_id, task["task_id"])["state"] == "expired"
