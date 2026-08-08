from __future__ import annotations

import asyncio
import sys

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

from ai_accounting.mcp_server import mcp


def test_mcp_exposes_only_domain_tools() -> None:
    names = {tool.name for tool in asyncio.run(mcp.list_tools())}
    assert names == {
        "finance_get_profile",
        "finance_get_event_schema",
        "finance_register_evidence",
        "finance_import_bank_statement",
        "finance_query_context",
        "finance_record_event",
        "finance_calculate_tax_period",
        "finance_reverse_event",
        "finance_get_event",
    }
    assert not any("journal_line" in name or "sql" in name for name in names)


def test_stdio_server_initializes_and_lists_tools() -> None:
    async def run() -> set[str]:
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-m", "ai_accounting.mcp_server"],
        )
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                await session.initialize()
                response = await session.list_tools()
                return {tool.name for tool in response.tools}

    names = asyncio.run(run())
    assert "finance_record_event" in names
    assert "finance_get_event" in names
