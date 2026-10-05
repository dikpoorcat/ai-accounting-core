"""Runtime packaging carries only the contracts its installed bundle can use."""

import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest

from ai_accounting.kernel import build
from ai_accounting.kernel.contract_files import production_contract_file_names


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


@pytest.mark.parametrize("status", ["draft", "released"])
def test_package_launchers_instructions_and_self_check_use_active_data_root(tmp_path, status):
    packager = _script("package_local_kernel.py")
    verifier = _script("verify_local_package.py")
    bundle = _bundle(status)
    packager.write_launchers(tmp_path, bundle)
    directory = "kernel-" + status
    cmd = (tmp_path / "finance-local.cmd").read_text(encoding="ascii")
    powershell = (tmp_path / "finance-local.ps1").read_text(encoding="utf-8")
    instructions = (tmp_path / "使用说明.txt").read_text(encoding="utf-8")
    assert (
        f'if not defined FINANCE_DATA_ROOT set "FINANCE_DATA_ROOT=%~dp0data\\{directory}"'
        in cmd
    )
    assert 'if (-not $env:FINANCE_DATA_ROOT)' in powershell
    assert f'Join-Path $PSScriptRoot "data/{directory}"' in powershell
    assert "-m ai_accounting.kernel.cli %*" in cmd
    assert "-m ai_accounting.kernel.cli @args" in powershell
    assert f"默认资料目录：包内 data\\{directory}。" in instructions
    assert "finance-local.cmd --root D:\\会计资料 serve" in instructions
    assert verifier.package_default_root(tmp_path, bundle) == tmp_path / "data" / directory
    assert not (tmp_path / "data").exists()  # Preparing software never opens a business root.


def test_package_launchers_reject_unknown_bundle_without_creating_files(tmp_path):
    packager = _script("package_local_kernel.py")
    verifier = _script("verify_local_package.py")
    bundle = _bundle("unknown")
    with pytest.raises(ValueError, match="status"):
        packager.write_launchers(tmp_path, bundle)
    with pytest.raises(ValueError, match="status"):
        verifier.package_default_root(tmp_path, bundle)
    assert list(tmp_path.iterdir()) == []


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
    monkeypatch.setattr("ai_accounting.kernel.schema_bundle.STATUS", "released")
    monkeypatch.setattr("ai_accounting.kernel.schema_bundle.VERSION", 1)
    for name in production_contract_file_names():
        path = package / "kernel/schema_contracts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    content.write_text('{"version":1}', encoding="utf-8")
    monkeypatch.setattr(build, "__file__", str(package / "kernel/build.py"))
    original = build.calculator_build_id()
    content.write_text('{"version":1,"changed":true}', encoding="utf-8")
    assert build.calculator_build_id() != original


def test_declared_development_sources_enter_package_and_verifier_inventory(tmp_path):
    bundle = _bundle("draft")
    bundle.development_contracts = {"company": {"a" * 64: {}}}
    names = {
        "company/draft.json",
        "catalog/draft.json",
        "development/company/" + "a" * 64 + ".json",
    }
    for name in names:
        path = tmp_path / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    packager, verifier = _script("package_local_kernel.py"), _script("verify_local_package.py")
    assert {
        path.relative_to(tmp_path).as_posix()
        for path in packager.selected_contract_files(bundle, tmp_path)
    } == names
    assert set(verifier.expected_contract_names(bundle)) == names


def test_source_and_selected_runtime_have_same_build_identity(tmp_path, monkeypatch):
    import shutil

    monkeypatch.setattr("ai_accounting.kernel.schema_bundle.STATUS", "released")
    monkeypatch.setattr("ai_accounting.kernel.schema_bundle.VERSION", 2)
    source, runtime = tmp_path / "source", tmp_path / "runtime"
    for name in (
        "kernel/build.py",
        "kernel/other.py",
        "payroll/source.py",
        *build.SHARED_SOURCE_FILES,
    ):
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("# effective source\n", encoding="utf-8")
    for name in production_contract_file_names():
        path = source / "kernel/schema_contracts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("{}", encoding="utf-8")
    shutil.copytree(source, runtime)
    monkeypatch.setattr(build, "__file__", str(source / "kernel/build.py"))
    original = build.calculator_build_id()
    for name in ("company/draft.json", "development/company/unregistered.json", "future.json"):
        path = source / "kernel/schema_contracts" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("invalid unused JSON", encoding="utf-8")
    assert build.calculator_build_id() == original
    monkeypatch.setattr(build, "__file__", str(runtime / "kernel/build.py"))
    assert build.calculator_build_id() == original
    for name in (
        "kernel/other.py",
        "payroll/source.py",
        build.SHARED_SOURCE_FILES[0],
        "kernel/schema_contracts/company/v1.json",
        "kernel/schema_contracts/company/v2.json",
        "kernel/schema_contracts/content-v1.json",
    ):
        path = runtime / name
        saved = path.read_bytes()
        path.write_bytes(saved + b" ")
        assert build.calculator_build_id() != original
        path.write_bytes(saved)
    historical_contract = runtime / "kernel/schema_contracts/company/v1.json"
    historical_contract.unlink()
    assert build.calculator_build_id() != original


@pytest.mark.parametrize(
    "runtime_build,source_after", [("different", "source"), ("source", "changed")]
)
def test_manifest_rejects_runtime_mismatch_or_source_changed_during_copy(
    tmp_path, monkeypatch, runtime_build, source_after
):
    import json

    packager = _script("package_local_kernel.py")
    monkeypatch.setattr(build, "calculator_build_id", lambda: source_after)
    monkeypatch.setattr(
        packager.subprocess,
        "run",
        lambda *args, **kwargs: SimpleNamespace(
            stdout=json.dumps(
                {
                    "python": "3.12.13",
                    "sqlite": "3.53.1",
                    "isolated": 1,
                    "build_id": runtime_build,
                }
            )
        ),
    )
    with pytest.raises(ValueError, match="differs from its source build"):
        packager.bundle_manifest(tmp_path, {}, [], source_build_id="source")


def test_first_release_export_bootstraps_but_missing_installed_contract_is_rejected(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    repository = Path(__file__).resolve().parents[2]
    package = tmp_path / "src/ai_accounting"
    shutil.copytree(
        repository / "src/ai_accounting",
        package,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    bundle_path = package / "kernel/schema_bundle.py"
    text = bundle_path.read_text("utf-8").replace('STATUS = "draft"', 'STATUS = "released"')
    bundle_path.write_text(text.replace("VERSION = 0", "VERSION = 1"), encoding="utf-8")
    contracts = package / "kernel/schema_contracts"
    for name in ("company/v1.json", "catalog/v1.json", "content-v1.json"):
        (contracts / name).unlink(missing_ok=True)
    script = tmp_path / "scripts/export_kernel_schema_contracts.py"
    script.parent.mkdir()
    shutil.copyfile(repository / "scripts/export_kernel_schema_contracts.py", script)
    environment = dict(os.environ, PYTHONPATH=str(tmp_path / "src"))

    def run(*arguments):
        return subprocess.run(
            [sys.executable, "-X", "utf8", *arguments],
            cwd=tmp_path,
            env=environment,
            text=True,
            capture_output=True,
            encoding="utf-8",
        )

    candidate = run(str(script), "--candidate-v1")
    assert candidate.returncode == 0, candidate.stderr
    assert not (contracts / "content-v1.json").exists()
    frozen = run(str(script), "--freeze-v1")
    assert frozen.returncode == 0, frozen.stderr
    checked = run(str(script), "--check")
    assert checked.returncode == 0, checked.stderr
    identity = (
        "from ai_accounting.kernel.build import calculator_build_id; print(calculator_build_id())"
    )
    original = run("-c", identity)
    assert original.returncode == 0, original.stderr
    installed = (
        "from ai_accounting.kernel.schema_bundle import production_bundle; production_bundle()"
    )
    assert run("-c", installed).returncode == 0
    (contracts / "catalog/v1.json").unlink()
    changed = run("-c", identity)
    assert changed.returncode == 0 and changed.stdout != original.stdout
    assert run("-c", installed).returncode != 0
