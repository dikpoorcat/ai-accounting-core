"""Runtime packaging carries only the contracts its installed bundle can use."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from ai_accounting.kernel import build


def _script(name):
    path = Path(__file__).resolve().parents[2] / "scripts" / name
    spec = importlib.util.spec_from_file_location("stage9_" + path.stem, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle(status):
    contracts = {
        kind: {
            0: {"status": "draft"},
            1: {"status": "released"},
            2: {"status": "released"},
        }
        for kind in ("company", "catalog")
    }
    return SimpleNamespace(
        status=status,
        current_versions={"company": 0, "catalog": 0},
        contracts=contracts,
    )


def test_development_package_selects_only_active_draft(tmp_path):
    packager = _script("package_local_kernel.py")
    verifier = _script("verify_local_package.py")
    for kind in ("company", "catalog"):
        for name in ("draft.json", "v1.json", "v2.json"):
            path = tmp_path / kind / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
    (tmp_path / "content-v1.json").write_text("{}", encoding="utf-8")
    selected = packager.selected_contract_files(_bundle("draft"), tmp_path)
    assert {path.relative_to(tmp_path).as_posix() for path in selected} == {
        "company/draft.json",
        "catalog/draft.json",
    }
    assert set(verifier.expected_contract_names(_bundle("draft"))) == {
        "company/draft.json",
        "catalog/draft.json",
    }


def test_released_package_keeps_declared_history_and_content_without_draft(tmp_path):
    packager = _script("package_local_kernel.py")
    verifier = _script("verify_local_package.py")
    for kind in ("company", "catalog"):
        for name in ("draft.json", "v1.json", "v2.json"):
            path = tmp_path / kind / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("{}", encoding="utf-8")
    (tmp_path / "content-v1.json").write_text("{}", encoding="utf-8")
    selected = {
        path.relative_to(tmp_path).as_posix()
        for path in packager.selected_contract_files(_bundle("released"), tmp_path)
    }
    assert selected == set(verifier.expected_contract_names(_bundle("released")))
    assert selected == {
        "company/v1.json",
        "company/v2.json",
        "catalog/v1.json",
        "catalog/v2.json",
        "content-v1.json",
    }
    (tmp_path / "content-v1.json").unlink()
    try:
        packager.selected_contract_files(_bundle("released"), tmp_path)
    except ValueError as error:
        assert "Required runtime contract is missing" in str(error)
    else:
        raise AssertionError("released package accepted a missing v1 content contract")


def test_build_identity_includes_the_selected_content_contract(tmp_path, monkeypatch):
    package = tmp_path / "ai_accounting"
    (package / "kernel/schema_contracts").mkdir(parents=True)
    (package / "payroll").mkdir()
    (package / "kernel/build.py").write_text("# synthetic source\n", encoding="utf-8")
    for name in build.SHARED_SOURCE_FILES:
        (package / name).write_text("# synthetic source\n", encoding="utf-8")
    content = package / "kernel/schema_contracts/content-v1.json"
    content.write_text('{"version":1}', encoding="utf-8")
    monkeypatch.setattr(build, "__file__", str(package / "kernel/build.py"))
    original = build.calculator_build_id()
    content.write_text('{"version":1,"changed":true}', encoding="utf-8")
    assert build.calculator_build_id() != original
