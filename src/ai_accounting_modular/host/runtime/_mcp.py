"""Stdio MCP is a thin client of the same resident used by CLI and the browser."""

import asyncio
import json
from typing import Any

from mcp.server.fastmcp import FastMCP
from pydantic import StrictInt

from ai_accounting_modular.agent.public import (
    INSTRUCTIONS,
    generate_ids,
    guide_index,
    offline_contracts,
    read_guide,
    read_work_checklist,
    read_work_stage,
)
from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.host.public import create_offline_application

from .public import Runtime


def _guides(mcp, *, offline=False):
    @mcp.resource("accounting://guides", mime_type="application/json")
    def guides() -> str:
        """按需说明目录；文档不代表当前实例支持的业务种类。"""
        directory = guide_index()
        if offline:
            directory["topics"] = [
                item for item in directory["topics"] if item["topic"] == "runtime"
            ]
            directory["message"] = "离线实例只处理明确的恢复／重建目标；参阅运行说明。"
            for key in ("checklist_uri", "stage_uri_template", "corrections_uri"):
                directory.pop(key)
        return json.dumps(directory, ensure_ascii=False)

    @mcp.resource("accounting://guides/{topic}", mime_type="text/markdown")
    def guide(topic: str) -> str:
        """读取明确静态主题，拒绝任意文件路径。"""
        if offline and topic != "runtime":
            raise ContractError("unsupported_guide", "离线实例只提供恢复／重建运行说明。")
        return read_guide(topic)

    if not offline:

        @mcp.resource("accounting://guides/work/checklist", mime_type="application/json")
        def work_checklist() -> str:
            """公共核对条件及阶段目录；按阶段读取完整条件，不含公司进度。"""
            return json.dumps(read_work_checklist(), ensure_ascii=False)

        @mcp.resource("accounting://guides/work/stages/{stage}", mime_type="application/json")
        def work_stage(stage: str) -> str:
            """读取1至8中的一项内部条件、依据和办理动作。"""
            return json.dumps(read_work_stage(stage), ensure_ascii=False)

        @mcp.resource("accounting://guides/business/{kind}", mime_type="text/markdown")
        def business_guide(kind: str) -> str:
            """读取指定完整 Fact kind 的原有业务规则；实际支持范围先查 accounting_schema。"""
            return read_guide("business", kind=kind)


def create_mcp(client):
    mcp = FastMCP("modular-local-accounting", instructions=INSTRUCTIONS)
    _guides(mcp)

    async def call(function, *arguments):
        try:
            return await asyncio.to_thread(function, *arguments)
        except ContractError as error:
            return error.to_response()
        except Exception as error:
            return Runtime.failure(error).to_response()

    @mcp.resource("accounting://contracts", mime_type="application/json")
    async def contracts() -> str:
        """当前命令和业务种类目录；完整输入按 accounting_schema 的 command 与 kinds 选取。"""
        return json.dumps(await call(client.contracts), ensure_ascii=False)

    @mcp.tool()
    async def accounting_schema(
        command: str | None = None, kinds: list[str] | None = None
    ) -> dict[str, Any]:
        """无参数读实际目录；command读完整命令合同。Apply须明确kinds，纯说明或撤回可用[]。

        kinds同时限定fact与known_fact；需要的种类均须列入。仅kinds沿用单项事实合同查询。
        其他命令不传kinds。合同发现不读取公司或替代实际命令的完整校验。
        """
        request = {}
        if command is not None:
            request["command"] = command
        if kinds is not None:
            request["kinds"] = kinds
        return await call(client.dispatch, "accounting_schema", request)

    @mcp.tool()
    async def accounting_new_ids(count: StrictInt = 1) -> dict[str, Any]:
        """生成1至1001个标准UUID，不创建或预留业务，也不保存未提交工作。

        新建时分配，调用方在原完整请求上下文中保存；更正和重试沿用原编号。
        生成回复不明可重新生成，提交回复不明须查原业务，不能换编号再记。
        """
        return {"ids": generate_ids(count)}

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
            "按需运行说明见 accounting://guides/runtime。"
        ),
    )
    _guides(mcp, offline=True)

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
