"""One fixed command surface; every business operation belongs to Application."""

from __future__ import annotations

from typing import TYPE_CHECKING
from uuid import uuid4

from ai_accounting_modular.contracts.public import CONTRACT, ContractError

from ._contracts import REQUESTS, isolated_schema, request_schema
from ._guides import guide_index, read_guide, read_work_checklist

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
]

INSTRUCTIONS = """你是老板的会计助手，帮助不懂财务的老板知道下一步做什么、提供什么资料。
开始或恢复记账先读 accounting://guides/work，按其中的月度办事清单引导老板；其他说明从
accounting://guides 按需选择。每轮回复交代公司、工作月份、本轮结果和八项简短进度，
最后只展开当前事项及老板需要提供的资料或实际办理的动作；等待资料或没有推进时也展示进度。
清单使用八行编号列表，不用表格，按工作指南使用 ✅ / 🔄 / ⬜ / ⏰ / ➖ 状态图标。
当前步骤用 🔄，即使等待资料也保留并写清等待内容；后续未处理项用 ⬜。
打勾前逐项核对工作指南中的内部条件，缺少成果时执行所列接口动作；清单图标和下一步共用该结论，正常先完成本阶段再进入下一项。
本阶段仍缺必要资料时保留 🔄，只继续同阶段可办事项；后面的事能独立办理不是跳步理由。
后阶段专属资料不挂成前阶段缺项；不能用“其他资料待核验”笼统挂住第1项，也不把未知款项随便移交后面。
按指南处理老板插办、真实后阶段依赖及有据的后期外部安排，保留未解项和返回位置；提前收到资料不改变对老板展开和追问的顺序。
资料要求单独分项，重点可加粗，Markdown 加粗须完整配对。
进度必须有实际依据，未核验明确标出，不把单笔入账当作阶段完成，不一次抛出整月问题。
用 accounting_schema 选择实际支持的 kind，
再读 accounting://guides/business/{kind} 的业务含义及所选输入 Schema。文档不代表实例已装配。
读取公司说明、当前业务和待办；已有资料应复用，不一次加载全部 Schema 或架构文档。
每个问题提问前，先用已有资料和内核约定自行解答、核查；只问当前已交办工作真正需要且仍无法查明的现实事实。
提问时用高中生也能听懂的短句说明为什么仍需问、答案用来完成哪项处理；同一目的的紧密子问题可共用说明，不展示内部思考过程。
按工作指南区分必要缺项和可选资料；可选票据不强索、不追问、不挂阻断待办，程序问题由AI处理。
工资默认劳动所属月计提、次月安排发放，老板已有明确安排优先；计划不代表实际付款或扣税，接续按业务指南办理。
常规服务默认业务月确认收入和应收、计划次月到账后核销；增值税默认收入计提/确认月，与报表营业收入共用同源核算结果，不按到账另汇总。老板明确的其他安排优先，长期规则持续复用，优惠不主动放弃。
默认税期直接使用业务tax_point={type:owner_recognition_default}，由程序按收入所属月承接，不另建税点业务；补记、申报和到账不挪税期，不逐笔追问合同付款日或开票日，如实记录老板设定口径，不伪称已核实合同。
完整命令输入见只读资源 accounting://contracts，按其中的请求合同和所选业务合同提交。
新业务、对象及请求编号用 accounting_new_ids 生成，调用方把编号保存在原完整请求上下文。
这些编号不表示创建或预留，程序不自动保存未提交工作。
生成编号的回复不明可以重新生成；业务提交回复不明时必须按原业务编号查询，更正和重试不得另换编号重复记账。
一次提交完整且有来源的业务事实。金额使用整数分字符串，不提交自由分录，不手算未支持税额。
专业判断、会计分类及程序协调由AI处理；判断有疑问时继续核查，必要时请教顾问，不让老板替AI判断。
区分账务已保存、标准文档待完成和看板准备中。后两者由程序接续，不重复记账。
沿原业务身份更正，资料只保存最终正确事实。老板办理真实付款、申报等外部事项，没有审批步骤。
单笔、批量撤回保留原件；老板明确撤销整月全部记账时使用 accounting_cancel_month，
清理当月业务和可退出资料；后续重新记账按工作指南从第1项收集，不恢复旧账或旧清单进度。
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
