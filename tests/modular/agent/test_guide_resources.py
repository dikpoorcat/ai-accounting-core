"""MCP serves whitelisted Markdown without changing the business tools or offline scope."""

import asyncio
import json
import sys

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ResourceError

from ai_accounting_modular.host.runtime._mcp import create_mcp, create_offline_mcp


def test_online_resources_are_on_demand_and_unknown_topics_or_kinds_are_rejected():
    async def check():
        server = create_mcp(object())  # Reading static text must not contact a company/service.
        resources = await server.list_resources()
        assert {str(item.uri) for item in resources} == {
            "accounting://contracts",
            "accounting://guides",
        }
        directory = list(await server.read_resource("accounting://guides"))[0]
        assert directory.mime_type == "application/json"
        assert len(json.loads(directory.content)["topics"]) == 5
        rules = list(
            await server.read_resource(
                "accounting://guides/business/investment.cooperation_contract"
            )
        )[0]
        assert rules.mime_type == "text/markdown"
        assert "经营者劳动不估价入账" in rules.content
        assert "opening.package" not in rules.content
        for uri in (
            "accounting://guides/unknown",
            "accounting://guides/business/operations.unknown",
            "accounting://guides/business/../../README.md",
        ):
            with pytest.raises((ResourceError, ValueError)):
                await server.read_resource(uri)
        assert {tool.name for tool in await server.list_tools()} == {
            "accounting_schema",
            "accounting_new_ids",
            "accounting_command",
            "accounting_security",
        }

    asyncio.run(check())


def test_offline_resources_include_only_relevant_runtime_guidance():
    async def check():
        server = create_offline_mcp()
        directory = json.loads(list(await server.read_resource("accounting://guides"))[0].content)
        assert [item["topic"] for item in directory["topics"]] == ["runtime"]
        text = list(await server.read_resource("accounting://guides/runtime"))[0].content
        assert "## 离线恢复和重建" in text
        for uri in ("accounting://guides/work", "accounting://guides/business/operations.expense"):
            with pytest.raises((ResourceError, ValueError)):
                await server.read_resource(uri)
        assert {tool.name for tool in await server.list_tools()} == {
            "accounting_restore",
            "accounting_rebuild",
        }
        assert {item.uriTemplate for item in await server.list_resource_templates()} == {
            "accounting://guides/{topic}"
        }

    asyncio.run(check())


def test_actual_offline_stdio_mcp_can_read_installed_runtime_guide(tmp_path):
    async def check():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=["-I", "-X", "utf8", "-m", "ai_accounting_modular.host.runtime.cli", "mcp"],
            cwd=str(tmp_path),
        )
        async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
            await session.initialize()
            result = await session.read_resource("accounting://guides/runtime")
            assert "company-register" in result.contents[0].text
            assert "## 离线恢复和重建" in result.contents[0].text
            assert {tool.name for tool in (await session.list_tools()).tools} == {
                "accounting_restore",
                "accounting_rebuild",
            }
        assert list(tmp_path.iterdir()) == []

    asyncio.run(check())
