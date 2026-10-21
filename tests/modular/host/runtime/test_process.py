"""Actual separate resident startup and offline CLI do not borrow the old runtime."""

import json
import subprocess
import sys
import time
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.runtime import _state
from ai_accounting_modular.host.runtime._client import Client
from ai_accounting_modular.storage.public import CompanySpec, create_company_database

from .conftest import native_login


def test_autostart_and_stop_use_only_explicit_new_catalog_and_no_business_login(local):
    runtime, _, _ = local
    runtime.close()
    client = Client(runtime.catalog_path)
    try:
        result = client.dispatch("accounting_schema", {})
        assert "people.remuneration" in result["supported_kinds"]
        assert not client.security("session_status", {})["authenticated"]
        assert not runtime.catalog_path.with_name(".service.json").exists()
        assert "capability" not in client.info()
    finally:
        client.stop()
    deadline = time.monotonic() + 10
    while _state.state_path(runtime.catalog_path).exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    assert not _state.state_path(runtime.catalog_path).exists()


def test_offline_command_does_not_start_or_need_a_catalog(tmp_path):
    source = tmp_path / "missing-company.finance-company.zip"
    target = tmp_path / "new-target"
    request = tmp_path / "restore.json"
    request.write_text(
        json.dumps(
            {
                "action": "restore",
                "source_path": str(source),
                "new_target_directory": str(target),
                "mode": "check",
            }
        ),
        encoding="utf-8",
    )
    process = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-m",
            "ai_accounting_modular.host.runtime.cli",
            "restore",
            "--input",
            str(request),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 1
    result = json.loads(process.stdout)
    assert result["action"] == "restore"
    assert result["state"] == "failed"
    assert not target.exists()
    assert list(tmp_path.glob("*.modular-service")) == []


def test_explicit_company_creation_is_installation_only_and_preserves_existing_targets(local):
    runtime, _, _ = local
    runtime.start()
    native_login(runtime)
    client = Client(runtime.catalog_path)
    root = runtime.catalog_path.parent / "new-independent-company"
    payload = {
        "credit_code": "913100000000000002",
        "legal_name": "合成新增公司",
        "start_period": "2026-02",
        "company_root": str(root),
    }
    created = client.create_company(payload)
    assert created["legal_name"] == payload["legal_name"]
    assert created["start_period"] == "2026-02"
    assert (
        client.dispatch("accounting_context", {"company_id": created["company_id"]})["state"]
        == "selected"
    )
    assert "company_create" not in runtime.agent.commands
    with pytest.raises(ContractError) as refused:
        client.create_company(payload)
    assert refused.value.code == "target_exists"
    assert len(runtime.kernel.storage.list_companies()) == 2


def test_explicit_registration_retains_verified_independent_company_identity(local):
    runtime, original, _ = local
    root = runtime.catalog_path.parent / "isolated-new-system-target"
    target = create_company_database(
        CompanySpec(
            str(uuid4()),
            str(uuid4()),
            "913100000000000002",
            "合成独立接续公司",
            "2026-03",
            root,
        )
    )
    runtime.start()
    native_login(runtime)
    registered = Client(runtime.catalog_path).register_company({"company_root": str(root)})
    assert registered["company_id"] == target.company_id
    assert registered["database_id"] == target.database_id
    current = runtime.kernel.application.accounting_context({"company_id": original.company_id})
    assert current["database_id"] == original.database_id
    assert len(runtime.kernel.storage.list_companies()) == 2
