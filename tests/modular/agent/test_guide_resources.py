"""MCP serves whitelisted Markdown without changing the business tools or offline scope."""

import asyncio
import json
import sys
from pathlib import Path

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.server.fastmcp.exceptions import ResourceError

from ai_accounting_modular.agent.public import create_agent
from ai_accounting_modular.host.runtime._mcp import create_mcp, create_offline_mcp

ROOT = Path(__file__).resolve().parents[3]


def test_online_resources_are_on_demand_and_unknown_topics_or_kinds_are_rejected():
    async def check():
        server = create_mcp(object())  # Reading static text must not contact a company/service.
        resources = await server.list_resources()
        assert {str(item.uri) for item in resources} == {
            "accounting://contracts",
            "accounting://guides",
            "accounting://guides/work/checklist",
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
        work = list(await server.read_resource("accounting://guides/work"))[0].content
        assert "owner_statement" in work and "accounting_sources(action=discard)" in work
        checklist = list(await server.read_resource("accounting://guides/work/checklist"))[0]
        assert checklist.mime_type == "application/json"
        stages = json.loads(checklist.content)["stages"]
        assert len(stages) == 8 and stages[3]["id"] == "payment_export"
        assert "task.path的绝对文件路径" in stages[3]["checks"][3]["condition"]
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


def test_mcp_bundles_discard_input_without_executing_a_business_command():
    class SourcesApplication:
        def accounting_sources(self, request):
            raise AssertionError("Contract discovery must not call accounting_sources")

    async def check():
        server = create_mcp(create_agent(SourcesApplication()))
        commands = list(await server.read_resource("accounting://contracts"))[0].content
        sources = next(
            item["request_schema"]
            for item in json.loads(commands)["commands"]
            if item["command"] == "accounting_sources"
        )
        branch = next(
            item
            for item in sources["oneOf"]
            if item["properties"]["action"].get("const") == "discard"
        )
        assert branch["required"] == ["company_id", "action", "source_ids"]
        assert branch["properties"]["source_ids"]["maxItems"] == 500

    asyncio.run(check())


def test_mcp_declares_month_cancellation_without_company_reads_or_extra_tools():
    class MonthApplication:
        def accounting_cancel_month(self, request):
            raise AssertionError("Contract discovery must not cancel or read a month")

    async def check():
        server = create_mcp(create_agent(MonthApplication()))
        commands = json.loads(list(await server.read_resource("accounting://contracts"))[0].content)
        assert [row["command"] for row in commands["commands"]] == ["accounting_cancel_month"]
        schema = commands["commands"][0]["request_schema"]
        branches = {row["properties"]["action"]["const"]: row for row in schema["oneOf"]}
        assert set(branches) == {"inspect", "cancel"}
        assert branches["inspect"]["required"] == ["company_id", "action", "period"]
        assert branches["cancel"]["properties"]["expected_head"]["type"] == "integer"
        assert "command_id" in branches["cancel"]["required"]
        assert "expected_head" in branches["cancel"]["required"]
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
            args=[
                "-I",
                "-X",
                "utf8",
                "-c",
                "import sys, runpy; from pathlib import Path; "
                "sys.path.insert(0, sys.argv.pop(1)); "
                "import ai_accounting_modular; "
                "assert Path(ai_accounting_modular.__file__).is_relative_to(Path(sys.path[0])); "
                "runpy.run_module('ai_accounting_modular.host.runtime.cli', run_name='__main__')",
                str(ROOT / "src"),
                "mcp",
            ],
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
