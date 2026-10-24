"""The same narrow payroll query is usable through actual CLI and generic MCP transport."""

import asyncio
import json
import subprocess
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from tests.modular.domains.people.test_application import request
from tests.modular.domains.people.test_people import no_contributions
from tests.modular.domains.people.test_read_remuneration_tax import tax_target, unpaid_wage

from .conftest import native_login


def test_cli_and_mcp_expose_the_same_kernel_payroll_capacity(local, tmp_path):
    runtime, company, _ = local
    wage = unpaid_wage()
    tax = tax_target(wage, "10000")
    saved = runtime.kernel.application.accounting_apply(
        request(company.company_id, no_contributions(), wage, tax)
    )
    assert saved["completion"] == "complete", saved
    runtime.start()
    native_login(runtime)
    payload = {
        "company_id": company.company_id,
        "period": "2026-02",
        "business_ids": [wage["business_id"]],
    }
    path = tmp_path / "remuneration.json"
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
        [*command, "call", "accounting-remuneration", "--input", str(path)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    cli_value = json.loads(process.stdout)
    assert cli_value["items"][0]["payable_fen"] == "90000"
    assert cli_value["items"][0]["tax_targets"][0]["business_id"] == tax["business_id"]

    async def check_mcp():
        params = StdioServerParameters(command=sys.executable, args=command[1:] + ["mcp"])
        async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            contracts = await session.read_resource("accounting://contracts")
            catalog = json.loads(contracts.contents[0].text)
            assert "accounting_remuneration" in {item["command"] for item in catalog["commands"]}
            result = await session.call_tool(
                "accounting_command", {"command": "accounting_remuneration", "payload": payload}
            )
            assert not result.isError
            assert result.structuredContent == cli_value

    asyncio.run(check_mcp())
