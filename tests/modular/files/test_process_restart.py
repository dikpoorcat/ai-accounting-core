from __future__ import annotations

import json
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ._support import SyntheticProducer, backup_plan, service

CHILD = Path(__file__).with_name("_child_worker.py")
PYTHON = sys.executable


def command(storage, company, task, mode, *extra):
    return [
        str(PYTHON),
        str(CHILD),
        str(storage.catalog_path),
        company.company_id,
        task["task_id"],
        mode,
        *map(str, extra),
    ]


@pytest.mark.parametrize("mode,exit_code", [("crash_produce", 37), ("crash_publish", 38)])
def test_actual_process_loss_resumes_same_plan_in_next_attempt(file_case, mode, exit_code):
    storage, (company, _), _, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    files.close()
    crashed = subprocess.run(command(storage, company, task, mode), capture_output=True, timeout=20)
    assert crashed.returncode == exit_code, crashed.stderr.decode(errors="replace")
    producer = SyntheticProducer()
    restarted = service(storage, producer)
    try:
        restarted.recover(company.company_id)
        current = restarted.get(company.company_id, task["task_id"])
        assert current["state"] == "running" and current["attempts"] == 1
        result = restarted.run(company.company_id, task["task_id"])
        assert result["state"] == "succeeded" and result["attempts"] == 2
        assert result["scope_head"] == plan["scope_head"]
        with restarted.verified_download(company.company_id, task["task_id"]) as download:
            assert download.byte_size > 0
    finally:
        restarted.close()


def test_os_execution_lock_prevents_other_process_claim_until_crash(file_case, tmp_path):
    storage, (company, _), _, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    files.close()
    marker = tmp_path / "child-entered.txt"
    process = subprocess.Popen(
        command(storage, company, task, "hold", marker),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    restarted = service(storage, SyntheticProducer())
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline and process.poll() is None:
            time.sleep(0.02)
        assert marker.exists()
        assert restarted.run(company.company_id, task["task_id"])["attempts"] == 1
        process.terminate()
        process.wait(timeout=10)
        result = restarted.run(company.company_id, task["task_id"])
        assert result["state"] == "succeeded" and result["attempts"] == 2
    finally:
        if process.poll() is None:
            process.terminate()
            process.wait(timeout=10)
        process.communicate(timeout=10)
        restarted.close()


def test_crash_during_shared_capture_handoff_rolls_back_to_complete_old_plan(file_case, tmp_path):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    producer.failures = 3
    for _ in range(3):
        files.run(company.company_id, task["task_id"])
    files.close()
    plan_file = tmp_path / "synthetic-plan.json"
    plan_file.write_text(json.dumps(plan), encoding="utf-8")
    result = subprocess.run(
        command(storage, company, task, "crash_handoff", plan_file), capture_output=True, timeout=20
    )
    assert result.returncode == 39, result.stderr.decode(errors="replace")
    restarted_producer = SyntheticProducer()
    restarted = service(storage, restarted_producer)
    try:
        restarted.recover(company.company_id)
        assert restarted.get(company.company_id, task["task_id"])["state"] == "failed"
        assert restarted_producer.pins[(company.company_id, plan["capture_id"])] == 1
        successor = restarted.ensure_current(plan, plan["scope_head"], "successor request")
        assert successor["task_id"] != task["task_id"]
        assert restarted.run(company.company_id, successor["task_id"])["state"] == "succeeded"
    finally:
        restarted.close()
