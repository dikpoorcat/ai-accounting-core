"""The checked-in draft contracts are generated exactly and checked read-only."""

import os
import shutil
import subprocess
import sys
from pathlib import Path


def test_draft_contract_check_matches_and_does_not_write():
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
    assert result.stdout.strip() == "Draft contracts verified"
    assert {path: path.read_bytes() for path in directory.rglob("*.json")} == before


def test_draft_contract_check_failure_and_write_are_scoped(tmp_path):
    source_repository = Path(__file__).resolve().parents[2]
    repository = tmp_path / "repository"
    script = repository / "scripts/export_kernel_schema_contracts.py"
    script.parent.mkdir(parents=True)
    shutil.copy2(source_repository / "scripts/export_kernel_schema_contracts.py", script)

    contracts = repository / "src/ai_accounting/kernel/schema_contracts"
    for kind in ("company", "catalog"):
        target = contracts / kind / "draft.json"
        target.parent.mkdir(parents=True)
        shutil.copy2(
            source_repository
            / "src/ai_accounting/kernel/schema_contracts"
            / kind
            / "draft.json",
            target,
        )

    company_draft = contracts / "company/draft.json"
    catalog_draft = contracts / "catalog/draft.json"
    released = contracts / "company/v1.json"
    released.write_text('{"synthetic": "released contract must stay untouched"}\n', "utf-8")
    expected_company = company_draft.read_bytes()
    expected_catalog = catalog_draft.read_bytes()
    expected_released = released.read_bytes()
    company_draft.write_text('{"tampered": true}\n', "utf-8")

    environment = os.environ.copy()
    python_path = str(source_repository / "src")
    if existing := environment.get("PYTHONPATH"):
        python_path = os.pathsep.join((python_path, existing))
    environment["PYTHONPATH"] = python_path

    failed_check = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=repository,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert failed_check.returncode == 1
    assert failed_check.stderr.strip() == "Draft contracts differ: company"
    assert company_draft.read_text("utf-8") == '{"tampered": true}\n'
    assert catalog_draft.read_bytes() == expected_catalog
    assert released.read_bytes() == expected_released

    written = subprocess.run(
        [sys.executable, str(script), "--write"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert written.stdout.strip() == "Draft contracts written"
    assert company_draft.read_bytes() == expected_company
    assert catalog_draft.read_bytes() == expected_catalog
    assert released.read_bytes() == expected_released

    before_final_check = {
        path.relative_to(contracts): path.read_bytes()
        for path in contracts.rglob("*.json")
    }
    verified = subprocess.run(
        [sys.executable, str(script), "--check"],
        cwd=repository,
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        env=environment,
    )
    assert verified.stdout.strip() == "Draft contracts verified"
    assert {
        path.relative_to(contracts): path.read_bytes()
        for path in contracts.rglob("*.json")
    } == before_final_check
