"""Fixed-source benchmark guards without opening a business database."""

import os
import subprocess
import sys
from pathlib import Path

import pytest
from stage9_book import synthetic_temporary

from scripts.stage9_source import synthetic_path, workspace_root


def _source(path: Path) -> Path:
    for name, content in (
        ("src/ai_accounting/__init__.py", "ORIGIN = 'snapshot'\n"),
        ("tests/kernel/stage9_book.py", "ORIGIN = 'snapshot'\n"),
        ("tests/kernel/stage9_independent_book.py", "ORIGIN = 'snapshot'\n"),
        ("tests/kernel/stage9_metrics.py", "ORIGIN = 'snapshot'\n"),
        ("scripts/benchmark_stage9_browser.py", "ORIGIN = 'snapshot'\n"),
    ):
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
    return path


def test_selected_source_precedes_active_fixture_in_new_process(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = _source(tmp_path / "snapshot")
    active = tmp_path / "active"
    active.mkdir()
    (active / "stage9_book.py").write_text("ORIGIN = 'active'\n", encoding="utf-8")
    script = "\n".join(
        (
            "import sys",
            "from pathlib import Path",
            "sys.path.insert(0, sys.argv[1])",
            "sys.path.insert(0, sys.argv[2])",
            "from stage9_source import configure_source, require_source_module",
            "source = configure_source(Path(sys.argv[3]), Path(sys.argv[4]))",
            "import stage9_book, ai_accounting",
            "require_source_module(stage9_book, source, 'tests/kernel/stage9_book.py')",
            "require_source_module(ai_accounting, source, 'src/ai_accounting/__init__.py')",
            "assert stage9_book.ORIGIN == ai_accounting.ORIGIN == 'snapshot'",
        )
    )
    scripts = Path(__file__).resolve().parents[2] / "scripts"
    completed = subprocess.run(
        [sys.executable, "-c", script, str(active), str(scripts), str(source), str(workspace)],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr


def test_workspace_environment_and_named_synthetic_paths(tmp_path, monkeypatch):
    first, second = tmp_path / "first", tmp_path / "second"
    (first / ".tmp").mkdir(parents=True)
    (second / ".tmp").mkdir(parents=True)
    monkeypatch.setenv("STAGE9_WORKSPACE_ROOT", str(first))
    assert workspace_root(second) == first
    assert synthetic_temporary() == first / ".tmp"
    assert workspace_root(first, second) == second
    assert synthetic_path(second / ".tmp/stage9-new", second) == second / ".tmp/stage9-new"
    for path in (
        second / ".tmp/ordinary",
        second / ".tmp/stage9-parent/stage9-nested",
        second / "stage9-outside",
        first / ".tmp/stage9-old",
    ):
        with pytest.raises(ValueError, match="Stage 9 synthetic paths"):
            synthetic_path(path, second)
    monkeypatch.delenv("STAGE9_WORKSPACE_ROOT")
    with pytest.raises(ValueError, match="existing .tmp"):
        workspace_root(tmp_path / "missing")


def test_selected_source_requires_both_fixture_files(tmp_path, monkeypatch):
    from scripts.stage9_source import configure_source

    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = tmp_path / "missing-fixture"
    (source / "src/ai_accounting").mkdir(parents=True)
    (source / "src/ai_accounting/__init__.py").write_text("", encoding="utf-8")
    original_path = sys.path[:]
    try:
        with pytest.raises(ValueError, match="implementation, fixtures"):
            configure_source(source, workspace)
        assert sys.path == original_path
        assert os.environ.get("STAGE9_WORKSPACE_ROOT") != str(workspace)
    finally:
        monkeypatch.setattr(sys, "path", original_path)


def test_main_benchmark_rejects_outside_workspace_before_import(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = _source(tmp_path / "snapshot")
    script = Path(__file__).resolve().parents[2] / "scripts/benchmark_stage9.py"
    completed = subprocess.run(
        [
            sys.executable, str(script), "--source", str(source), "--workspace", str(workspace),
            "--root", str(tmp_path / "stage9-outside"),
            "--output", str(workspace / ".tmp/stage9-report.json"),
            "--read-existing", "--verify-only",
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 2
    assert "Stage 9 synthetic paths" in completed.stderr
    assert not (workspace / ".tmp/stage9-report.json").exists()


def test_independent_wrapper_executes_selected_fixture(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = _source(tmp_path / "snapshot")
    fixture = source / "tests/kernel/stage9_independent_book.py"
    fixture.write_text(
        "from pathlib import Path\n"
        "import sys\n"
        "Path(sys.argv[-1]).write_text(__file__, encoding='utf-8')\n",
        encoding="utf-8",
    )
    output = workspace / ".tmp/stage9-selected-fixture.txt"
    script = Path(__file__).resolve().parents[2] / "scripts/benchmark_stage9_independent.py"
    completed = subprocess.run(
        [
            sys.executable, str(script), "--source", str(source), "--workspace", str(workspace),
            str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr
    assert output.read_text(encoding="utf-8") == str(fixture.resolve())


def test_fork_rejects_outside_source_without_creating_target(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = _source(tmp_path / "snapshot")
    target = workspace / ".tmp/stage9-target"
    output = workspace / ".tmp/stage9-fork-report.json"
    script = Path(__file__).resolve().parents[2] / "scripts/fork_stage9_book.py"
    completed = subprocess.run(
        [
            sys.executable, str(script), "--code-source", str(source),
            "--workspace", str(workspace), "--source", str(tmp_path / "stage9-outside"),
            "--target", str(target),
            "--archive-directory", str(workspace / ".tmp/stage9-archive"),
            "--output", str(output),
        ],
        text=True,
        capture_output=True,
        check=False,
    )
    assert completed.returncode != 0
    assert "Stage 9 synthetic paths" in completed.stderr
    assert not target.exists() and not output.exists()
