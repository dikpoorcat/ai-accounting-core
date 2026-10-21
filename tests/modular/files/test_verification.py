from __future__ import annotations

import io
import os
import shutil
from copy import deepcopy
from pathlib import Path

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.files import _paths
from ai_accounting_modular.files.public import FileArtifact, ProductionError

from ._support import backup_plan, report_plan


@pytest.mark.parametrize(
    "value", ["../escape", "/absolute", "C:/escape", "a\\b", "a//b", "a/./b", "a/../b"]
)
def test_input_paths_cannot_escape_controlled_roots(file_case, value):
    storage, (company, _), _, files = file_case
    plan = backup_plan(storage, company)
    plan["inputs"][0]["relative_path"] = value
    with pytest.raises(ContractError) as unsafe:
        files.ensure_current(plan, plan["scope_head"], "unsafe")
    assert unsafe.value.code == "unsafe_path"


def test_manifest_and_inputs_must_match_exactly(file_case):
    storage, (company, _), _, files = file_case
    plan = backup_plan(storage, company)
    plan["inputs"][0]["sha256"] = "f" * 64
    with pytest.raises(ContractError) as invalid:
        files.ensure_current(plan, plan["scope_head"], "bad metadata")
    assert invalid.value.code == "invalid_input"


def test_candidate_cannot_escape_and_no_publish_occurs(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    producer.produce = lambda plan, context: FileArtifact("../../elsewhere.xlsx")
    result = files.run(company.company_id, task["task_id"])
    assert result["state"] == "pending" and result["error_code"] == "unsafe_path"
    assert not any(event[0] == "publish" for event in producer.events)


def test_publish_result_cannot_point_at_unrelated_host_file(file_case, tmp_path):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    unrelated = tmp_path / "unrelated.xlsx"
    unrelated.write_bytes(b"untouched")
    producer.publish_hook = lambda plan, artifact, context: unrelated
    result = files.run(company.company_id, task["task_id"])
    assert result["error_code"] == "unsafe_path"
    assert unrelated.read_bytes() == b"untouched"


def test_symlinked_task_root_is_rejected(file_case, tmp_path):
    _, (company, _), _, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    target = tmp_path / "outside"
    target.mkdir()
    root = company.company_root / "runtime/file-tasks" / task["task_id"]
    root.parent.mkdir(exist_ok=True)
    try:
        root.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Windows 当前会话未开放符号链接创建。")
    with pytest.raises(ContractError) as unsafe:
        files.run(company.company_id, task["task_id"])
    assert unsafe.value.code == "unsafe_path"
    assert list(target.iterdir()) == []


@pytest.mark.parametrize("damage", ["tampered", "missing"])
def test_missing_or_tampered_download_never_delivers(file_case, damage):
    _, (company, _), _, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    done = files.run(company.company_id, task["task_id"])
    path = Path(done["path"])
    if damage == "tampered":
        path.write_bytes(b"not an xlsx")
    else:
        path.unlink()
    with pytest.raises(ContractError) as wrong:
        with files.verified_download(company.company_id, task["task_id"]):
            pytest.fail("corrupt bytes delivered")
    assert wrong.value.code == "file_invalid"
    failed = files.get(company.company_id, task["task_id"])
    assert failed["state"] == "failed" and failed["attempts"] == 1 and "path" not in failed
    assert files.ensure_current(plan, plan["scope_head"], "creator") == failed
    newer = files.ensure_current(plan, plan["scope_head"], "explicit regenerate")
    assert newer["task_id"] != task["task_id"]
    assert files.run(company.company_id, newer["task_id"])["state"] == "succeeded"


def test_successful_download_reuses_its_saved_strict_semantic_proof(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    files.run(company.company_id, task["task_id"])

    def semantic_mismatch(*args):
        raise ProductionError("wrong_company", "文件内公司不匹配。", retryable=False)

    producer.verify_hook = semantic_mismatch
    before = sum(event[0] == "verify" for event in producer.events)
    with files.verified_download(company.company_id, task["task_id"]) as download:
        assert download.stream.read(1)
    assert sum(event[0] == "verify" for event in producer.events) == before == 1


def test_same_length_and_mtime_changed_download_is_rejected_by_actual_sha(file_case):
    _, (company, _), _, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    done = files.run(company.company_id, task["task_id"])
    path = Path(done["path"])
    info, raw = path.stat(), path.read_bytes()
    path.write_bytes(bytes([raw[0] ^ 1]) + raw[1:])
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert path.stat().st_size == info.st_size
    assert path.stat().st_mtime_ns == info.st_mtime_ns
    with pytest.raises(ContractError) as damaged:
        with files.verified_download(company.company_id, task["task_id"]):
            pytest.fail("same timestamp damage delivered")
    assert damaged.value.code == "file_invalid"
    assert files.get(company.company_id, task["task_id"])["state"] == "failed"


def test_candidate_semantic_mismatch_never_becomes_a_saved_delivery(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")

    def reject(*args):
        raise ProductionError("wrong_company", "文件内公司不匹配。", retryable=False)

    producer.verify_hook = reject
    result = files.run(company.company_id, task["task_id"])
    assert result["state"] == "failed" and result["error_code"] == "wrong_company"
    assert not any(event[0] == "publish" for event in producer.events)


def test_verifier_only_receives_read_only_exact_bytes(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")

    def cannot_write(plan, stream, context):
        with pytest.raises(io.UnsupportedOperation):
            stream.write(b"changed")

    producer.verify_hook = cannot_write
    assert files.run(company.company_id, task["task_id"])["state"] == "succeeded"


def test_download_has_independent_snapshot_after_final_validity_check(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    done = files.run(company.company_id, task["task_id"])
    original = Path(done["path"]).read_bytes()
    with files.verified_download(company.company_id, task["task_id"]) as download:
        Path(done["path"]).write_bytes(b"changed after response snapshot")
        producer.current_heads = set()
        assert download.stream.read() == original
    with pytest.raises(ContractError):
        with files.verified_download(company.company_id, task["task_id"]):
            pytest.fail("later invalid response")


def test_plan_becoming_invalid_during_read_is_not_sent(file_case, monkeypatch):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")
    files.run(company.company_id, task["task_id"])
    snapshot = _paths.snapshot_file

    def change_after_actual_read(path):
        result = snapshot(path)
        producer.current_heads = set()
        return result

    monkeypatch.setattr(_paths, "snapshot_file", change_after_actual_read)
    with pytest.raises(ContractError) as expired:
        with files.verified_download(company.company_id, task["task_id"]):
            pytest.fail("expired after full read")
    assert expired.value.code == "plan_expired"
    assert files.get(company.company_id, task["task_id"])["state"] == "expired"


@pytest.mark.parametrize("field", ["company_id", "database_id"])
def test_nested_snapshot_identity_cannot_mismatch_outer_plan(file_case, field):
    _, (company, other), _, files = file_case
    plan = report_plan(company)
    wrong = deepcopy(plan)
    wrong["snapshot"][field] = getattr(other, field)
    with pytest.raises(ContractError) as mismatch:
        files.ensure_current(wrong, wrong["scope_head"], "wrong inner identity")
    assert mismatch.value.code == "conflict"


def test_changed_published_bytes_cannot_be_marked_successful(file_case):
    _, (company, _), producer, files = file_case
    plan = report_plan(company)
    task = files.ensure_current(plan, plan["scope_head"], "creator")

    def substitute(plan, artifact, context):
        from openpyxl import load_workbook

        path = context.task_root / artifact.relative_path
        output = context.task_root / "work/substituted.xlsx"
        workbook = load_workbook(path)
        workbook.active["C1"] = "same identity, different content"
        workbook.save(output)
        workbook.close()
        return output

    producer.publish_hook = substitute
    failed = files.run(company.company_id, task["task_id"])
    assert failed["state"] != "succeeded"
    assert failed["error_code"] == "file_invalid"


def test_external_backup_current_and_previous_survive_task_cleanup(file_case):
    storage, (company, _), producer, files = file_case
    current = company.backup_directory / f"{company.credit_code}.finance-company.zip"
    previous = company.backup_directory / f"{company.credit_code}.previous.finance-company.zip"

    def roll(plan, artifact, context):
        company.backup_directory.mkdir(exist_ok=True)
        if current.exists():
            current.replace(previous)
        shutil.copyfile(context.task_root / artifact.relative_path, current)
        return current

    producer.publish_hook = roll
    first = backup_plan(storage, company)
    task = files.ensure_current(first, first["scope_head"], "first")
    first_done = files.run(company.company_id, task["task_id"])
    first_bytes = current.read_bytes()
    assert first_done["path"] == str(current)
    second = backup_plan(storage, company, label="second")
    successor = files.ensure_current(second, second["scope_head"], "second")
    assert current.read_bytes() == first_bytes
    assert files.run(company.company_id, successor["task_id"])["state"] == "succeeded"
    assert previous.read_bytes() == first_bytes
    second_bytes = current.read_bytes()
    from ai_accounting_modular.files.public import ExpireTask

    files.update(
        successor["task_id"],
        "succeeded",
        second["scope_head"],
        ExpireTask(),
        company_id=company.company_id,
    )
    files.cleanup(company.company_id)
    assert current.read_bytes() == second_bytes
    assert previous.read_bytes() == first_bytes


def test_partial_financial_snapshot_cannot_create_an_export_task(file_case):
    _, (company, _), _, files = file_case
    plan = report_plan(company)
    plan["snapshot"]["status"] = "partial"
    with pytest.raises(ContractError) as partial:
        files.ensure_current(plan, plan["scope_head"], "partial")
    assert partial.value.code == "invalid_input"
