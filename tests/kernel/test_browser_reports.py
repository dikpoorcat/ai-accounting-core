"""Browser exports use frozen jobs and never expose arbitrary local file reads."""

import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_reports as report_cases
from openpyxl import load_workbook
from test_reports import close_quarter, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.reports import run_report_jobs
from ai_accounting.kernel.types import digest

book = report_cases.book


def queue(report, request_id="browser-download"):
    plan = report.preview_export(2026, 1)
    return report.confirm_browser_export(
        2026,
        1,
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        request_id=request_id,
    )


def test_browser_export_job_is_idempotent_scoped_and_verified(book):
    report = scenario(book)
    close_quarter(book)
    task = queue(report)
    assert queue(report) == task
    assert len(book[0].jobs(job_id=task["job_id"])) == 1
    assert report.browser_export_status(task["job_id"])["status"] == "pending"
    assert book[0].jobs(job_id="another-company-job") == []
    with pytest.raises(KernelError, match="尚未生成成功"):
        report.download_browser_report(task["job_id"])
    result = run_report_jobs(book[0])[0]
    assert result["status"] == "succeeded"
    target = Path(result["result"]["directory"])
    assert target == (
        book[0].store.path.parent / "exports" / "browser-reports"
        / hashlib.sha256(b"browser-download").hexdigest() / task["job_id"]
    )
    name, content = report.download_browser_report(task["job_id"])
    assert report.browser_export_status(task["job_id"])["status"] == "succeeded"
    assert name.endswith("2026Q1.xlsx")
    workbook = load_workbook(io.BytesIO(content), data_only=True)
    assert workbook.worksheets[0]["D7"].value == 400
    workbook.close()
    assert queue(report) == task
    assert book[0].jobs(job_id=task["job_id"])[0]["status"] == "succeeded"
    Path(result["result"]["path"]).write_bytes(b"changed file")
    assert report.browser_export_status(task["job_id"])["status"] == "succeeded"
    with pytest.raises(KernelError) as error:
        report.download_browser_report(task["job_id"])
    assert error.value.code == "report_download_invalid"


def test_pre_change_browser_job_keeps_its_original_path_and_request_cache(book):
    from test_export_directories import seed_pre_change_task

    report = scenario(book)
    close_quarter(book)
    plan = report.preview_export(2026, 1)
    request_id = "previous-browser-export"
    target = (
        book[0].store.path.parent / "exports" / "browser-reports"
        / hashlib.sha256(request_id.encode()).hexdigest()
    )
    case = SimpleNamespace(
        kind="report", engine=book[0], plan=plan, action="report_export",
        old_hash=lambda parent: digest([
            "report_export", 2026, 1, plan["digest"], plan["epochs"], str(parent), None,
        ]),
    )
    task = seed_pre_change_task(case, target, request_id)
    assert queue(report, request_id) == task
    result = run_report_jobs(book[0])[0]
    assert result["status"] == "succeeded"
    assert Path(result["result"]["directory"]) == target
    name, content = report.download_browser_report(task["job_id"])
    assert name.endswith("2026Q1.xlsx") and content == Path(result["result"]["path"]).read_bytes()
    assert queue(report, request_id) == task
    assert len(book[0].jobs()) == 1


def test_browser_download_rejects_unknown_job_and_nonbrowser_target(book, tmp_path):
    report = scenario(book)
    close_quarter(book)
    with pytest.raises(KernelError) as error:
        report.download_browser_report("foreign-company-job")
    assert error.value.code == "unknown_report_job"
    plan = report.preview_export(2026, 1)
    task = report.confirm_export(
        2026,
        1,
        preview_digest=plan["digest"],
        epochs=plan["epochs"],
        output_directory=str(tmp_path / "cli-output"),
        request_id="cli-output",
    )
    assert run_report_jobs(book[0])[0]["status"] == "succeeded"
    assert report.browser_export_status(task["job_id"])["status"] == "succeeded"
    with pytest.raises(KernelError) as error:
        report.download_browser_report(task["job_id"])
    assert error.value.code == "report_download_invalid"


def test_browser_download_checks_manifest_and_frozen_company_identity(book):
    report = scenario(book)
    close_quarter(book)
    task = queue(report)
    result = run_report_jobs(book[0])[0]["result"]
    manifest_path = Path(result["directory"]) / "manifest.json"
    manifest = json.loads(manifest_path.read_text("utf-8"))
    manifest["plan"]["company_id"] = "different-company"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(KernelError) as error:
        report.download_browser_report(task["job_id"])
    assert error.value.code == "report_download_invalid"


def test_browser_export_rejects_changed_preview_without_creating_job(book):
    report = scenario(book)
    close_quarter(book)
    plan = report.preview_export(2026, 1)
    with pytest.raises(KernelError) as error:
        report.confirm_browser_export(
            2026,
            1,
            preview_digest="0" * 64,
            epochs=plan["epochs"],
            request_id="stale",
        )
    assert error.value.code == "preview_expired"
    assert book[0].jobs() == []
