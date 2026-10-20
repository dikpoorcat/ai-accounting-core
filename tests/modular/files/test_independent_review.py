"""Independent acceptance checks using real synthetic-company transactions."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from ai_accounting_modular.contracts.public import ContractError

from ._support import backup_plan, report_plan, save_business_object


def test_creation_rechecks_semantic_head_inside_task_insert_transaction(file_case, monkeypatch):
    storage, (company, _), producer, files = file_case
    plan = report_plan(company)
    with storage.read_unit(company.company_id) as read:
        expected = read.head

    def current(plan, context):
        with storage.read_unit(company.company_id) as read:
            return read.head == expected

    producer.is_current = current
    original = storage.maintenance_unit
    changed = False

    @contextmanager
    def business_commit_before_insert(company_id):
        nonlocal changed
        if not changed:
            changed = True
            save_business_object(storage, company)
        with original(company_id) as unit:
            yield unit

    monkeypatch.setattr(storage, "maintenance_unit", business_commit_before_insert)
    with pytest.raises(ContractError) as expired:
        files.ensure_current(plan, plan["scope_head"], "creator")
    assert expired.value.code == "plan_expired"
    with storage.read_unit(company.company_id) as read:
        assert read.head == expected + 1
        assert read.repository("files").current() == ()


def test_failed_capture_handoff_rechecks_head_before_replacing_old_task(file_case, monkeypatch):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    old = files.ensure_current(plan, plan["scope_head"], "original creator")
    producer.failures = 3
    for _ in range(3):
        failed = files.run(company.company_id, old["task_id"])
    assert failed["state"] == "failed"
    with storage.read_unit(company.company_id) as read:
        expected = read.head
        original_task = read.repository("files").get(old["task_id"], plan=True)

    def current(plan, context):
        with storage.read_unit(company.company_id) as read:
            return read.head == expected

    producer.is_current = current
    original = storage.maintenance_unit
    changed = False

    @contextmanager
    def business_commit_before_handoff(company_id):
        nonlocal changed
        if not changed:
            changed = True
            save_business_object(storage, company)
        with original(company_id) as unit:
            yield unit

    monkeypatch.setattr(storage, "maintenance_unit", business_commit_before_handoff)
    with pytest.raises(ContractError) as expired:
        files.ensure_current(plan, plan["scope_head"], "new creator")
    assert expired.value.code == "plan_expired"
    with storage.read_unit(company.company_id) as read:
        assert read.head == expected + 1
        assert read.repository("files").current() == (original_task,)
    assert producer.pins[(company.company_id, plan["capture_id"])] == 1
