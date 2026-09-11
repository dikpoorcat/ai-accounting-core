"""Browser exports use frozen jobs and never expose arbitrary local file reads."""

import io
import json
from pathlib import Path

import pytest
import test_reports as report_cases
from openpyxl import load_workbook
from test_reports import close_quarter, scenario

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.reports import run_report_jobs

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
    assert report.browser_job_results(book[0].jobs())[0]["download_available"] is False
    assert book[0].jobs(job_id="another-company-job") == []
    with pytest.raises(KernelError, match="尚未生成成功"):
        report.download_browser_report(task["job_id"])
    result = run_report_jobs(book[0])[0]
    assert result["status"] == "succeeded"
    name, content = report.download_browser_report(task["job_id"])
    available = report.browser_job_results(book[0].jobs())[0]
    assert available["download_available"] is True
    assert available["download_file_name"] == name
    assert name.endswith("2026Q1.xlsx")
    workbook = load_workbook(io.BytesIO(content), data_only=True)
    assert workbook.worksheets[0]["D7"].value == 400
    workbook.close()
    assert queue(report) == task
    assert book[0].jobs(job_id=task["job_id"])[0]["status"] == "succeeded"
    Path(result["result"]["path"]).write_bytes(b"changed file")
    assert report.browser_job_results(book[0].jobs())[0]["download_available"] is False
    with pytest.raises(KernelError) as error:
        report.download_browser_report(task["job_id"])
    assert error.value.code == "report_download_invalid"


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
    assert report.browser_job_results(book[0].jobs())[0]["download_available"] is False
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
