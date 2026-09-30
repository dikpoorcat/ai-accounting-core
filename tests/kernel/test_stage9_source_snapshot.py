"""Source snapshots must be byte-identical before a synthetic build starts."""

import json

import pytest

from scripts import snapshot_stage9_source


def _source(path):
    files = {
        "src/ai_accounting/__init__.py": "# implementation\n",
        "src/ai_accounting/kernel/example.py": "VALUE = 1\n",
        "tests/kernel/stage9_book.py": "# mixed fixture\n",
        "tests/kernel/stage9_independent_book.py": "# independent fixture\n",
        "tests/pure/test_example.py": "# pure test\n",
        "tests/conftest.py": "# conftest\n",
        "scripts/benchmark_stage9.py": "# builder\n",
        "scripts/benchmark_stage9_browser.py": "# browser harness\n",
        "frontend/dist/index.html": "<html>built</html>\n",
        "frontend/tests/browser-stage9-hot-refresh.cjs": "// fixed browser harness\n",
        "frontend/package.json": "{}\n",
        "frontend/package-lock.json": "{}\n",
        "frontend/vite.config.ts": "// config\n",
        "frontend/tsconfig.json": "{}\n",
        "pyproject.toml": "[project]\nname = 'synthetic'\n",
    }
    for name, content in files.items():
        item = path / name
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text(content, encoding="utf-8")
    ignored = path / "src/ai_accounting/__pycache__/example.pyc"
    ignored.parent.mkdir(parents=True)
    ignored.write_bytes(b"cached")
    return path


def test_snapshot_records_equal_before_after_and_copied_bytes(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = _source(tmp_path / "source")
    target = workspace / ".tmp/stage9-source-copy"
    result = snapshot_stage9_source.snapshot_source(source, target, workspace=workspace)
    saved = json.loads((target / "source-manifest.json").read_text(encoding="utf-8"))
    assert result == saved
    assert saved["status"] == "complete"
    assert saved["files"] == snapshot_stage9_source._inventory(source)
    assert saved["files"] == snapshot_stage9_source._inventory(target)
    assert "src/ai_accounting/__pycache__/example.pyc" not in saved["files"]
    assert "frontend/tests/browser-stage9-hot-refresh.cjs" in saved["files"]
    assert "frontend/package-lock.json" in saved["files"]
    with pytest.raises(ValueError, match="must not exist"):
        snapshot_stage9_source.snapshot_source(source, target, workspace=workspace)


def test_snapshot_never_publishes_manifest_when_source_changes(tmp_path, monkeypatch):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = _source(tmp_path / "source")
    target = workspace / ".tmp/stage9-drifting-copy"
    original = snapshot_stage9_source._inventory
    source_reads = 0

    def drift(path):
        nonlocal source_reads
        if path == source:
            source_reads += 1
            if source_reads == 2:
                (source / "src/ai_accounting/kernel/example.py").write_text(
                    "VALUE = 2\n", encoding="utf-8"
                )
        return original(path)

    monkeypatch.setattr(snapshot_stage9_source, "_inventory", drift)
    with pytest.raises(RuntimeError, match="changed during copy"):
        snapshot_stage9_source.snapshot_source(source, target, workspace=workspace)
    assert not (target / "source-manifest.json").exists()
    audit = json.loads((target / "copy-audit.json").read_text(encoding="utf-8"))
    assert audit["status"] == "incomplete"
    copied = target / "src/ai_accounting/kernel/example.py"
    assert copied.read_text(encoding="utf-8") == "VALUE = 1\n"
