"""Stdio MCP is a thin client of the same resident used by CLI and the browser."""

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP

from ai_accounting_modular.agent.public import INSTRUCTIONS, offline_contracts
from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.public import create_offline_application

from .public import Runtime


def create_mcp(client):
    mcp = FastMCP("modular-local-accounting", instructions=INSTRUCTIONS)

    async def call(function, *arguments):
        try:
            return await asyncio.to_thread(function, *arguments)
        except ContractError as error:
            return error.to_response()
        except Exception as error:
            return Runtime.failure(error).to_response()

    @mcp.resource("accounting://contracts", mime_type="application/json")
    async def contracts() -> str:
        """当前可调用的完整命令输入及自包含 Schema；业务类型先按 accounting_schema 选择。"""
        return json.dumps(await call(client.contracts), ensure_ascii=False)

    @mcp.tool()
    async def accounting_schema(kinds: list[str] | None = None) -> dict[str, Any]:
        """先读实际支持种类；选择 kinds 返回对应类型化事实合同。"""
        return await call(client.dispatch, "accounting_schema", {"kinds": kinds or []})

    @mcp.tool()
    async def accounting_command(command: str, payload: dict) -> dict[str, Any]:
        """调用 accounting_* 完整业务命令；不接受 SQL、自由分录或密码。"""
        return await call(client.dispatch, command, payload)

    @mcp.tool()
    async def accounting_security(operation: str, payload: dict) -> dict[str, Any]:
        """只请求、查询、取消本机窗口或查询登录；密码及恢复码只在窗口内输入。"""
        return await call(client.security, operation, payload)

    return mcp


def create_offline_mcp():
    """Offline tools construct no catalog, client, resident or background task."""
    application = create_offline_application()
    mcp = FastMCP(
        "modular-offline-accounting",
        instructions=(
            "仅处理显式选定的备份包或标准业务文档及新目标。先读 accounting://contracts。"
            "restore 恢复原账，rebuild 只用最终事实建立简化新账。"
            "check 不创建目标，run 不覆盖既有位置。"
            "这里没有当前公司、居民或日常记账；完成后须明确登记接入。"
        ),
    )

    @mcp.resource("accounting://contracts", mime_type="application/json")
    def contracts() -> str:
        return json.dumps(offline_contracts(), ensure_ascii=False)

    async def run(action, payload):
        try:
            if payload.get("action") != action:
                raise ContractError("invalid_input", "离线工具与请求 action 必须一致。")
            return await asyncio.to_thread(application.accounting_isolated, payload)
        except ContractError as error:
            return error.to_response()
        except Exception as error:
            return Runtime.failure(error).to_response()

    @mcp.tool()
    async def accounting_restore(payload: dict) -> dict[str, Any]:
        """同步检查或恢复明确备份到新目标；不连接原目录或创建后台任务。"""
        return await run("restore", payload)

    @mcp.tool()
    async def accounting_rebuild(payload: dict) -> dict[str, Any]:
        """同步用明确标准文档检查或建立简化新账；不从旧库补隐藏资料。"""
        return await run("rebuild", payload)

    return mcp
