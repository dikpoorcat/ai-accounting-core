"""Guard the browser benchmark against accepting partial synthetic books."""

import json
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from scripts import benchmark_stage9_browser
from scripts.benchmark_stage9_browser import (
    browser_source_paths,
    measurement_scope,
    prepare_browser_service,
    prepare_browser_static_runtime,
    require_report_source,
    sanitized_browser_stderr,
    validate_book_report,
)
from scripts.benchmark_stage9_switch_company import complete_switch_report


def complete_report():
    periods = ("2016-01", "2016-02")
    report = {
        "status": "complete",
        "source": "synthetic-source",
        "company": {
            "id": "synthetic-company",
            "database_id": "synthetic-database",
            "name": "阶段九合成独立业务企业",
        },
        "integrity": {"status": "verified"},
        "distribution": "independent_local_pairs",
        "requested_months": 2,
        "monthly_business_count": 1000,
        "business_count": 2000,
        "months": [
            {"period": period, "business_count": 1000, "closed": index == 0}
            for index, period in enumerate(periods)
        ],
        "snapshots": {
            period: {
                "closed": index == 0,
                "owner_confirmation": "synthetic-proof",
                "preview_digest": "synthetic-preview",
            }
            for index, period in enumerate(periods)
        },
    }
    report["verified_open_preview"] = fake_current_preview(report)
    return report


def fake_current_preview(report):
    period = report["months"][-1]["period"]
    return {
        "source": report["source"],
        "company_id": report["company"]["id"],
        "database_id": report["company"]["database_id"],
        "period": period,
        "construction_preview_digest": report["snapshots"][period]["preview_digest"],
        "digest": "current-source-preview",
        "epochs": {"accounting": 0, "material": 0, "management": 0},
        "state": [1, 0, 0, 0, 1, 0],
    }


def test_limited_integrity_cannot_become_a_timed_page_sample():
    report = complete_report()
    report["integrity"]["limitations"] = ["synthetic coverage gap"]
    with pytest.raises(ValueError, match="integrity must be verified"):
        validate_book_report(report, company_name="阶段九合成独立业务企业")


def test_timing_gate_waits_until_after_ready_without_exposing_credentials(tmp_path, monkeypatch):
    ready, release = tmp_path / "ready.json", tmp_path / "release"
    waits = []

    def allow_timing(delay):
        waits.append(delay)
        assert json.loads(ready.read_text()) == {
            "status": "timing_ready", "pid": 123, "output": "report.json"
        }
        release.touch()

    monkeypatch.setattr(benchmark_stage9_browser.os, "getpid", lambda: 123)
    monkeypatch.setattr(benchmark_stage9_browser.time, "sleep", allow_timing)
    benchmark_stage9_browser.wait_for_timing_release(ready, release, output="report.json")
    assert waits == [0.2]


def test_timing_gate_rejects_stale_release_and_times_out_without_starting(tmp_path):
    ready, release = tmp_path / "ready.json", tmp_path / "release"
    release.touch()
    with pytest.raises(ValueError, match="after the ready"):
        benchmark_stage9_browser.wait_for_timing_release(ready, release, output="report.json")
    assert not ready.exists()
    release.unlink()
    with pytest.raises(TimeoutError, match="No timing release"):
        benchmark_stage9_browser.wait_for_timing_release(
            ready, release, output="report.json", timeout=0
        )
    assert ready.is_file() and not release.exists()


def test_complete_book_selects_last_open_month():
    assert (
        validate_book_report(complete_report(), company_name="阶段九合成独立业务企业") == "2016-02"
    )


def test_complete_shape_can_be_checked_before_independent_integrity_verification():
    report = complete_report()
    report["status"] = "measuring"
    report.pop("integrity")
    original = deepcopy(report)
    assert (
        validate_book_report(report, company_name="阶段九合成独立业务企业", require_verified=False)
        == "2016-02"
    )
    assert report == original
    with pytest.raises(ValueError):
        validate_book_report(report, company_name="阶段九合成独立业务企业")
    report["business_count"] -= 1
    with pytest.raises(ValueError):
        validate_book_report(report, company_name="阶段九合成独立业务企业", require_verified=False)


@pytest.mark.parametrize(
    "mutation", ("short", "closed", "missing_preview", "count", "missing_requested")
)
def test_partial_or_mislabeled_book_cannot_enter_browser(mutation):
    report = deepcopy(complete_report())
    if mutation == "short":
        report["months"].pop()
        report["snapshots"].pop("2016-02")
        report["business_count"] = 1000
    elif mutation == "closed":
        report["months"][-1]["closed"] = True
        report["snapshots"]["2016-02"]["closed"] = True
    elif mutation == "missing_preview":
        report["snapshots"]["2016-02"]["preview_digest"] = ""
    elif mutation == "missing_requested":
        report.pop("requested_months")
    else:
        report["business_count"] -= 1
    with pytest.raises(ValueError):
        validate_book_report(report, company_name="阶段九合成独立业务企业")


def test_mixed_book_must_keep_requested_months_to_prove_completion():
    report = complete_report()
    report["distribution"] = "mixed_cumulative"
    report["company"]["name"] = "阶段九合成规模企业"
    report.pop("requested_months")
    with pytest.raises(ValueError):
        validate_book_report(report, company_name="阶段九合成规模企业")


def test_measurement_scope_separates_final_page_candidates_from_diagnostics():
    report = complete_report()
    report.update(distribution="mixed_cumulative", employee_count=50, requested_months=12)
    options = {"repeats": 30, "warmups": 3, "instrument": False, "navigation_only": None}
    assert measurement_scope(report, **options) == "main_page_candidate"
    assert measurement_scope(report, **(options | {"static_build": "dist"})) == "diagnostic"
    assert measurement_scope(report, **(options | {"repeats": 3})) == "diagnostic"
    assert measurement_scope(report, **(options | {"instrument": True})) == "diagnostic"
    assert measurement_scope(report, **(options | {"navigation_only": "cold"})) == "navigation_cold"
    report.update(distribution="independent_local_pairs", registered_object_count=50)
    assert measurement_scope(report, **options) == "independent_page_candidate"
    report.update(distribution="mixed_cumulative", employee_count=200, monthly_business_count=5000)
    assert measurement_scope(report, **options) == "pressure_diagnostic"


def test_browser_build_and_harness_must_share_selected_source(tmp_path):
    source = tmp_path / "selected"
    release = source / "src/ai_accounting/static/dashboard"
    dist = source / "frontend/dist"
    harness = source / "frontend/tests/browser-stage9-hot-refresh.cjs"
    dist.mkdir(parents=True)
    harness.parent.mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html>", encoding="utf-8")
    with pytest.raises(ValueError, match="release static build"):
        browser_source_paths(source)
    harness.write_text("// synthetic harness", encoding="utf-8")
    assert browser_source_paths(source, static_build="dist") == (dist, harness)
    with pytest.raises(ValueError, match="release static build"):
        browser_source_paths(source)
    release.mkdir(parents=True)
    (release / "index.html").write_text("<!doctype html>", encoding="utf-8")
    assert browser_source_paths(source) == (release, harness)
    with pytest.raises(ValueError, match="Unknown Stage 9"):
        browser_source_paths(source, static_build="unknown")


def test_browser_wait_guard_defers_rejection_without_swallowing_it():
    node = shutil.which("node")
    if node is None:
        pytest.skip("Node.js is unavailable")
    harness = Path(__file__).resolve().parents[2] / "frontend/tests/browser-stage9-hot-refresh.cjs"
    script = """
const assert = require('node:assert/strict');
const { waitForLater } = require(process.argv[1]);
(async () => {
  const error = new Error('expected timeout');
  const pending = waitForLater(Promise.reject(error));
  await new Promise(resolve => setTimeout(resolve, 20));
  await assert.rejects(pending, actual => actual === error);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
    result = subprocess.run(
        [node, "--unhandled-rejections=strict", "-e", script, str(harness)],
        capture_output=True, text=True, timeout=10,
    )
    assert result.returncode == 0, result.stderr


def test_browser_stderr_diagnostic_redacts_secrets_and_keeps_timeout():
    diagnostic = sanitized_browser_stderr(
        "page.waitForResponse: Timeout 30000ms exceeded at "
        "http://127.0.0.1:1000/#ticket=secret-ticket "
        "Bearer secret-token C:\\private\\runner.js password=secret-password",
        secrets=("secret-ticket", "secret-token", "secret-password"),
    )
    assert "Timeout 30000ms exceeded" in diagnostic
    assert "secret-ticket" not in diagnostic
    assert "secret-token" not in diagnostic
    assert "secret-password" not in diagnostic
    assert "C:\\private" not in diagnostic


def test_browser_report_requires_exact_absolute_source(tmp_path):
    source = tmp_path / "source"
    with pytest.raises(ValueError, match="absolute source"):
        require_report_source({}, source)
    with pytest.raises(ValueError, match="differs"):
        require_report_source({"source": str(tmp_path / "other")}, source)
    require_report_source({"source": str(source)}, source)


def test_browser_prepares_static_runtime_before_opening_synthetic_catalog(monkeypatch, tmp_path):
    from ai_accounting.kernel import daemon, service

    events = []
    prepared = (object(), {"prepared-command": object()})

    def prepare():
        events.append("static")
        return prepared

    class FakeService:
        def __init__(self, root, *, enable_read_pool, enable_parallel_brief, _static_runtime):
            assert events == ["static"]
            assert root == tmp_path
            assert enable_read_pool is True
            assert enable_parallel_brief is True
            assert _static_runtime is prepared
            events.append("catalog")

    monkeypatch.setattr(daemon, "_prepare_static_runtime", prepare)
    monkeypatch.setattr(service, "LocalService", FakeService)
    static_runtime, static_startup_ms = prepare_browser_static_runtime()
    app, startup_ms = prepare_browser_service(tmp_path, static_runtime, static_startup_ms)
    assert isinstance(app, FakeService)
    assert startup_ms >= static_startup_ms >= 0
    assert events == ["static", "catalog"]


def test_browser_static_freeze_precedes_book_report_json_load(monkeypatch, tmp_path):
    from ai_accounting.kernel import daemon

    # The CLI source selector changes these process globals before reading the
    # intentionally incomplete report. Keep that in-process test isolated.
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(tmp_path))
    monkeypatch.setattr(sys, "path", sys.path.copy())
    (tmp_path / ".tmp").mkdir()
    report_path = tmp_path / ".tmp/stage9-synthetic-book.json"
    report_path.write_text(
        json.dumps({"source": str(Path(__file__).resolve().parents[2])}), encoding="utf-8"
    )
    output_path = tmp_path / ".tmp/stage9-unused-result.json"
    events = []
    original_read_text = Path.read_text

    def read_text(path, *args, **kwargs):
        if path == report_path:
            events.append("report")
        return original_read_text(path, *args, **kwargs)

    monkeypatch.setattr(daemon, "_prepare_static_runtime", lambda: events.append("static") or ())
    monkeypatch.setattr(Path, "read_text", read_text)
    monkeypatch.setattr(benchmark_stage9_browser.sys, "prefix", str(tmp_path / ".tmp-kernel-venv"))
    monkeypatch.setattr(
        benchmark_stage9_browser.sys,
        "argv",
        [
            "benchmark_stage9_browser.py",
            "--book-report",
            str(report_path),
            "--workspace",
            str(tmp_path),
            "--output",
            str(output_path),
            "--node",
            str(tmp_path / "node"),
            "--playwright-module",
            str(tmp_path / "playwright"),
        ],
    )
    with pytest.raises(KeyError, match="root"):
        benchmark_stage9_browser.main()
    assert events == ["static", "report"]
    assert not output_path.exists()


def test_switch_report_passes_same_complete_book_gate(tmp_path):
    class Book:
        def describe(self):
            report = complete_report()
            report["company"].update(id="switch", name="阶段九合成切换企业")
            report["requested_months"] = 1
            report["monthly_business_count"] = 40
            report["business_count"] = 40
            report["months"] = [{"period": "2016-01", "business_count": 40, "closed": False}]
            report["snapshots"] = {"2016-01": report["snapshots"]["2016-01"]}
            report["snapshots"]["2016-01"]["closed"] = False
            return report

    format_value = {"status": "released", "version": 1}
    report = complete_switch_report(
        Book(), {"status": "verified"}, format_value, tmp_path, "primary", tmp_path
    )
    assert report["primary_company_id"] == "primary"
    assert report["integrity_contract"] == format_value
    assert report["source"] == str(tmp_path)
    report["verified_open_preview"] = fake_current_preview(report)
    assert validate_book_report(report, company_name="阶段九合成切换企业") == "2016-01"
