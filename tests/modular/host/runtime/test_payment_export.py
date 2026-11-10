"""CLI and real MCP use the same current file plan and resident background."""

import asyncio
import json
import subprocess
import sys
from hashlib import sha256
from pathlib import Path
from uuid import uuid4

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host import _desktop_export
from ai_accounting_modular.host.runtime._client import Client
from tests.modular.application._working_case import Case
from tests.modular.application.test_payment_export import prepare

from .conftest import native_login


def test_cli_generation_and_mcp_save_deliver_the_same_verified_workbook(
    local, tmp_path, monkeypatch
):
    runtime, company, _ = local
    desktop = tmp_path / "redirected-desktop"
    desktop.mkdir()
    monkeypatch.setattr(_desktop_export, "desktop_directory", lambda: desktop)
    case = Case(runtime.kernel, company.company_id, str(uuid4()), str(uuid4()))
    wage, source = prepare(case, shifted=True)
    before = case.get(wage["business_id"])["businesses"]
    runtime.start()
    native_login(runtime)
    payload = {
        "action": "generate",
        "company_id": case.company,
        "period": "2026-01",
        "template_source_id": source,
    }
    path = tmp_path / "payment-file-request.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    command = [
        sys.executable,
        "-I",
        "-X",
        "utf8",
        "-m",
        "ai_accounting_modular.host.runtime.cli",
        "--catalog",
        str(runtime.catalog_path),
    ]
    process = subprocess.run(
        [*command, "call", "accounting-payment-export", "--input", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    generated = json.loads(process.stdout)
    assert generated["state"] in {"pending", "running", "succeeded"}, generated

    async def check_mcp():
        params = StdioServerParameters(command=sys.executable, args=command[1:] + ["mcp"])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            contracts = json.loads(
                (await session.read_resource("accounting://contracts")).contents[0].text
            )
            assert "accounting_payment_export" in contracts["commands"]
            checklist = json.loads(
                (await session.read_resource("accounting://guides/work/checklist")).contents[0].text
            )
            assert checklist["stages"]
            for _ in range(100):
                response = await session.call_tool(
                    "accounting_command",
                    {
                        "command": "accounting_payment_export",
                        "payload": {
                            "company_id": case.company,
                            "period": "2026-01",
                            "action": "status",
                        },
                    },
                )
                assert not response.isError
                result = response.structuredContent
                if result["state"] not in {"pending", "running"}:
                    break
                await asyncio.sleep(0.1)
            assert result["state"] == "succeeded", result
            assert result["task"]["task_id"] == generated["task"]["task_id"]
            assert Path(result["task"]["path"]).is_file()
            assert result["inspection"]["total_fen"] == "100000"
            if sys.platform != "win32":
                return
            payload = {
                "company_id": case.company,
                "action": "save",
                "task_id": result["task"]["task_id"],
                "destination": "desktop",
            }
            saved = None
            for state in ("copied", "reused"):
                response = await session.call_tool(
                    "accounting_command",
                    {"command": "accounting_payment_export", "payload": payload},
                )
                assert not response.isError
                receipt = response.structuredContent
                assert receipt["state"] == state
                if saved is not None:
                    assert receipt == saved | {"state": "reused"}
                saved = receipt
                assert saved["company_id"] == case.company
                assert saved["database_id"] == company.database_id
                assert saved["task_id"] == result["task"]["task_id"]
                output = Path(saved["path"])
                assert output.parent == desktop
                raw = output.read_bytes()
                assert len(raw) == saved["byte_size"] == result["task"]["byte_size"]
                assert sha256(raw).hexdigest() == saved["sha256"] == result["task"]["sha256"]
                assert raw == Path(result["task"]["path"]).read_bytes()
            rejected = await session.call_tool(
                "accounting_command",
                {
                    "command": "accounting_payment_export",
                    "payload": payload | {"task_id": str(uuid4())},
                },
            )
            failure = rejected.structuredContent
            assert failure["status"] == "rejected" and failure["code"] == "not_found"
            assert "fact_issues" not in failure
            assert list(desktop.iterdir()) == [output]

    asyncio.run(check_mcp())
    assert case.get(wage["business_id"])["businesses"] == before


def test_payment_save_requires_native_owner_login(local):
    runtime, company, _ = local
    runtime.start()
    client = Client(runtime.catalog_path, autostart=False)
    with pytest.raises(ContractError) as failed:
        client.dispatch(
            "accounting_payment_export",
            {
                "company_id": company.company_id,
                "action": "save",
                "task_id": str(uuid4()),
                "destination": "desktop",
            },
        )
    assert failed.value.code == "owner_session_required"
