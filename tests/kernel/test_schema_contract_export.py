"""Active contracts are checked read-only; draft writes stay isolated."""

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from ai_accounting.kernel.schema_bundle import production_bundle


def _exporter():
    script = Path(__file__).resolve().parents[2] / "scripts/export_kernel_schema_contracts.py"
    spec = importlib.util.spec_from_file_location("stage9_contract_export_test", script)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_active_contract_check_matches_and_does_not_write():
    repository = Path(__file__).resolve().parents[2]
    directory = repository / "src/ai_accounting/kernel/schema_contracts"
    before = {path: path.read_bytes() for path in directory.rglob("*.json")}
    result = subprocess.run(
        [sys.executable, "scripts/export_kernel_schema_contracts.py", "--check"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
    )
    label = "Draft" if production_bundle().status == "draft" else "Released"
    assert result.stdout.strip() == f"{label} contracts verified"
    assert {path: path.read_bytes() for path in directory.rglob("*.json")} == before


def test_draft_contract_check_failure_and_write_are_scoped(tmp_path, monkeypatch, capsys):
    exporter = _exporter()
    contracts = tmp_path / "src/ai_accounting/kernel/schema_contracts"
    monkeypatch.setattr(exporter, "STATUS", "draft")
    monkeypatch.setattr(exporter, "VERSION", 0)
    monkeypatch.setattr(
        exporter, "__file__", str(tmp_path / "scripts/export_kernel_schema_contracts.py")
    )
    monkeypatch.setattr(sys, "argv", ["export_kernel_schema_contracts.py", "--write"])
    exporter.main()
    assert capsys.readouterr().out.strip() == "Draft contracts written"

    company_draft = contracts / "company/draft.json"
    catalog_draft = contracts / "catalog/draft.json"
    released = contracts / "company/v1.json"
    released.write_text('{"synthetic": "released contract must stay untouched"}\n', "utf-8")
    expected_company = company_draft.read_bytes()
    expected_catalog = catalog_draft.read_bytes()
    expected_released = released.read_bytes()
    company_draft.write_text('{"tampered": true}\n', "utf-8")

    monkeypatch.setattr(sys, "argv", ["export_kernel_schema_contracts.py", "--check"])
    with pytest.raises(SystemExit) as failed_check:
        exporter.main()
    assert failed_check.value.code == 1
    assert capsys.readouterr().err.strip() == "Draft contracts differ: company"
    assert company_draft.read_text("utf-8") == '{"tampered": true}\n'
    assert catalog_draft.read_bytes() == expected_catalog
    assert released.read_bytes() == expected_released

    monkeypatch.setattr(sys, "argv", ["export_kernel_schema_contracts.py", "--write"])
    exporter.main()
    assert capsys.readouterr().out.strip() == "Draft contracts written"
    assert company_draft.read_bytes() == expected_company
    assert catalog_draft.read_bytes() == expected_catalog
    assert released.read_bytes() == expected_released

    before_final_check = {
        path.relative_to(contracts): path.read_bytes() for path in contracts.rglob("*.json")
    }
    monkeypatch.setattr(sys, "argv", ["export_kernel_schema_contracts.py", "--check"])
    exporter.main()
    assert capsys.readouterr().out.strip() == "Draft contracts verified"
    assert {
        path.relative_to(contracts): path.read_bytes() for path in contracts.rglob("*.json")
    } == before_final_check


def test_freeze_prepares_every_candidate_before_creating_any_target(tmp_path, monkeypatch):
    exporter = _exporter()
    directory = tmp_path / "src/ai_accounting/kernel/schema_contracts"
    monkeypatch.setattr(exporter, "STATUS", "released")
    monkeypatch.setattr(exporter, "VERSION", 1)
    monkeypatch.setattr(
        exporter, "__file__", str(tmp_path / "scripts/export_kernel_schema_contracts.py")
    )
    monkeypatch.setattr(exporter, "default_registry", object)
    monkeypatch.setattr(
        exporter, "schema_sql", lambda _registry: "CREATE TABLE company_probe(id INTEGER);"
    )
    monkeypatch.setattr(exporter, "catalog_sql", lambda: "CREATE TABLE catalog_probe(id INTEGER);")

    def failed_content(_registry):
        raise ValueError("synthetic content generation failure")

    monkeypatch.setattr(exporter, "content_contract", failed_content)
    monkeypatch.setattr(sys, "argv", ["export_kernel_schema_contracts.py", "--freeze-v1"])
    with pytest.raises(ValueError, match="synthetic content generation failure"):
        exporter.main()
    assert not directory.exists()


def test_draft_candidate_prints_exact_freeze_bodies_without_writing(tmp_path, monkeypatch, capsys):
    exporter = _exporter()
    directory = tmp_path / "src/ai_accounting/kernel/schema_contracts"
    registry = object()
    monkeypatch.setattr(exporter, "default_registry", lambda: registry)
    monkeypatch.setattr(
        exporter, "schema_sql", lambda _registry: "CREATE TABLE company_probe(id INTEGER);"
    )
    monkeypatch.setattr(exporter, "catalog_sql", lambda: "CREATE TABLE catalog_probe(id INTEGER);")
    monkeypatch.setattr(
        exporter,
        "content_contract",
        lambda _registry: {"status": "released", "version": 1, "descriptor": {"probe": 1}},
    )
    monkeypatch.setattr(
        exporter, "__file__", str(tmp_path / "scripts/export_kernel_schema_contracts.py")
    )
    expected = {
        path.relative_to(directory).as_posix(): body
        for path, body in exporter._release_outputs(directory, registry).items()
    }
    monkeypatch.setattr(sys, "argv", ["export_kernel_schema_contracts.py", "--candidate-v1"])
    exporter.main()
    assert json.loads(capsys.readouterr().out) == expected
    assert not directory.exists()


def test_freeze_exclusive_publication_preserves_existing_target(tmp_path):
    exporter = _exporter()
    directory = tmp_path / "schema_contracts"
    outputs = {
        directory / "company/v1.json": '{"company":true}\n',
        directory / "catalog/v1.json": '{"catalog":true}\n',
        directory / "content-v1.json": '{"content":true}\n',
    }
    existing = directory / "catalog/v1.json"
    existing.parent.mkdir(parents=True)
    existing.write_text("preserve me\n", encoding="utf-8")
    with pytest.raises(FileExistsError, match="released v1 already exists"):
        exporter._publish_release(outputs)
    assert existing.read_text("utf-8") == "preserve me\n"
    assert not (directory / "company/v1.json").exists()
    assert not (directory / "content-v1.json").exists()


def test_freeze_publication_rolls_back_only_its_own_files(tmp_path, monkeypatch):
    exporter = _exporter()
    directory = tmp_path / "schema_contracts"
    outputs = {
        directory / "company/v1.json": '{"company":true}\n',
        directory / "catalog/v1.json": '{"catalog":true}\n',
        directory / "content-v1.json": '{"content":true}\n',
    }
    original_open = Path.open

    def fail_last(self, mode="r", *args, **kwargs):
        if self == directory / "content-v1.json" and mode == "x":
            raise OSError("synthetic final publication failure")
        return original_open(self, mode, *args, **kwargs)

    with monkeypatch.context() as context:
        context.setattr(Path, "open", fail_last)
        with pytest.raises(OSError, match="synthetic final publication failure"):
            exporter._publish_release(outputs)
    assert all(not path.exists() for path in outputs)
    exporter._publish_release(outputs)
    assert {path: path.read_text("utf-8") for path in outputs} == outputs


def test_freeze_publication_removes_partially_written_final_contract(tmp_path, monkeypatch):
    exporter = _exporter()
    directory = tmp_path / "schema_contracts"
    outputs = {
        directory / "company/v1.json": '{"company":true}\n',
        directory / "catalog/v1.json": '{"catalog":true}\n',
        directory / "content-v1.json": '{"content":true}\n',
    }
    final = directory / "content-v1.json"
    original_open = Path.open

    class InterruptedWrite:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            self.handle.__enter__()
            return self

        def __exit__(self, *args):
            return self.handle.__exit__(*args)

        def write(self, value):
            self.handle.write(value[:5])
            raise OSError("synthetic partial contract write")

    def partial_last(self, mode="r", *args, **kwargs):
        handle = original_open(self, mode, *args, **kwargs)
        return InterruptedWrite(handle) if self == final and mode == "x" else handle

    with monkeypatch.context() as context:
        context.setattr(Path, "open", partial_last)
        with pytest.raises(OSError, match="synthetic partial contract write"):
            exporter._publish_release(outputs)
    assert all(not path.exists() for path in outputs)
    exporter._publish_release(outputs)
    assert {path: path.read_text("utf-8") for path in outputs} == outputs


def test_released_freeze_is_single_use_and_check_is_read_only(tmp_path, monkeypatch, capsys):
    exporter = _exporter()
    directory = tmp_path / "src/ai_accounting/kernel/schema_contracts"
    monkeypatch.setattr(exporter, "STATUS", "released")
    monkeypatch.setattr(exporter, "VERSION", 1)
    monkeypatch.setattr(
        exporter, "__file__", str(tmp_path / "scripts/export_kernel_schema_contracts.py")
    )
    monkeypatch.setattr(sys, "argv", ["export_kernel_schema_contracts.py", "--freeze-v1"])
    exporter.main()
    assert capsys.readouterr().out.strip() == "Released contracts created"
    targets = (
        directory / "company/v1.json",
        directory / "catalog/v1.json",
        directory / "content-v1.json",
    )
    frozen = {path: path.read_bytes() for path in targets}

    monkeypatch.setattr(sys, "argv", ["export_kernel_schema_contracts.py", "--check"])
    exporter.main()
    assert capsys.readouterr().out.strip() == "Released contracts verified"
    assert {path: path.read_bytes() for path in targets} == frozen

    monkeypatch.setattr(sys, "argv", ["export_kernel_schema_contracts.py", "--freeze-v1"])
    with pytest.raises(SystemExit) as repeated:
        exporter.main()
    assert repeated.value.code == 2
    assert "already exists" in capsys.readouterr().err
    assert {path: path.read_bytes() for path in targets} == frozen
