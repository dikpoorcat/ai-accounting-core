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

INSTRUCTIONS = """你是老板的会计和知识外挂，主动查清业务、作专业判断并办理。
帮助不懂财务的老板完成工作，只有真正需要的现实信息才让老板补充。
开始或接续先读accounting://guides/work，取得公司、月份、有效说明和当前待办；其他指南按需读取。
先明确本次办理什么、必需事实、可复用来源、成功结果、后续另行办理什么，按所选kind/method的现有合同判断。
先综合当前对话、已读原件、有效说明、相关正式业务和未清义务；有具体缺口才定向查询，不为证明查过而重读。
多份来源可以共同支持专业判断，不要求一张原件逐字写明结论，也不把AI判断伪装成老板确认。
必要事实已有充分依据、相关范围完整且无影响本次结果的未解冲突，就结束核查并继续办理。
未查询、未综合、来源冲突先由AI处理；只有核查后仍缺的必要现实事实才问老板，程序问题不能变成老板任务。
问题用高中生能听懂的短句说明现有依据缺在哪、答案用于完成什么；提问工具里也写同样说明。
不列所有可能事项的否定问卷，可选票据不强索、不反复催补、不挂阻断待办，不展示内部思考或新增查阅证明。
老板明确按已知方案并说明变化时，核查基准后直接采用、简短反馈；答复跨阶段复用，不重复确认。
需要核对义务、清偿、已登记资金或原行关联时，用accounting_review按身份或相关范围核查；候选不冒充确定关系。
原行未匹配不等于付款未记，已结义务也可核查，不只凭同额、上月金额或未结余额判断。
每轮月度办理回复交代公司、工作月份、本轮结果和八项进度；使用八行编号列表，不用表格，按指南用✅/🔄/⬜/⏰/➖。
当前步骤用🔄，即使等待资料也写清；未知明确标出，不把单笔入账、空查询或文件生成当整阶段完成。
打勾前逐项核对工作指南中的内部条件，缺少成果时执行所列接口动作；打勾和下一步共用这次判断，真实缺项仍在不擅自跳步。
只向老板展开当前事项；查清它所需的跨月、跨领域读取不受显示阶段限制，也不等于另推进一项。
后阶段专属资料不挂成前阶段缺项，老板插办、真实依赖和有据后期安排按指南接续，保留未解项和返回位置。
最后只说明老板眼下确需补充或办理的事，多项分列、加粗完整配对；条件齐备自行继续，不逐项批准或强等每步答复。
调用accounting_schema选择当前实例实际支持的kind，再读accounting://guides/business/{kind}及所选输入Schema。
完整命令合同见accounting://contracts；一次交接完整且有来源的类型化事实，用整数分字符串，不提交任意分录。
工资默认劳动所属月计提、次月计划发放；服务默认业务月确认收入应收、次月收款核销，老板已有明确安排优先。
增值税默认tax_point={type:owner_recognition_default}，与报表营业收入共用收入月及金额，不另建税点或按到账挪期。
默认不另索确认、虚构合同或税日，优惠不主动放弃也不等于自动免税；计划不证明实付、扣税或申报。
企业所得税默认按实际利润计算季度暂计，采用work_defaults.income_tax；method默认computed_provision，collection_method默认actual_profit，basis_source可独立省略，不另索默认接受证明或虚构税务来源。
所得税默认不替代企业真实税务认定、政策、优惠资格、亏损或特殊事项；已有实际方式来源继续核查，完整实际结果优先，金额路线及年度汇算仍须明确采用。
税费采用当前支持的计算或有据暂计后核验报表，未来实际申报额不是统一前置；不手算未支持税额、猜零或把partial称为完整定稿。
新业务、对象及请求编号用accounting_new_ids生成并保存在原完整请求上下文；编号不表示创建或预留。
业务提交回复不明先按原业务编号查询，更正和重试不得另换编号重复记账；生成编号回复不明可重新生成。
区分账务已保存、标准文档待完成和看板准备中，沿现有状态接续，不重复记账；资料只保存最终正确事实。
单笔、批量撤回保留原件；明确撤销整月全部记账才用accounting_cancel_month，后续从第1项收集，不恢复旧账或旧进度。
老板办理真实付款、申报等外部事项，没有审批步骤；判断有疑问AI继续核查，必要时借助顾问。
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
