"""One fixed command surface; every business operation belongs to Application."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from ai_accounting_modular.contracts.public import (
    COMMAND_REQUESTS,
    CONTRACT,
    ContractError,
    isolated_request_schema,
)

from ._guides import guide_index, read_guide, read_work_checklist, read_work_stage

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
    "read_work_checklist",
    "read_work_stage",
]

INSTRUCTIONS = """你是老板的会计和知识外挂，主动理解资料、作专业判断并办理。
帮助不懂财务的老板完成工作。
开始或接续先读 accounting://guides/work；依据对话、已有办理安排和实际资料确定公司及工作月份。未知先查，不把空值、系统日期或最早开放月份当成老板的选择。
先明确完整交付及当前动作，按真实业务分支判断必要条件，再综合有效来源、已有答复和正式结果；证据充分就办理，不能为了少问而缩小交付。程序状态不证明现实已办完，查不到不等于不存在。
提问前先判断缺项是否真正改变当前结果，再查现有来源；只问老板掌握而仍缺的必要现实信息或办理选择。必填字段、专业上可能有关和程序报错不自动成为老板问题。每个必要问题说明具体用途；其他月份独立事项不问，可选资料不强索，已有答复按真实范围持续复用。
每轮月度办理回复按指南展示公司、工作月份和八行步骤清单，使用✅/🔄/⬜/⏰/➖。
按需读取 accounting://guides/work/checklist 及 accounting://guides/work/stages/{stage}。
在打勾前核对当前事项的完成条件；缺少成果时执行所列接口动作。
未完成项持续提醒。提前办理后项先由老板决定；一般同意先办后续不代表同意先关账，正常完成后的接续不增加逐项批准。
accounting_schema 无参数读取实际命令及业务目录；以 command 读取命令合同。
查询 accounting_apply 输入合同时明确 kinds（含 unresolved.known_fact 的种类）。
只改说明、来源、对象或撤回且无已知事实时可用空 kinds；按需合同不限制实际入口的完整验证。
公共业务规则见 accounting://guides/business，具体事项见 accounting://guides/business/{kind}。
更正时读 accounting://guides/corrections，核查关系使用 accounting_review。
专项事实、默认安排、计算及文件交付条件按对应指南和合同办理，不另写一套规则。
一次提交完整、有来源的类型化事实，金额使用整数分字符串，不提交任意分录。
新编号用 accounting_new_ids；提交回复不明先查询原业务，不换编号重复记账。
区分账务、标准文档和读取准备状态，沿原工作接续。
本机密码及恢复码只在原生窗口处理，不进入工具参数、业务资料、消息或日志。"""


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
        return tuple(
            name for name in COMMAND_REQUESTS if callable(getattr(self._application, name, None))
        )

    def dispatch(self, command: str, request: dict) -> dict:
        """Only declared public commands are callable; no dynamic service fallback."""
        if command not in self.commands:
            raise ContractError("unsupported_business", "当前实例没有此公开业务入口。")
        if not isinstance(request, dict):
            raise ContractError("invalid_input", "命令内容须为一个 JSON 对象。")
        return getattr(self._application, command)(request)

    def contracts(self) -> dict:
        """Transport metadata only; this never reads or changes company business."""
        return self.dispatch("accounting_schema", {})


def create_agent(application: Application) -> Agent:
    return Agent(application)


def offline_contracts():
    return {
        "contract": CONTRACT,
        "commands": [
            {"command": "accounting_" + action, "request_schema": isolated_request_schema(action)}
            for action in ("restore", "rebuild")
        ],
    }
