"""A repaired observer cannot replace independently verified runtime content."""

import hashlib

import pytest

from scripts import snapshot_stage9_source
from scripts.benchmark_stage9_browser import verified_browser_harness
from tests.kernel.test_stage9_source_snapshot import _source


def _pair(tmp_path, change):
    workspace = tmp_path / "workspace"
    (workspace / ".tmp").mkdir(parents=True)
    working = _source(tmp_path / "working")
    # Include the real generated-contract location in the signed inventory.
    contract = working / "frontend/src/api/generated/dashboardResponseSchemas.json"
    contract.parent.mkdir(parents=True)
    contract.write_text('{"assets":{"schema_version":10}}', encoding="utf-8")
    original = workspace / ".tmp/stage9-original"
    snapshot_stage9_source.snapshot_source(working, original, workspace=workspace)
    target = working / change
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("changed\n", encoding="utf-8")
    repaired = workspace / ".tmp/stage9-repaired"
    snapshot_stage9_source.snapshot_source(working, repaired, workspace=workspace)
    book = {
        "qualification": {
            "source_manifest_sha256": hashlib.sha256(
                (original / "source-manifest.json").read_bytes()
            ).hexdigest(),
        },
    }
    return original, repaired, book


def test_repaired_browser_observer_preserves_qualified_runtime(tmp_path):
    original, repaired, book = _pair(tmp_path, "frontend/tests/browser-stage9-hot-refresh.cjs")
    path, proof = verified_browser_harness(original, repaired, book)
    assert path == repaired / "frontend/tests/browser-stage9-hot-refresh.cjs"
    assert proof["status"] == "qualified_runtime_bytes_unchanged"
    assert proof["changed_tool_files"] == ["frontend/tests/browser-stage9-hot-refresh.cjs"]
    assert proof["harness_sha256"] == hashlib.sha256(path.read_bytes()).hexdigest()
    book["qualification"]["source_manifest_sha256"] = "0" * 64
    with pytest.raises(ValueError, match="exact independent qualification"):
        verified_browser_harness(original, repaired, book)


@pytest.mark.parametrize("change", (
    "src/ai_accounting/kernel/example.py",
    "src/ai_accounting/schema_contracts/company/draft.json",
    "src/ai_accounting/static/dashboard/assets/main.js",
    "frontend/src/App.vue",
    "frontend/src/api/generated/dashboardResponseSchemas.json",
    "frontend/package-lock.json",
    "tests/kernel/stage9_book.py",
    "scripts/benchmark_stage9.py",
))
def test_browser_repair_rejects_changed_business_contract_assets_or_fixture(tmp_path, change):
    original, repaired, book = _pair(tmp_path, change)
    with pytest.raises(ValueError, match="qualified runtime or fixture bytes"):
        verified_browser_harness(original, repaired, book)


def test_browser_repair_rejects_tampering_after_sealing(tmp_path):
    original, repaired, book = _pair(tmp_path, "frontend/tests/browser-stage9-hot-refresh.cjs")
    (repaired / "frontend/tests/browser-stage9-hot-refresh.cjs").write_text(
        "// unrecorded observer", encoding="utf-8",
    )
    with pytest.raises(ValueError, match="complete fixed inventory"):
        verified_browser_harness(original, repaired, book)
