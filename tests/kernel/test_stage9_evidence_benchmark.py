"""The evidence scale runner must preserve distinct bytes through portable restore."""

import hashlib
import json
import sys
import tempfile
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from scripts.benchmark_stage9_evidence import (
    MIB,
    REPOSITORY,
    finalize_monitor_result,
    foreground_scope,
    payload,
    read_default_brief_http,
    run,
    source_inventory,
    update_memory_peaks,
)


@pytest.fixture
def isolated_stage9_source(monkeypatch):
    # These tests invoke a CLI runner in-process. Its source selector is
    # intentionally process-wide for a real command, so restore both globals
    # before another synthetic fixture chooses its own workspace.
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(REPOSITORY))
    monkeypatch.setattr(sys, "path", sys.path.copy())


def test_foreground_scope_is_default_complete_http_without_browser_rendering(monkeypatch):
    recorded = {}

    class Response:
        status = 200

        def read(self):
            return json.dumps({"data": {}, "selected_period": {"key": "2026-01"}}).encode()

    class Connection:
        def __init__(self, host, port, timeout):
            recorded.update(host=host, port=port, timeout=timeout)

        def request(self, method, path, *, headers):
            recorded.update(method=method, path=path, headers=headers)

        def getresponse(self):
            return Response()

        def close(self):
            recorded["closed"] = True

    monkeypatch.setattr(
        "scripts.benchmark_stage9_evidence.http.client.HTTPConnection", Connection
    )
    assert read_default_brief_http(5000, "synthetic-token", "company-1", "2026-01") > 0
    query = urlsplit(recorded["path"])
    assert (recorded["host"], recorded["port"], recorded["method"]) == (
        "127.0.0.1", 5000, "GET"
    )
    assert query.path == "/api/dashboard/brief"
    assert parse_qs(query.query) == {
        "company_id": ["company-1"], "period": ["2026-01"],
        "limit": ["100"], "preparation": ["complete"],
    }
    assert recorded["headers"] == {"Authorization": "Bearer synthetic-token"}
    assert recorded["closed"] is True
    assert foreground_scope() == {
        "entry": "GET /api/dashboard/brief", "limit": 100,
        "preparation": "complete", "includes_http": True,
        "includes_browser_rendering": False,
        "purpose": "evidence_operations_default_brief_http_contention",
    }
    assert foreground_scope("browser") == {
        "entry": "browser_brief_refresh", "limit": 100,
        "preparation": "complete", "includes_http": True,
        "includes_browser_rendering": True,
        "purpose": "evidence_operations_default_brief_browser_contention",
    }


def test_memory_peak_sum_uses_one_sample_not_independent_process_peaks():
    peaks = {"backup": {"parent": 0, "workers": {}, "sum": 0}}
    update_memory_peaks(peaks, "backup", 100, {11: 2, 12: 3, 13: 4})
    update_memory_peaks(peaks, "backup", 20, {11: 50, 12: 60, 13: 70})
    assert peaks["backup"] == {
        "parent": 100, "workers": {11: 50, 12: 60, 13: 70}, "sum": 200,
    }
    assert peaks["backup"]["sum"] != 100 + 50 + 60 + 70


@pytest.mark.parametrize("fault", ["browser", "rss"])
def test_late_monitor_fault_invalidates_completed_report(fault):
    report = {"status": "complete"}
    foreground_errors = (
        [{"type": "TimeoutError", "message": "refresh failed"}]
        if fault == "browser" else []
    )
    memory_errors = ["worker RSS sample failed"] if fault == "rss" else []
    message = finalize_monitor_result(report, foreground_errors, memory_errors)
    assert message
    assert report["status"] == "failed"
    assert report["error"]["type"] == "MonitorFailure"
    assert "refresh failed" in message or "worker RSS sample failed" in message
    assert finalize_monitor_result({"status": "complete"}, [], []) is None


@pytest.mark.skipif(
    Path(sys.prefix).resolve() != REPOSITORY / ".tmp-kernel-venv",
    reason="the local scale runner requires the repository virtual environment",
)
def test_late_rss_fault_fails_runner_and_persisted_report(monkeypatch, isolated_stage9_source):
    import scripts.benchmark_stage9_evidence as benchmark

    original = benchmark.finalize_monitor_result

    def inject_after_business_work(report, foreground_errors, memory_errors):
        memory_errors.append("injected late RSS failure")
        return original(report, foreground_errors, memory_errors)

    monkeypatch.setattr(benchmark, "finalize_monitor_result", inject_after_business_work)
    temporary = REPOSITORY / ".tmp"
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stage9-evidence-fault-", dir=temporary) as directory:
        root = Path(directory)
        root.rmdir()
        output = temporary / f"{root.name}-result.json"
        try:
            with pytest.raises(AssertionError, match="Evidence monitor failed"):
                run(root, output, size_mib=1, profile="compressible", chunk_mib=1)
            saved = json.loads(output.read_text(encoding="utf-8"))
            assert saved["status"] == "failed"
            assert saved["error"]["type"] == "MonitorFailure"
            assert saved["memory_errors"] == ["injected late RSS failure"]
        finally:
            output.unlink(missing_ok=True)


def test_source_manifest_identity_and_content_are_checked(tmp_path):
    source = tmp_path / "sealed"
    source.mkdir()
    item = source / "sample.py"
    item.write_text("answer = 1\n", encoding="utf-8")
    files = {"sample.py": hashlib.sha256(item.read_bytes()).hexdigest()}
    manifest = {
        "status": "complete", "target": str(source.resolve()),
        "file_count": 1, "files": files,
        "sha256": hashlib.sha256(
            json.dumps(files, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }
    (source / "source-manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    assert source_inventory(source) == {
        "status": "verified", "sha256": manifest["sha256"], "file_count": 1
    }
    item.write_text("answer = 2\n", encoding="utf-8")
    with pytest.raises(ValueError, match="changed after snapshot"):
        source_inventory(source)


def test_evidence_profiles_are_distinct_and_actually_differ_in_compressibility():
    compressible = payload(9, 0, MIB, "compressible")
    random_bytes = payload(9, 1, MIB, "incompressible")
    assert hashlib.sha256(compressible).digest() != hashlib.sha256(random_bytes).digest()
    assert len(set(compressible[i:i + 32] for i in range(0, MIB, 32))) == 1
    assert len(set(random_bytes[i:i + 32] for i in range(0, MIB, 32))) > 30_000


@pytest.mark.skipif(
    Path(sys.prefix).resolve() != REPOSITORY / ".tmp-kernel-venv",
    reason="the local scale runner requires the repository virtual environment",
)
def test_evidence_survives_production_verify_backup_and_restore(isolated_stage9_source):
    temporary = REPOSITORY / ".tmp"
    temporary.mkdir(exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="stage9-evidence-test-", dir=temporary) as directory:
        root = Path(directory)
        root.rmdir()  # The scale tool itself must create the absent synthetic root.
        output = temporary / f"{root.name}-result.json"
        try:
            report = run(root, output, size_mib=1, profile="compressible", chunk_mib=1)
        finally:
            output.unlink(missing_ok=True)
        evidence = report["evidence"]
        assert report["status"] == "complete"
        assert evidence["count"] == evidence["distinct_digests"] == 1
        assert evidence["content_bytes"] == MIB
        assert report["restore"]["synthetic_evidence"] == {
            "count": 1, "distinct_digests": 1, "content_bytes": MIB,
        }
        assert report["backup"]["zip_bytes"] < MIB
        assert report["foreground"]["errors"] == []
        assert report["foreground"]["scope"] == foreground_scope()
        assert report["foreground"]["parallel_brief_enabled"] is True
        assert report["foreground"]["response_bytes"] > 0
        assert all(
            "samples" in report["foreground"][name]
            for name in ("verify", "backup", "restore", "restored_verify")
        )
        assert report["static_runtime_prepared_before_business_root"] is True
        assert report["company_format"] == report["backup"]["database_format"]
        assert report["source_inventory"]["status"] == "unsealed_working_tree"
        assert report["measurement_harness"]["separate_from_fixed_source"] is False
        assert len(report["brief_worker_pids"]) == 3
        assert len(set(report["brief_worker_pids"])) == 3
        assert report["memory_errors"] == []
        assert report["memory_scope"]["total_peak"].startswith("maximum_of_parent_plus_workers")
        assert report["sampled_peak_parent_plus_brief_workers_rss_bytes"] >= (
            report["sampled_peak_rss_bytes"]
        )
        for name in ("verify", "backup", "restore", "restored_verify"):
            measured = report["phases"][name]
            assert set(measured["sampled_peak_brief_worker_rss_by_pid_bytes"]) == set(
                report["brief_worker_pids"]
            )
            assert measured["sampled_peak_parent_plus_brief_workers_rss_bytes"] >= (
                measured["sampled_peak_rss_bytes"]
            )
