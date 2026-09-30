"""The diagnostic counts work moved to brief workers instead of losing it."""

import sys

import pytest
from stage9_book import MixedBook

from ai_accounting.kernel.daemon import _prepare_static_runtime
from scripts.benchmark_stage9_parallel_work import _sum_work, main, measure_brief_pair
from scripts.stage9_source import synthetic_path


def test_parallel_work_sums_parent_including_guard_and_three_workers(tmp_path):
    book = MixedBook(tmp_path / "stage9-parallel-work", employees=1, businesses=26)
    book.add_month(0, close=False)
    report = measure_brief_pair(
        book.root,
        book.company["id"],
        book.month_stats[0]["period"],
        _prepare_static_runtime(),
    )

    assert report["response_equal"]
    assert report["response_bytes"] > 0
    assert set(report["parallel"]["workers"]) == {"materials", "duplicates", "reports"}
    assert len(set(report["parallel"]["worker_pids"].values())) == 3
    reports = [
        report["parallel"]["parent"],
        *report["parallel"]["workers"].values(),
    ]
    assert report["parallel"]["total"] == _sum_work(reports)
    assert report["serial"]["total"] == _sum_work((report["serial"]["parent"],))
    assert report["parallel"]["guard_work_included_in_parent"] is True
    guard_sql = [
        item for item in report["parallel"]["parent"]["sql"]
        if item["statement"] == "PRAGMA data_version"
    ]
    assert len(guard_sql) == 1 and guard_sql[0]["calls"] == 2
    for item in (
        report["serial"]["parent"],
        report["parallel"]["parent"],
        *report["parallel"]["workers"].values(),
    ):
        assert item["counters"]["sql_calls"] > 0
        assert item["python_peak_bytes"] > 0
    assert "cannot be summed" in report["measurement_scope"]


def test_parallel_work_paths_are_only_named_workspace_synthetics(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    assert synthetic_path(workspace / ".tmp/stage9-work.json", workspace) == (
        workspace / ".tmp/stage9-work.json"
    )
    for unsafe in (workspace / "book.json", workspace / ".tmp/book.json"):
        try:
            synthetic_path(unsafe, workspace)
        except ValueError:
            pass
        else:
            raise AssertionError(f"accepted non-stage9 path: {unsafe}")


def test_parallel_work_cli_rejects_non_synthetic_book_and_existing_output(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    output = workspace / ".tmp/stage9-work.json"
    args = [
        "benchmark_stage9_parallel_work.py",
        "--workspace",
        str(workspace),
        "--book-report",
        str(workspace / "book.json"),
        "--output",
        str(output),
    ]
    monkeypatch.setattr(sys, "argv", args)
    with pytest.raises(SystemExit):
        main()
    assert not output.exists()

    book_path = workspace / ".tmp/stage9-book.json"
    book_path.write_text("{}", encoding="utf-8")
    output.write_text("previous result", encoding="utf-8")
    monkeypatch.setattr(sys, "argv", [*args[:4], str(book_path), *args[5:]])
    with pytest.raises(SystemExit):
        main()
    assert output.read_text(encoding="utf-8") == "previous result"
