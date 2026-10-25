"""Actual CLI and MCP deliver the same bounded financial basis and typed errors."""

import asyncio
import json
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from .conftest import business_request, native_login


def test_review_round_trips_through_cli_and_mcp(local, tmp_path):
    runtime, company, _ = local
    saved = business_request(company.company_id)
    assert runtime.kernel.application.accounting_apply(saved)["completion"] == "complete"
    runtime.start()
    native_login(runtime)
    payload = {
        "company_id": company.company_id,
        "action": "inspect",
        "cutoff_period": "2026-01",
        "subject": {
            "type": "business",
            "business_id": saved["mutations"][1]["fact"]["business_id"],
        },
        "collection": "obligations",
    }
    path = tmp_path / "review.json"
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
        [*command, "call", "accounting-review", "--input", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    expected = json.loads(process.stdout)
    assert expected["items"][0]["amounts"]["remaining_fen"] == "100000"

    async def check():
        params = StdioServerParameters(command=sys.executable, args=command[1:] + ["mcp"])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            contracts = await session.read_resource("accounting://contracts")
            names = {item["command"] for item in json.loads(contracts.contents[0].text)["commands"]}
            assert "accounting_review" in names
            response = await session.call_tool(
                "accounting_command", {"command": "accounting_review", "payload": payload}
            )
            assert not response.isError
            assert response.structuredContent == expected
            changed = await session.call_tool(
                "accounting_command",
                {
                    "command": "accounting_review",
                    "payload": {**payload, "expected_version": "0" * 64},
                },
            )
            assert changed.structuredContent["code"] == "snapshot_changed"

    asyncio.run(check())
