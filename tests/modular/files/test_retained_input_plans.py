"""Caller-transaction inspection never restores leases or performs file work."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, canonical_json
from ai_accounting_modular.files import _service
from ai_accounting_modular.files.public import ExpireTask

from ._support import backup_plan, report_plan


@pytest.mark.parametrize("kind", ["backup", "report_export"])
@pytest.mark.parametrize("state", ["pending", "running", "failed", "succeeded", "expired"])
def test_retention_matches_actual_current_task_lifecycle(file_case, kind, state):
    storage, (company, other), producer, files = file_case
    plan = backup_plan(storage, company) if kind == "backup" else report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "accepted")
    if state == "succeeded":
        assert files.run(company.company_id, task["task_id"])["state"] == "succeeded"
    elif state == "failed":
        producer.failures = 3
        for _ in range(3):
            outcome = files.run(company.company_id, task["task_id"])
        assert outcome["state"] == "failed"
    elif state == "expired":
        files.update(
            task["task_id"],
            "pending",
            plan["scope_head"],
            ExpireTask(),
            company_id=company.company_id,
        )
    with storage.maintenance_unit(company.company_id) as unit:
        repository = unit.repository("files")
        row = repository.get(task["task_id"], plan=True)
        if state == "running":
            assert repository.replace(row, state=state, attempts=1) == 1
        expected = () if state in {"succeeded", "expired"} else (plan,)
        assert files.retained_input_plans(kind, unit) == expected
    with storage.read_unit(company.company_id) as read:
        assert files.retained_input_plans(kind, read) == expected
    with storage.read_unit(other.company_id) as read:
        assert files.retained_input_plans(kind, read) == ()


def test_same_read_only_snapshot_no_extra_connection_lock_or_file_access(file_case, monkeypatch):
    storage, (company, _), producer, files = file_case
    plan = backup_plan(storage, company)
    files.ensure_current(plan, plan["scope_head"], "accepted")
    events = tuple(producer.events)

    def forbidden(*args, **kwargs):
        raise AssertionError("retained-input query must use the caller's transaction only")

    with storage.read_unit(company.company_id) as read:
        monkeypatch.setattr(storage, "read_unit", forbidden)
        monkeypatch.setattr(storage, "maintenance_unit", forbidden)
        monkeypatch.setattr(files, "_metadata", forbidden)
        monkeypatch.setattr(files, "recover", forbidden)
        monkeypatch.setattr(_service, "locked", forbidden)
        monkeypatch.setattr(_service._paths, "controlled", forbidden)
        assert files.retained_input_plans("backup", read) == (plan,)
        assert tuple(producer.events) == events


def test_kind_is_narrow_and_each_return_is_an_isolated_complete_plan(file_case):
    storage, (company, _), _, files = file_case
    backup = backup_plan(storage, company)
    report = report_plan(company)
    files.ensure_current(backup, backup["scope_head"], "backup")
    files.ensure_current(report, report["scope_head"], "report")
    with storage.read_unit(company.company_id) as read:
        captured = files.retained_input_plans("backup", read)
        assert captured == (backup,)
        captured[0]["inputs"].clear()
        captured[0]["manifest"]["files"].clear()
        assert files.retained_input_plans("backup", read) == (backup,)
        assert files.retained_input_plans("report_export", read) == (report,)


def test_actual_company_original_input_is_preserved_without_path_interpretation(file_case):
    storage, (company, _), _, files = file_case
    plan = backup_plan(storage, company)
    original = company.company_root / "originals/2026-01/source.txt"
    original.parent.mkdir(parents=True)
    data = "合成必要原件".encode()
    original.write_bytes(data)
    digest = sha256(data).hexdigest()
    plan["inputs"].append(
        {
            "source_root": "company",
            "relative_path": "originals/2026-01/source.txt",
            "archive_path": "originals/source.txt",
            "sha256": digest,
            "byte_size": len(data),
        }
    )
    plan["manifest"]["files"].append(
        {
            "path": "originals/source.txt",
            "kind": "original",
            "sha256": digest,
            "byte_size": len(data),
        }
    )
    files.ensure_current(plan, plan["scope_head"], "accepted-original")
    with storage.read_unit(company.company_id) as read:
        assert files.retained_input_plans("backup", read) == (plan,)
    assert original.read_bytes() == data


def test_all_retained_report_scopes_are_returned_in_the_same_read(file_case):
    storage, (company, _), _, files = file_case
    plans = tuple(
        report_plan(company, label=month, period=month) for month in ("2026-01", "2026-02")
    )
    for index, plan in enumerate(plans):
        files.ensure_current(plan, plan["scope_head"], f"accepted-report-{index}")
    with storage.read_unit(company.company_id) as read:
        assert files.retained_input_plans("report_export", read) == plans


@pytest.mark.parametrize(
    "damage",
    [
        "json",
        "schema",
        "company",
        "database",
        "nested_identity",
        "kind",
        "head",
        "capture",
        "scope",
        "missing_input",
    ],
)
def test_retained_invalid_plan_or_identity_is_corruption_never_empty(file_case, damage):
    storage, (company, _), _, files = file_case
    plan = backup_plan(storage, company)
    task = files.ensure_current(plan, plan["scope_head"], "accepted")
    saved = deepcopy(plan)
    column = "plan_json"
    if damage == "json":
        value = "[]"
    elif damage == "schema":
        column, value = "plan_schema", "ReportExportPlan"
    elif damage in {"company", "database"}:
        saved[damage + "_id"] = str(uuid4())
        value = canonical_json(saved)
    elif damage == "nested_identity":
        saved["manifest"]["company_id"] = str(uuid4())
        value = canonical_json(saved)
    elif damage == "kind":
        saved["kind"] = "report_export"
        value = canonical_json(saved)
    elif damage == "head":
        column, value = "scope_head", "a" * 64
    elif damage == "capture":
        column, value = "capture_id", str(uuid4())
    elif damage == "scope":
        column, value = "scope_key", "not-company"
    else:
        saved["inputs"].clear()
        value = canonical_json(saved)
    with storage.maintenance_unit(company.company_id) as unit:
        unit.repository("files")._session.execute(
            f"UPDATE file_task SET {column}=? WHERE task_id=?", (value, task["task_id"])
        )
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as error:
            files.retained_input_plans("backup", read)
        assert error.value.code == "storage_corrupt"


@pytest.mark.parametrize("kind", [None, [], "unknown", True])
def test_invalid_kind_does_not_return_empty(file_case, kind):
    storage, (company, _), _, files = file_case
    with storage.read_unit(company.company_id) as read:
        with pytest.raises(ContractError) as error:
            files.retained_input_plans(kind, read)
        assert error.value.code == "invalid_input"


def test_closed_caller_transaction_is_not_silently_treated_as_no_plans(file_case):
    storage, (company, _), _, files = file_case
    with storage.read_unit(company.company_id) as read:
        assert files.retained_input_plans("backup", read) == ()
    with pytest.raises(ContractError):
        files.retained_input_plans("backup", read)
