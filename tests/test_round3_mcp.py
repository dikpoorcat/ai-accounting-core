from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client


def _stdio_environment() -> dict[str, str]:
    repository_root = Path(__file__).parents[1]
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    environment = os.environ.copy()
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            [
                str(repository_root / "src"),
                str(site_packages),
                str(site_packages / "win32"),
                str(site_packages / "win32" / "lib"),
                str(site_packages / "pywin32_system32"),
                environment.get("PYTHONPATH"),
            ],
        )
    )
    return environment


def test_r3_012_stdio_prevalidation_errors_expose_paths_but_not_input_values() -> None:
    sentinel = "SECRET-123456 postgresql://user:password@host/db 6222020202020202"

    async def run() -> None:
        parameters = StdioServerParameters(
            command=getattr(sys, "_base_executable", sys.executable),
            args=["-m", "ai_accounting.mcp_server"],
            cwd=Path(__file__).parents[1],
            env=_stdio_environment(),
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                extra = await client.call_tool(
                    "finance_preview_payroll",
                    {
                        "request": {"org_id": "00000000-0000-0000-0000-000000000000"},
                        "unexpected": sentinel,
                    },
                )
                assert extra.isError is True
                extra_text = extra.content[0].text
                assert "VALIDATION_ERROR" in extra_text
                assert "unexpected" in extra_text
                assert sentinel not in extra_text
                assert "input_value" not in extra_text

                nested = await client.call_tool(
                    "finance_record_event",
                    {
                        "request": {
                            "org_id": "00000000-0000-0000-0000-000000000000",
                            "idempotency_key": "r3-invalid-sensitive-request",
                            "event_type": "expense_cash",
                            "business_dates": {
                                "business_date": "2026-09-01",
                                "posting_date": "2026-09-01",
                            },
                            "amounts": {"amount_fen": sentinel},
                        }
                    },
                )
                assert nested.isError is True
                nested_text = nested.content[0].text
                assert "VALIDATION_ERROR" in nested_text
                assert "request.amounts.amount_fen" in nested_text
                assert sentinel not in nested_text
                assert "input_value" not in nested_text

                policy = await client.call_tool(
                    "finance_register_payroll_policy_version",
                    {
                        "request": {
                            "org_id": "00000000-0000-0000-0000-000000000000",
                            "region": "R3",
                            "effective_from": "2026-01-01",
                            "version": "r3-invalid-policy",
                            "source_url": "https://www.chinatax.gov.cn/",
                            "parameters": {
                                "income_tax": {"version": sentinel},
                            },
                        }
                    },
                )
                assert policy.isError is True
                policy_text = policy.content[0].text
                assert "VALIDATION_ERROR" in policy_text
                assert "request.parameters" in policy_text
                assert sentinel not in policy_text
                assert "input_value" not in policy_text

    asyncio.run(run())
