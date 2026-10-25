"""Whole-month cancellation uses the same declared command through CLI and MCP."""

import asyncio
import json
import subprocess
import sys
from uuid import uuid4

from jsonschema import Draft202012Validator
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .conftest import business_request, native_login


def test_cli_inspects_and_mcp_cancels_the_same_complete_month(local, tmp_path):
    runtime, company, _ = local
    submitted = business_request(company.company_id)
    assert runtime.kernel.application.accounting_apply(submitted)["completion"] == "complete"
    original_ids = [row["fact"]["business_id"] for row in submitted["mutations"]]
    runtime.start()
    native_login(runtime)
    inspect = {"company_id": company.company_id, "period": "2026-01", "action": "inspect"}
    payload = tmp_path / "inspect.json"
    payload.write_text(json.dumps(inspect), encoding="utf-8")
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
        [*command, "call", "accounting-cancel-month", "--input", str(payload)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    preview = json.loads(process.stdout)
    assert preview["state"] == "inspected"
    assert set(preview["business_ids"]) == set(original_ids)
    assert preview["source_ids"] == [submitted["sources"][0]["source_id"]]
    assert preview["discarded_source_ids"] == preview["retained_source_ids"] == []
    assert preview["file_cleanup"] == {"state": "not_started", "issues": []}
    current = runtime.kernel.application.accounting_get(
        {"company_id": company.company_id, "business_ids": original_ids, "include": ["facts"]}
    )
    assert all(row["state"]["state"] == "active" for row in current["businesses"])

    async def cancel_through_mcp():
        params = StdioServerParameters(command=sys.executable, args=command[1:] + ["mcp"])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            contracts = json.loads(
                (await session.read_resource("accounting://contracts")).contents[0].text
            )
            schema = next(
                row["request_schema"]
                for row in contracts["commands"]
                if row["command"] == "accounting_cancel_month"
            )
            cancel = {
                **inspect,
                "action": "cancel",
                "command_id": str(uuid4()),
                "expected_head": preview["head"],
            }
            assert Draft202012Validator(schema).is_valid(cancel)
            result = await session.call_tool(
                "accounting_command", {"command": "accounting_cancel_month", "payload": cancel}
            )
            assert not result.isError
            return result.structuredContent

    result = asyncio.run(cancel_through_mcp())
    assert result["state"] == "cancelled" and result["documents"] == "ready"
    assert set(result["business_ids"]) == set(original_ids)
    assert result["discarded_source_ids"] == preview["source_ids"]
    assert result["retained_source_ids"] == []
    assert result["file_cleanup"] == {"state": "complete", "issues": []}
    final = runtime.kernel.application.accounting_get(
        {"company_id": company.company_id, "business_ids": original_ids, "include": ["facts"]}
    )
    assert all(
        row["state"]["state"] == "withdrawn" and "fact" not in row for row in final["businesses"]
    )
