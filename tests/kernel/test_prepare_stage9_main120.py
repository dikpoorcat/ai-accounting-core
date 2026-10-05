"""Narrow fixture preparation validates manifests and refuses implicit targets."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
import prepare_stage9_main120 as tool


def _manifest(tree, files):
    tree.mkdir(exist_ok=True)
    for name, contents in files.items():
        path = tree / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(contents)
    inventory = {name: tool.original._sha(tree / name) for name in files}
    sha = tool._digest(inventory)
    (tree / "source-manifest.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "target": str(tree),
                "sha256": sha,
                "file_count": len(inventory),
                "files": inventory,
            }
        ),
        encoding="utf-8",
    )
    return sha, inventory


def test_historical_manifest_keeps_original_inventory_layout(tmp_path, monkeypatch):
    tree = tmp_path / "r22"
    sha, _ = _manifest(tree, {"src/old.py": b"original", "frontend/dist/old.js": b"built"})
    monkeypatch.setattr(tool, "_inventory", lambda _: pytest.fail("must not impose current layout"))
    assert tool.require_manifest(tree, expected=sha, historical=True) == sha
    (tree / "src/old.py").write_bytes(b"changed")
    with pytest.raises(ValueError, match="changed"):
        tool.require_manifest(tree, expected=sha, historical=True)


def test_current_manifest_requires_complete_current_inventory(tmp_path, monkeypatch):
    tree = tmp_path / "current"
    sha, inventory = _manifest(tree, {"src/current.py": b"current"})
    monkeypatch.setattr(tool, "_inventory", lambda _: inventory)
    assert tool.require_manifest(tree) == sha
    monkeypatch.setattr(
        tool, "_inventory", lambda _: {**inventory, "frontend/src/new.vue": "extra"}
    )
    with pytest.raises(ValueError, match="inventory"):
        tool.require_manifest(tree)


@pytest.mark.parametrize("damage", ["missing", "changed"])
def test_historical_manifest_rechecks_every_listed_file_at_completion(tmp_path, monkeypatch, damage):
    tree = tmp_path / "r73"
    sha, _ = _manifest(tree, {
        "src/old.py": b"original", "frontend/dist/old.js": b"old build",
    })
    monkeypatch.setattr(tool, "_inventory", lambda _: pytest.fail("historical layout is fixed"))
    assert tool.require_manifest(tree, historical=True) == sha
    built = tree / "frontend/dist/old.js"
    if damage == "missing":
        built.unlink()
    else:
        built.write_bytes(b"different build")
    with pytest.raises(ValueError, match="disappeared or changed"):
        tool.require_manifest(tree, expected=sha, historical=True)


def test_manifest_rejects_parent_escape_even_with_valid_inventory_digest(tmp_path):
    tree = tmp_path / "source"
    tree.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_bytes(b"outside")
    inventory = {"../outside.py": tool.original._sha(outside)}
    (tree / "source-manifest.json").write_text(
        json.dumps(
            {
                "status": "complete",
                "target": str(tree),
                "files": inventory,
                "sha256": tool._digest(inventory),
                "file_count": 1,
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="escaped"):
        tool.require_manifest(tree, historical=True)


def test_paths_are_fixed_main120_and_never_overwrite_output(tmp_path):
    temporary = tmp_path / ".tmp"
    temporary.mkdir()
    for name in (
        tool.CASE_NAME,
        tool.CASE["source_tree"],
        tool.CASE["report"],
        "stage9-final-source",
    ):
        (temporary / name).mkdir()
    args = argparse.Namespace(
        target_root=temporary / "stage9-main120-candidate-final",
        target_source=temporary / "stage9-final-source",
    )
    paths = tool.selected_paths(args, tmp_path)
    assert paths[0].name == "stage9-release-main-120"
    assert paths[1].name == "stage9-build-source-r22-release"
    paths[3].mkdir()
    marker = paths[3] / "preserve.txt"
    marker.write_bytes(b"original")
    with pytest.raises(ValueError, match="new main120"):
        tool.selected_paths(args, tmp_path)
    assert marker.read_bytes() == b"original"


def test_current_factory_sql_must_match_both_declared_contracts():
    from ai_accounting.kernel.catalog import catalog_sql
    from ai_accounting.kernel.schema import schema_sql
    from ai_accounting.kernel.schema_bundle import production_bundle
    from ai_accounting.kernel.versions import contract

    current = production_bundle()
    contracts = {
        "company": {"objects": contract(schema_sql(current.registry))},
        "catalog": {"objects": contract(catalog_sql())},
    }
    candidate = SimpleNamespace(
        status="released",
        current_versions={"company": 1, "catalog": 1},
        registry=current.registry,
        current=contracts.__getitem__,
    )
    tool.require_factory_sql(candidate)
    contracts["company"]["objects"] = []
    with pytest.raises(ValueError, match="factory SQL"):
        tool.require_factory_sql(candidate)
    candidate.status = "draft"
    with pytest.raises(ValueError, match="released/1"):
        tool.require_factory_sql(candidate)


def test_executor_helpers_must_match_fixed_candidate(tmp_path):
    scripts = tmp_path / "scripts"
    scripts.mkdir()
    for module in (tool.original, tool.reseed):
        helper = Path(module.__file__)
        (scripts / helper.name).write_bytes(helper.read_bytes())
    tool.require_copy_helpers(tmp_path)
    (scripts / "reseed_stage9_book.py").write_bytes(b"changed helper")
    with pytest.raises(ValueError, match="Copy helpers"):
        tool.require_copy_helpers(tmp_path)
