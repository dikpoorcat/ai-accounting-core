"""Real export tasks isolate files while preserving frozen pre-change jobs."""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_exports as payment_cases
import test_reports as report_cases
import test_tax_import as tax_cases

from ai_accounting.kernel import exports, reports, tax_import
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.export_directories import (
    _validate_windows_components,
    publish_export_directory,
)
from ai_accounting.kernel.exports import run_export_jobs
from ai_accounting.kernel.read_indexes import sync_job
from ai_accounting.kernel.reports import run_report_jobs
from ai_accounting.kernel.tax_import import run_tax_import_jobs
from ai_accounting.kernel.types import canonical, digest

payment_setup = payment_cases.setup
report_book = report_cases.book


@pytest.fixture(params=["payment", "report", "tax"])
def export_case(request, tmp_path):
    kind = request.param
    if kind == "payment":
        company, service, template = request.getfixturevalue("payment_setup")
        engine = company.engine
        plan = service.preview("2026-01", template_evidence_digest=template)
        arguments = ("2026-01",)
        options = {
            "template_evidence_digest": template,
            "preview_digest": plan["digest"],
            "epochs": plan["epochs"],
        }
        confirm, runner, action = service.confirm, run_export_jobs, "payment_export"

        def old_hash(parent):
            return digest([
                action, "2026-01", template, plan["digest"], plan["epochs"], str(parent), None,
            ])

    elif kind == "report":
        book = request.getfixturevalue("report_book")
        engine = book[0]
        service = report_cases.scenario(book)
        report_cases.close_quarter(book)
        plan = service.preview_export(2026, 1)
        arguments = (2026, 1)
        options = {"preview_digest": plan["digest"], "epochs": plan["epochs"]}
        confirm, runner, action = service.confirm_export, run_report_jobs, "report_export"

        def old_hash(parent):
            return digest([
                action, 2026, 1, plan["digest"], plan["epochs"], str(parent), None,
            ])

    else:
        company = tax_cases.company(tmp_path, publish=False)
        engine = company.engine
        service = tax_cases.complete_details(company)
        plan = service.preview("2026-01")
        arguments = ("2026-01",)
        options = {"preview_digest": plan["digest"]}
        confirm, runner, action = service.confirm, run_tax_import_jobs, "tax_import"

        def old_hash(parent):
            return digest([action, "2026-01", plan["digest"], str(parent)])

    def submit(request_id, **extra):
        return confirm(*arguments, **options, request_id=request_id, **extra)

    return SimpleNamespace(
        kind=kind, engine=engine, plan=plan, options=options, confirm=submit, run=runner,
        action=action, old_hash=old_hash,
    )


def frozen_payload(case, job_id):
    with case.engine.store.connection(read_only=True) as connection:
        return json.loads(connection.execute(
            "SELECT payload FROM jobs WHERE id=?", (job_id,),
        ).fetchone()[0])


def test_existing_parent_has_isolated_tasks_and_replays_keep_the_same_task(export_case, tmp_path):
    case = export_case
    parent = tmp_path / "owner-exports"
    parent.mkdir()
    sentinel = parent / "owner-file.txt"
    sentinel.write_text("preserve this original", encoding="utf-8")
    first = case.confirm("first", output_directory=str(parent))
    second = case.confirm("second", output_directory=str(parent))
    assert first["job_id"] != second["job_id"]
    for task in (first, second):
        payload = frozen_payload(case, task["job_id"])
        assert Path(payload["output_directory"]) == parent / task["job_id"]
        assert not Path(payload["output_directory"]).exists()
    assert case.confirm("first", output_directory=str(parent)) == first
    results = case.run(case.engine)
    assert len(results) == 2 and all(item["status"] == "succeeded" for item in results)
    assert {Path(item["result"]["path"]).parent for item in results} == {
        parent / first["job_id"], parent / second["job_id"],
    }
    assert sentinel.read_text(encoding="utf-8") == "preserve this original"
    assert {path.name for path in parent.iterdir()} == {
        "owner-file.txt", first["job_id"], second["job_id"],
    }
    assert case.confirm("first", output_directory=str(parent)) == first
    assert len(case.engine.jobs()) == 2


def test_omitted_parent_is_company_exports_and_confirmation_creates_no_directories(export_case):
    case = export_case
    parent = case.engine.store.path.parent / "exports"
    assert not parent.exists()
    task = case.confirm("default-parent")
    assert frozen_payload(case, task["job_id"])["output_directory"] == str(parent / task["job_id"])
    assert not parent.exists()
    assert case.confirm("default-parent") == task
    result = case.run(case.engine)[0]
    assert result["status"] == "succeeded"
    assert Path(result["result"]["path"]).parent == parent / task["job_id"]
    assert Path(result["result"]["path"]).is_file()
    assert case.confirm("default-parent") == task


def test_invalid_parent_does_not_queue_jobs_or_create_paths(export_case, tmp_path):
    case = export_case
    blocker = tmp_path / "owner-file"
    blocker.write_text("must not be replaced", encoding="utf-8")
    for index, parent in enumerate(("", " \t", str(blocker), str(blocker / "nested"))):
        with pytest.raises(KernelError) as failure:
            case.confirm(f"invalid-{index}", output_directory=parent)
        assert failure.value.code == "export_parent_invalid"
    default = case.engine.store.path.parent / "exports"
    default.write_text("default parent blocker", encoding="utf-8")
    with pytest.raises(KernelError) as failure:
        case.confirm("invalid-default")
    assert failure.value.code == "export_parent_invalid"
    assert case.engine.jobs() == []
    assert blocker.read_text(encoding="utf-8") == "must not be replaced"
    assert default.read_text(encoding="utf-8") == "default parent blocker"


@pytest.mark.skipif(os.name != "nt", reason="Windows filename constraints")
def test_invalid_windows_parent_components_never_queue_tasks(export_case, tmp_path):
    case = export_case
    invalid_components = (
        "question?mark", "wild*card", "less<than", "greater>than", 'quote"name',
        "stream:name", "pipe|name", "control\x01name", "CON", "nul.txt", "COM1",
        "lpt9.log", "trailing.", "trailing ",
    )
    for index, component in enumerate(invalid_components):
        # Place the bad component above the leaf to check the entire parent chain.
        invalid = str(tmp_path / component / "new-export-parent")
        with pytest.raises(KernelError) as failure:
            case.confirm(f"invalid-windows-{index}", output_directory=invalid)
        assert failure.value.code == "export_parent_invalid"
        assert case.engine.jobs() == []


@pytest.mark.parametrize("parent", [
    r"C:\exports\company", r"C:exports\company", r"\\server\share\company",
    r"\\?\C:\exports\company", r"\\?\UNC\server\share\company",
    r"\\?\unc\server\share\company", r"\\.\UNC\server\share\company",
    r"\\CON\NUL\company", r"\\server.example.\share.\company",
    r"\\server\share \company",
    r"C:\exports\..\company", r"exports\公司\2026-10",
])
def test_windows_component_validation_preserves_drive_unc_and_relative_anchors(parent):
    # Pure component checks do not access a local drive or contact a UNC server.
    _validate_windows_components(parent)


@pytest.mark.parametrize("parent", [
    r"\\server\bad?share\exports", r"\\bad*server\share\exports",
    r"\\server\bad+share\exports", r"\\?\UNC\server\bad?share\exports",
    r"\\?\unc\bad*server\share\exports",
    r"\\.\UNC\server\bad?share\exports", "\\\\server\\" + "s" * 81 + "\\exports",
])
def test_windows_unc_names_are_validated_without_accessing_the_network(parent):
    with pytest.raises(KernelError) as failure:
        _validate_windows_components(parent)
    assert failure.value.code == "export_parent_invalid"


def test_directory_publication_preserves_an_existing_empty_directory(tmp_path):
    staging, target = tmp_path / "staging", tmp_path / "owner-directory"
    staging.mkdir()
    (staging / "export.xlsx").write_bytes(b"verified export")
    target.mkdir()
    identity = target.stat().st_ino
    with pytest.raises(FileExistsError):
        publish_export_directory(staging, target)
    assert target.stat().st_ino == identity and list(target.iterdir()) == []
    assert (staging / "export.xlsx").read_bytes() == b"verified export"


def test_directory_publication_moves_the_complete_stage_to_a_new_target(tmp_path):
    staging, target = tmp_path / "staging", tmp_path / "new-task"
    staging.mkdir()
    (staging / "export.xlsx").write_bytes(b"verified export")
    publish_export_directory(staging, target)
    assert not staging.exists()
    assert (target / "export.xlsx").read_bytes() == b"verified export"


@pytest.mark.parametrize("occupant", [
    "empty", "different", "same", "damaged_manifest", "broken_symlink",
])
def test_last_instant_target_race_reuses_only_exact_task_files(
    export_case, tmp_path, monkeypatch, occupant,
):
    case = export_case
    task = case.confirm("last-instant-race", output_directory=str(tmp_path / "parent"))
    module = {"payment": exports, "report": reports, "tax": tax_import}[case.kind]
    original_publish = module.publish_export_directory
    occupied = {}

    def race(staging, target):
        if occupant in {"same", "damaged_manifest"}:
            shutil.copytree(staging, target)
            if occupant == "damaged_manifest":
                manifest = next(target.glob("*.json"))
                manifest.write_text("[]", encoding="utf-8")
        elif occupant == "broken_symlink":
            try:
                target.symlink_to(tmp_path / "owner-missing-target", target_is_directory=True)
            except OSError as exc:
                pytest.skip(f"Directory symlinks are unavailable: {exc}")
        else:
            target.mkdir()
            if occupant == "different":
                (target / "owner-file.txt").write_bytes(b"original owner content")
        occupied.update(
            target=target, identity=target.lstat().st_ino,
            link=target.readlink() if occupant == "broken_symlink" else None,
            files=None if occupant == "broken_symlink"
            else {path.name: path.read_bytes() for path in target.iterdir()},
        )
        return original_publish(staging, target)

    monkeypatch.setattr(module, "publish_export_directory", race)
    result = case.run(case.engine)[0]
    assert result["job_id"] == task["job_id"]
    if occupant == "same":
        assert result["status"] == "succeeded" and result["error_code"] is None
    else:
        assert result["status"] == "failed"
        assert result["error_code"] == (
            "report_target_conflict" if case.kind == "report" else "export_target_conflict"
        )
    target = occupied["target"]
    assert target.lstat().st_ino == occupied["identity"]
    if occupant == "broken_symlink":
        assert target.is_symlink() and not target.exists()
        assert target.readlink() == occupied["link"]
    else:
        assert {path.name: path.read_bytes() for path in target.iterdir()} == occupied["files"]
    assert len(case.engine.jobs()) == 1


def test_publication_failure_without_a_target_keeps_the_original_storage_error(
    export_case, tmp_path, monkeypatch,
):
    case = export_case
    task = case.confirm("publication-denied", output_directory=str(tmp_path / "parent"))
    module = {"payment": exports, "report": reports, "tax": tax_import}[case.kind]

    def deny(staging, target):
        raise PermissionError("simulated write denial")

    monkeypatch.setattr(module, "publish_export_directory", deny)
    result = case.run(case.engine)[0]
    assert result["status"] == "failed" and result["error_code"] == "storage_not_writable"
    assert not Path(frozen_payload(case, task["job_id"])["output_directory"]).exists()


def test_worker_preserves_a_task_directory_occupied_after_confirmation(export_case, tmp_path):
    case = export_case
    task = case.confirm("raced", output_directory=str(tmp_path / "parent"))
    target = Path(frozen_payload(case, task["job_id"])["output_directory"])
    target.mkdir(parents=True)
    sentinel = target / "owner-file.txt"
    sentinel.write_text("racing original", encoding="utf-8")
    result = case.run(case.engine)[0]
    assert result["status"] == "failed"
    assert result["error_code"] == (
        "report_target_conflict" if case.kind == "report" else "export_target_conflict"
    )
    assert case.engine.jobs(job_id=task["job_id"])[0]["error_code"] == result["error_code"]
    assert list(target.iterdir()) == [sentinel]
    assert sentinel.read_text(encoding="utf-8") == "racing original"


def seed_pre_change_task(case, target, request_id="old-confirm"):
    """Use the historical request hash and exact final-directory payload."""
    job_id = "pre-directory-change"
    payload = {"plan": case.plan, "output_directory": str(target)}
    receipt = {"status": "queued", "job_id": job_id, "preview_digest": case.plan["digest"]}
    if case.kind == "payment":
        payload["template_evidence_digest"] = case.options["template_evidence_digest"]
        receipt.update(row_count=len(case.plan["rows"]), total_fen=case.plan["total_fen"])
    elif case.kind == "tax":
        receipt["row_count"] = case.plan["row_count"]

    def operation(connection):
        connection.execute(
            "INSERT INTO jobs(id,kind,payload,status) VALUES(?,?,?,'pending')",
            (job_id, case.action, canonical(payload)),
        )
        sync_job(connection, job_id)
        return receipt

    return case.engine._write(
        request_id, case.old_hash(target), case.plan["epochs"], (), case.action, operation,
        checked_lanes=("accounting", "management") if case.kind == "tax"
        else ("accounting", "material", "management"),
    )


def test_pre_change_pending_job_recovers_at_exact_path_and_success_cache_is_reused(
    export_case, tmp_path,
):
    case = export_case
    target = tmp_path / "old-final-directory"
    task = seed_pre_change_task(case, target)
    payload = frozen_payload(case, task["job_id"])
    assert case.confirm("old-confirm", output_directory=str(target)) == task

    def crash(stage, job_id):
        if stage == "files_published":
            assert job_id == task["job_id"]
            raise RuntimeError("interrupted after old files were published")

    assert case.run(case.engine, fault=crash)[0]["status"] == "failed"
    original = {path.name: path.read_bytes() for path in target.iterdir()}
    result = case.run(case.engine)[0]
    assert result["status"] == "succeeded" and result["job_id"] == task["job_id"]
    assert Path(result["result"]["path"]).parent == target
    assert {path.name: path.read_bytes() for path in target.iterdir()} == original
    assert frozen_payload(case, task["job_id"]) == payload
    assert case.confirm("old-confirm", output_directory=str(target)) == task
    assert len(case.engine.jobs()) == 1


@pytest.mark.skipif(os.name != "nt", reason="Windows filename constraints")
def test_old_request_cache_precedes_new_windows_parent_validation(export_case, tmp_path):
    case = export_case
    target = tmp_path / "old-invalid?target"
    task = seed_pre_change_task(case, target)
    original = frozen_payload(case, task["job_id"])
    assert case.confirm("old-confirm", output_directory=str(target)) == task
    assert frozen_payload(case, task["job_id"]) == original
    assert len(case.engine.jobs()) == 1
