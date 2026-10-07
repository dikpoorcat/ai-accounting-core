"""Preparation receipts must reflect worker exits and preserve reusable evidence."""

import json
import sys
from pathlib import Path

import pytest

from scripts import prepare_stage9_suite as suite


@pytest.fixture
def preparation(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = workspace / ".tmp/stage9-fixed-source"
    (source / "scripts").mkdir(parents=True)
    worker = source / "scripts/worker.py"
    worker.write_text(
        "import argparse,json,sys\n"
        "p=argparse.ArgumentParser()\n"
        "p.add_argument('--source');p.add_argument('--workspace')\n"
        "p.add_argument('--output');p.add_argument('--exit',type=int,default=0)\n"
        "a=p.parse_args()\n"
        "open(a.output,'w').write(json.dumps({'status':'complete','value':1}))\n"
        "print('worker output');print('worker diagnostic',file=sys.stderr)\n"
        "sys.exit(a.exit)\n", encoding="utf-8",
    )
    task = suite.Preparation.__new__(suite.Preparation)
    task.workspace, task.source, task.run_id = workspace, source, "test"
    task.python, task.sha = Path(sys.executable), "fixed"
    task.path = task.file("preparation", ".json")
    task.report = {"processes": [], "samples": {}, "smoke": {}}
    monkeypatch.setattr(suite, "fixed_source", lambda source: "fixed")
    return task


def run(task, output, code=0):
    task.command("verify", "worker.py", ["--output", output, "--exit", code],
                 expected_status="complete", report_path=output)


def test_nonzero_worker_cannot_pass_with_success_shaped_output(preparation):
    output = preparation.file("result", ".json")
    with pytest.raises(RuntimeError, match="exited 7"):
        run(preparation, output, 7)
    saved = suite.read_json(preparation.path)["processes"][0]
    assert saved["status"] == "failed" and saved["exit_code"] == 7
    assert "worker diagnostic" in Path(saved["stderr"]).read_text()
    assert suite.read_json(output)["status"] == "complete"
    with pytest.raises(ValueError, match="Preserve existing output"):
        run(preparation, output)
    assert len(preparation.report["processes"]) == 1


def test_success_reuse_preserves_receipt_and_rejects_rewritten_output(preparation):
    output = preparation.file("result", ".json")
    run(preparation, output)
    run(preparation, output)
    assert len(preparation.report["processes"]) == 1
    output.write_text(json.dumps({"status": "complete", "value": 2}), encoding="utf-8")
    with pytest.raises(ValueError, match="output bytes changed"):
        run(preparation, output)


def test_success_reuse_rejects_changed_fixed_runtime(preparation, monkeypatch):
    output = preparation.file("result", ".json")
    run(preparation, output)
    monkeypatch.setattr(suite, "fixed_source", lambda source: "changed")
    with pytest.raises(ValueError, match="Fixed source changed"):
        run(preparation, output)


def test_scale_matrix_keeps_pressure_separate():
    cases = suite.sample_cases()
    main = [case for case in cases if case["purpose"] == "scale_acceptance"]
    assert {(case["kind"], case["months"]) for case in main} == {
        (kind, months) for kind in ("main", "independent") for months in (12, 48, 120)
    }
    assert all(case["people"] == 50 and case["businesses"] == 1000 for case in main)
    pressure, = [case for case in cases if case["purpose"] == "pressure_diagnostic"]
    assert pressure["people"] == 200 and pressure["businesses"] == 5000


def test_slow_complete_smoke_is_preserved_but_failed_partial_and_bad_exit_are_rejected():
    result = {"status": "over_target", "sample_count": 1, "warmups": 3,
              "pages": {name: {"samples": [620]}
                        for name in ("brief", "funds", "employees", "assets", "reports")}}
    suite.require_diagnostic_smoke(result, 1)
    with pytest.raises(ValueError, match="unexpected exit"):
        suite.require_diagnostic_smoke(result, 7)
    result["status"] = "failed"
    with pytest.raises(ValueError, match="technical failure"):
        suite.require_diagnostic_smoke(result, 1)
    result["status"] = "over_target"
    result["pages"]["assets"]["samples"] = []
    with pytest.raises(ValueError, match="all five"):
        suite.require_diagnostic_smoke(result, 1)


def test_final_accumulators_reused_without_downgrading_verified_samples(preparation, monkeypatch):
    copied = []
    monkeypatch.setattr(preparation, "construct", lambda *args, **kwargs: None)

    def copy(case, source_root, target_root, source_report):
        copied.append((case["key"], source_root, target_root))
        return {"status": "built_not_verified"}

    monkeypatch.setattr(preparation, "copy", copy)
    preparation.build()
    assert {key for key, _, _ in copied} == {"main12", "main48", "independent12", "independent48"}
    for kind in ("main", "independent"):
        final = preparation.report["samples"][f"{kind}120"]
        assert final["root"] == str(preparation.file(f"{kind}120"))
        assert final["built_report"].endswith(f"{kind}120-construction.json")
        assert all(source == preparation.file(f"{kind}120")
                   for key, source, _ in copied if key.startswith(kind))
    for sample in preparation.report["samples"].values():
        sample.update(status="verified", book_report="saved qualification")
    preparation.build()
    assert preparation.report["status"] == "samples_verified"
    assert all(sample["status"] == "verified" and sample["book_report"] == "saved qualification"
               for sample in preparation.report["samples"].values())
    preparation.report["smoke"] = {kind: {"status": "passed"}
                                   for kind in ("main", "independent")}
    assert preparation.completion_status() == "ready"
    preparation.report["smoke"]["main"]["status"] = "failed"
    assert preparation.completion_status() == "samples_verified"
