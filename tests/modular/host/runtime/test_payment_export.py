"""CLI and real MCP use the same current file plan and resident background."""

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from uuid import uuid4

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.modular.application._working_case import Case
from tests.modular.application.test_payment_export import prepare

from .conftest import native_login


def test_cli_generation_and_mcp_status_deliver_the_same_verified_workbook(local, tmp_path):
    runtime, company, _ = local
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
            assert "accounting_payment_export" in {
                item["command"] for item in contracts["commands"]
            }
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

    asyncio.run(check_mcp())
    assert case.get(wage["business_id"])["businesses"] == before
