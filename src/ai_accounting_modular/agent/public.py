"""One fixed command surface; every business operation belongs to Application."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from ai_accounting_modular.contracts.public import CONTRACT, ContractError

from ._contracts import REQUESTS, isolated_schema, request_schema
from ._guides import guide_index, read_guide

if TYPE_CHECKING:
    from ai_accounting_modular.application.public import Application

__all__ = [
    "Agent",
    "INSTRUCTIONS",
    "create_agent",
    "generate_ids",
    "guide_index",
    "offline_contracts",
    "read_guide",
]

INSTRUCTIONS = """你是老板的会计助手，帮助不懂财务的老板知道下一步做什么、提供什么资料。
开始或恢复记账先读 accounting://guides/work，按其中的月度办事清单引导老板；其他说明从
accounting://guides 按需选择。每轮回复交代公司、工作月份、本轮结果和八项简短进度，
最后只展开当前事项及老板需要提供的资料或实际办理的动作；等待资料或没有推进时也展示进度。
进度必须有实际依据，未核验明确标出，不把单笔入账当作阶段完成，不一次抛出整月问题。
用 accounting_schema 选择实际支持的 kind，
再读 accounting://guides/business/{kind} 的业务含义及所选输入 Schema。文档不代表实例已装配。
读取公司说明、当前业务和待办；已有资料应复用，不一次加载全部 Schema 或架构文档。
完整命令输入见只读资源 accounting://contracts，按其中的请求合同和所选业务合同提交。
新业务、对象及请求编号用 accounting_new_ids 生成，调用方把编号保存在原完整请求上下文。
这些编号不表示创建或预留，程序不自动保存未提交工作。
生成编号的回复不明可以重新生成；业务提交回复不明时必须按原业务编号查询，更正和重试不得另换编号重复记账。
一次提交完整且有来源的业务事实。金额使用整数分字符串，不提交自由分录，不手算未支持税额。
缺现实资料时先核查已有来源，再问老板具体问题；专业判断有疑问时继续核查，必要时请教顾问。
区分账务已保存、标准文档待完成和看板准备中。后两者由程序接续，不重复记账。
沿原业务身份更正，资料只保存最终正确事实。老板办理真实付款、申报等外部事项，没有审批步骤。
本机密码及恢复码只在原生窗口处理，不放进工具参数、业务资料、消息或日志。"""


def generate_ids(count: int = 1) -> list[str]:
    """Generate candidate UUIDs without creating, reserving or saving any business."""
    if type(count) is not int or not 1 <= count <= 1001:
        raise ContractError("invalid_input", "生成编号的数量须为1至1001的整数。")
    return [str(uuid4()) for _ in range(count)]


class Agent:
    def __init__(self, application: Application):
        self._application = application

    @property
    def commands(self) -> tuple[str, ...]:
        return tuple(name for name in REQUESTS if callable(getattr(self._application, name, None)))

    def dispatch(self, command: str, request: dict) -> dict:
        """Only declared public commands are callable; no dynamic service fallback."""
        if command not in self.commands:
            raise ContractError("unsupported_business", "当前实例没有此公开业务入口。")
        if not isinstance(request, dict):
            raise ContractError("invalid_input", "命令内容须为一个 JSON 对象。")
        return getattr(self._application, command)(request)

    def contracts(self) -> dict:
        """Transport metadata only; this never reads or changes company business."""
        return {
            "contract": CONTRACT,
            "instructions": INSTRUCTIONS,
            "commands": [
                {"command": name, "request_schema": request_schema(name)} for name in self.commands
            ],
        }


def create_agent(application: Application) -> Agent:
    return Agent(application)


def offline_contracts():
    return {
        "contract": CONTRACT,
        "commands": [
            {"command": "accounting_" + action, "request_schema": isolated_schema(action)}
            for action in ("restore", "rebuild")
        ],
    }
