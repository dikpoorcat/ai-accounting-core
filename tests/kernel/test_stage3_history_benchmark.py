"""The history benchmark must exercise the real lifecycle and preserve evidence."""

import json
import subprocess
import sys
from pathlib import Path


def test_synthetic_history_benchmark_measures_both_dependency_shapes(tmp_path):
    root = Path(__file__).resolve().parents[2]
    output = tmp_path / "history.json"
    command = [
        sys.executable,
        str(root / "scripts/benchmark_stage3_history.py"),
        "--source",
        str(root),
        "--output",
        str(output),
        "--sizes",
        "1,2",
        "--repeats",
        "1",
        "--timing-context",
        "smoke test; no timing threshold",
    ]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    report = json.loads(output.read_text("utf-8"))
    for workload, checkpoints in report["workloads"].items():
        assert [item["months"] for item in checkpoints] == [1, 2]
        final = checkpoints[-1]
        assert final["calculation_count"] == 4
        assert final["stored_close_result_references"] == 4
        assert final["last_close_result_references"] == 2
        assert final["dependency_edges"] == (4 if workload == "cumulative" else 0)
        assert final["manifest_bytes"] > 0
        assert final["database_logical_bytes"] > final["manifest_bytes"]
        for operation in ("full_verify", "funds_history", "settlement_summary", "brief"):
            sample = final[operation]["samples"][0]
            assert sample["sql_read_calls"] > 0
            assert sample["returned_sql_rows"] > 0
        assert final["target_account_balance"]["samples"][0].get("outcome_decodes", 0) == 0

    # A rerun must explicitly pick another output/company directory; existing
    # synthetic evidence is never silently deleted or replaced.
    saved = output.read_bytes()
    repeated = subprocess.run(command, capture_output=True, text=True, timeout=60)
    assert repeated.returncode != 0
    assert output.read_bytes() == saved

    reread = list(command)
    reread[reread.index("--output") + 1] = str(tmp_path / "reread.json")
    reread[reread.index("--sizes") + 1] = "1,2"
    reread.extend(("--read-existing", str(tmp_path / "history-companies")))
    result = subprocess.run(reread, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    remeasured = json.loads((tmp_path / "reread.json").read_text("utf-8"))
    for workload, checkpoints in remeasured["workloads"].items():
        assert [item["months"] for item in checkpoints] == [1, 2]
        assert [item["database_months"] for item in checkpoints] == [2, 2]
        assert checkpoints[0]["close_last"] is None
        assert (
            checkpoints[0]["manifest_bytes"] == report["workloads"][workload][-1]["manifest_bytes"]
        )
