"""Large plan preparation stays outside the short task writer transaction."""

from __future__ import annotations

from contextlib import contextmanager
from threading import local

import pytest

from ai_accounting_modular.contracts.public import loads_json
from ai_accounting_modular.files import _plans, _service

from ._support import backup_plan, report_plan


def test_predicate_mutation_never_leaks_into_another_check_or_saved_plan(file_case):
    storage, (company, _), producer, files = file_case
    plan = report_plan(company)
    versions = []

    def mutating_current(value, context):
        versions.append(value["template_version"])
        value["template_version"] = 999
        return True

    producer.is_current = mutating_current
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    assert files.run(company.company_id, task["task_id"])["state"] == "succeeded"
    assert len(versions) >= 5 and all(version == plan["template_version"] for version in versions)
    with storage.read_unit(company.company_id) as read:
        row = read.repository("files").get(task["task_id"], plan=True)
    assert loads_json(row["plan_json"])["template_version"] == plan["template_version"]


@pytest.mark.parametrize("shared_capture", [False, True])
def test_plan_copy_and_serialization_happen_before_maintenance_writer_lock(
    file_case, monkeypatch, shared_capture
):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company) if shared_capture else report_plan(company)
    state = local()
    original_unit = storage.maintenance_unit

    @contextmanager
    def maintenance(company_id):
        with original_unit(company_id) as unit:
            state.writing = True
            try:
                yield unit
            finally:
                state.writing = False

    storage.maintenance_unit = maintenance

    def outside_writer(original):
        def serialize(value):
            assert not getattr(state, "writing", False), "large plan serialized inside writer lock"
            return original(value)

        return serialize

    monkeypatch.setattr(_plans, "canonical_json", outside_writer(_plans.canonical_json))
    monkeypatch.setattr(_service, "canonical_json", outside_writer(_service.canonical_json))
    if shared_capture:
        producer.failures = 3
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    if shared_capture:
        for _ in range(3):
            files.run(company.company_id, task["task_id"])
        task = files.ensure_current(plan, plan["scope_head"], "new explicit request")
    assert files.run(company.company_id, task["task_id"])["state"] == "succeeded"
