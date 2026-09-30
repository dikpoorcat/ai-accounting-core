"""Small calibration of the opt-in Stage 9 production-entry boundary harness."""

import json
import subprocess
import sys
from pathlib import Path


def test_synthetic_boundary_harness_uses_fixed_source_and_new_root(tmp_path):
    source = Path(__file__).resolve().parents[2]
    (tmp_path / ".tmp").mkdir()
    root = tmp_path / ".tmp" / "stage9-boundaries-calibration"
    command = [
        sys.executable,
        str(source / "scripts/benchmark_stage9_boundaries.py"),
        "--source",
        str(source),
        "--workspace",
        str(tmp_path),
        "--root",
        str(root),
        "--calibration-rows",
        "2",
        "--calibration-facts",
        "2",
    ]
    subprocess.run(command, check=True, capture_output=True, text=True)
    report = json.loads((root / "report.json").read_text(encoding="utf-8"))
    assert report["mode"] == "calibration"
    assert report["source"] == str(source)
    assert report["bank_rows"]["physical_entries"] == 2
    assert report["bank_rows"]["over_limit"] == {"rows": 100_001, "status": "not_run"}
    assert report["batch"]["physical_fact_delta"] == 2
    assert report["batch"]["over_limit"] == {"facts": 5001, "status": "not_run"}
    assert report["evidence"]["over_limit"] == {
        "bytes": 20 * 1024 * 1024 + 1,
        "status": "not_run",
    }
    assert report["final_physical_counts"]["fact_bank_statement_entries"] == 2

    # A localized Windows traceback may contain bytes outside the parent's
    # UTF-8 mode. The exception class itself is ASCII and remains observable.
    repeated = subprocess.run(command, capture_output=True)
    assert repeated.returncode != 0
    assert b"FileExistsError" in repeated.stderr
    assert json.loads((root / "report.json").read_text(encoding="utf-8")) == report
