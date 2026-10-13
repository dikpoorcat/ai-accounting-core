"""Source snapshots must be byte-identical before a synthetic build starts."""

import json

import pytest

from scripts import snapshot_stage9_source


def _source(path, *, diagnostic_dist=True):
    files = {
        "src/ai_accounting/__init__.py": "# implementation\n",
        "src/ai_accounting/kernel/example.py": "VALUE = 1\n",
        "src/ai_accounting/static/dashboard/index.html": "<html>release</html>\n",
        "src/ai_accounting/static/dashboard/assets/main.js": "// shipped dashboard\n",
        "tests/kernel/stage9_book.py": "# mixed fixture\n",
        "tests/kernel/stage9_independent_book.py": "# independent fixture\n",
        "tests/pure/test_example.py": "# pure test\n",
        "tests/conftest.py": "# conftest\n",
        "scripts/benchmark_stage9.py": "# builder\n",
        "scripts/benchmark_stage9_browser.py": "# browser harness\n",
        "scripts/stage9_history_adjustment.py": "# sample-only adjustment\n",
        "frontend/src/App.vue": "<template>老板看板</template>\n",
        "frontend/src/main.ts": "// application entry\n",
        "frontend/tests/browser-stage9-hot-refresh.cjs": "// fixed browser harness\n",
        "frontend/package.json": "{}\n",
        "frontend/package-lock.json": "{}\n",
        "frontend/vite.config.ts": "// config\n",
        "frontend/local-api-proxy.ts": "// local service discovery\n",
        "frontend/scripts/check-contract-samples.mjs": "// fixed sample checker\n",
        "frontend/scripts/generate-dashboard-contracts.mjs": "// fixed contract generator\n",
        "frontend/tsconfig.json": "{}\n",
        "pyproject.toml": "[project]\nname = 'synthetic'\n",
    }
    if diagnostic_dist:
        files["frontend/dist/index.html"] = "<html>diagnostic</html>\n"
    for name, content in files.items():
        item = path / name
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text(content, encoding="utf-8")
    ignored = path / "src/ai_accounting/__pycache__/example.pyc"
    ignored.parent.mkdir(parents=True)
    ignored.write_bytes(b"cached")
    for name in ("frontend/node_modules/example/index.js", "frontend/scripts/check-result.tmp"):
        item = path / name
        item.parent.mkdir(parents=True, exist_ok=True)
        item.write_text("runtime or temporary artifact", encoding="utf-8")
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
    assert "frontend/local-api-proxy.ts" in saved["files"]
    assert "frontend/scripts/check-contract-samples.mjs" in saved["files"]
    assert "frontend/scripts/generate-dashboard-contracts.mjs" in saved["files"]
    assert not any("node_modules" in name or name.endswith(".tmp") for name in saved["files"])
    assert "frontend/src/App.vue" in saved["files"]
    assert "frontend/src/main.ts" in saved["files"]
    assert "scripts/stage9_history_adjustment.py" in saved["files"]
    assert "src/ai_accounting/kernel/offline_history_adjustment.py" not in saved["files"]
    assert "src/ai_accounting/static/dashboard/assets/main.js" in saved["files"]
    assert "frontend/dist/index.html" in saved["files"]
    with pytest.raises(ValueError, match="must not exist"):
        snapshot_stage9_source.snapshot_source(source, target, workspace=workspace)


@pytest.mark.parametrize(
    "changed_file", ("src/ai_accounting/kernel/example.py", "frontend/src/App.vue",
                     "frontend/scripts/check-contract-samples.mjs")
)
def test_snapshot_never_publishes_manifest_when_source_changes(tmp_path, monkeypatch, changed_file):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = _source(tmp_path / "source")
    initial = (source / changed_file).read_text(encoding="utf-8")
    target = workspace / ".tmp/stage9-drifting-copy"
    original = snapshot_stage9_source._inventory
    source_reads = 0

    def drift(path):
        nonlocal source_reads
        if path == source:
            source_reads += 1
            if source_reads == 2:
                (source / changed_file).write_text("changed during copy\n", encoding="utf-8")
        return original(path)

    monkeypatch.setattr(snapshot_stage9_source, "_inventory", drift)
    with pytest.raises(RuntimeError, match="changed during copy"):
        snapshot_stage9_source.snapshot_source(source, target, workspace=workspace)
    assert not (target / "source-manifest.json").exists()
    audit = json.loads((target / "copy-audit.json").read_text(encoding="utf-8"))
    assert audit["status"] == "incomplete"
    copied = target / changed_file
    assert copied.read_text(encoding="utf-8") == initial


def test_snapshot_requires_formal_static_build_even_with_diagnostic_dist(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = _source(tmp_path / "source")
    (source / "src/ai_accounting/static/dashboard/index.html").unlink()
    target = workspace / ".tmp/stage9-missing-release"
    with pytest.raises(ValueError, match="built frontend"):
        snapshot_stage9_source.snapshot_source(source, target, workspace=workspace)
    assert not target.exists()


def test_snapshot_rejects_sample_tool_left_in_runtime_kernel(tmp_path):
    source = _source(tmp_path / "source")
    (source / "src/ai_accounting/kernel/offline_history_adjustment.py").write_text(
        "# obsolete runtime copy\n", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="belongs in scripts"):
        snapshot_stage9_source._selected(source)


def test_snapshot_without_diagnostic_dist_fixes_frontend_source_and_release(tmp_path):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = _source(tmp_path / "source", diagnostic_dist=False)
    target = workspace / ".tmp/stage9-release-only"
    result = snapshot_stage9_source.snapshot_source(source, target, workspace=workspace)
    assert result["status"] == "complete"
    assert result["files"] == snapshot_stage9_source._inventory(source)
    assert result["files"] == snapshot_stage9_source._inventory(target)
    assert "frontend/src/App.vue" in result["files"]
    assert "src/ai_accounting/static/dashboard/index.html" in result["files"]
    assert not (target / "frontend/dist").exists()


@pytest.mark.parametrize(
    "helper", ["check-contract-samples.mjs", "generate-dashboard-contracts.mjs"]
)
def test_snapshot_requires_its_contract_helpers(tmp_path, helper):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    source = _source(tmp_path / "source")
    (source / "frontend/scripts" / helper).unlink()
    target = workspace / ".tmp/stage9-missing-helper"
    with pytest.raises(ValueError, match="missing code"):
        snapshot_stage9_source.snapshot_source(source, target, workspace=workspace)
    assert not target.exists()
