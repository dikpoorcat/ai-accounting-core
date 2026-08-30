"""Versioned operating contract for AI clients of the accounting core.

This is runtime product guidance, not a repository-development instruction.
Every MCP client receives the concise instruction at initialization and can
retrieve the structured protocol from ``finance_get_event_schema``.
"""

from __future__ import annotations

from typing import Any

AI_OPERATING_PROTOCOL_VERSION = "evidence_first_minimum_question_v6"

EVIDENCE_FIRST_RUNTIME_INSTRUCTION = (
    "先充分审阅和交叉核对用户已经提供的原始材料、规范化数据、银行流水及内核现有事实，"
    "能够由这些事实和冻结规则唯一确定的事项由AI直接形成结论，不得再次让用户确认。"
    "只有仍缺少会改变入账金额、会计分类、归属期间、税额或能否入账的关键事实时，"
    "才向用户提出最少且具体的问题；提问前必须说明已核对的事实、当前结论、"
    "缺少的准确事实及其影响。不得用“还有没有收入／费用”等泛泛询问代替材料核对，"
    "不得把数据库没有记录推断为没有业务，也不得臆测缺失事实。"
)

CLOSE_APPROVAL_RUNTIME_INSTRUCTION = (
    "现账关账需要负责人密码复核时，AI必须调用 "
    "finance_request_accounting_period_close_approval_window，启动标题为"
    "‘AI 记账内核 - 关账密码确认’的独立可见本机窗口；负责人表示完成后，再调用 "
    "finance_get_accounting_period_close_approval 取得与当前会话、期间和预览哈希精确匹配的"
    "未消费授权。普通MCP会话过期时，请求工具仍应直接启动该专用窗口，不得先要求负责人"
    "完成一次通用登录再重复输入关账密码；专用窗口等待期间，授权查询遇到旧会话或过期会话"
    "只能继续返回等待状态，不得另启通用登录窗。approve-close 是专用窗口内部命令，AI不得直接在"
    "隐藏终端、后台会话、"
    "MCP stdio、Codex底部终端或其他不可见输入通道中运行它等待密码；窗口未出现时必须"
    "修复启动链，不得回退到不可见终端。AI不得索取、代输、读取或记录负责人密码。"
)

CLOSE_BACKUP_RUNTIME_INSTRUCTION = (
    "正式关账前，AI必须调用 finance_get_close_backup_configuration 核对自动备份配置和就绪状态；"
    "未配置或未就绪时不得绕过。finance_confirm_accounting_period_close 在关账事务提交后由内核"
    "自动导出该公司一致性快照并生成便携ZIP，返回 close_backup；AI必须核对其 status。"
    "status=completed 才表示本次关账备份完成；若关账已 posted 但 close_backup.status=failed，"
    "AI应使用完全相同的关账请求重试，让内核幂等续做同一关账的备份，不得重复关账、另写临时"
    "备份脚本或把手工复制文件当成成功。持续失败时应报告稳定错误码和已关账但备份未完成的状态。"
)

MCP_SERVER_INSTRUCTIONS = (
    "这是确定性记账内核，不是自由分录接口。调用企业数据工具前先调用 "
    "finance_get_event_schema，并遵守其 agent_operating_protocol。"
    f"{EVIDENCE_FIRST_RUNTIME_INSTRUCTION}"
    "关账预览返回 management_commentary 时，必须严格依据其中的 context、instruction "
    "和 success_criteria 生成月度经营解读，并在确认关账时提交解读及 context_hash；"
    "解读应形成综合判断，不得把看板指标简单拼接成结论。"
    "无法唯一确定时让受控工作流返回 needs_information。"
    f"{CLOSE_APPROVAL_RUNTIME_INSTRUCTION}"
    f"{CLOSE_BACKUP_RUNTIME_INSTRUCTION}"
    "所有金额使用整数分，日期使用 YYYY-MM-DD。"
)


def agent_operating_protocol() -> dict[str, Any]:
    """Return a fresh JSON-safe protocol payload for MCP discovery."""

    return {
        "version": AI_OPERATING_PROTOCOL_VERSION,
        "objective": "充分利用已有事实，在不臆测的前提下把对用户的打扰降到最低。",
        "required_sequence": [
            {
                "code": "inspect_available_materials",
                "instruction": (
                    "提问前先读取并交叉核对用户已提供的原始材料、已保存规范化数据、"
                    "银行流水、既有业务事件、开放项、期间和税务状态。"
                ),
            },
            {
                "code": "derive_when_unique",
                "instruction": (
                    "已有事实与冻结规则能够唯一确定结果时，AI直接形成结论并使用受控工具推进，"
                    "不得把已提供材料重新包装成问题交还用户。"
                ),
            },
            {
                "code": "identify_material_unknowns",
                "instruction": (
                    "只把会改变金额、分类、归属期间、税额或能否入账的未知事实列为待补信息。"
                ),
            },
            {
                "code": "separate_contribution_policy_actual_and_cash",
                "instruction": (
                    "社保公积金必须区分公司统一政策计算基线、员工所属月逐险种实际申报应缴、"
                    "现金缴款和历史补缴。材料证明某员工某月漏报或少报险种时，先登记有证据的"
                    "逐险种实际事实；恢复正常月份继续使用统一政策。历史补缴绑定原所属月，"
                    "但在实际确认月份入账，不得重算或改写已关闭工资批次。"
                ),
            },
            {
                "code": "apply_first_wage_tax_treatment_only_with_evidence",
                "instruction": (
                    "年度中间首次取得工资薪金的累计减除费用待遇必须按员工和纳税年度单独登记，"
                    "并留存其此前未取得工资薪金、也未按累计预扣法预扣连续性劳务报酬个税的"
                    "证据或负责人确认。不得通过提前员工入职日、伪造个税期初状态或默认为所有"
                    "新员工适用来得到税额；符合条件时按国家税务总局公告2020年第13号从当年1月"
                    "起累计5000元/月。"
                ),
            },
            {
                "code": "generate_period_close_management_commentary",
                "instruction": (
                    "每次关账必须使用预览提供的 management_commentary 上下文和版本化要求生成"
                    "月度经营解读：提炼经营阶段、环比趋势、损益与现金差异、回款付款节奏和"
                    "一至两个关键风险；不得复述看板、不得猜测 context 不能证明的原因，并将"
                    "原文及 context_hash 一并提交给确认关账工具。"
                ),
            },
            {
                "code": "launch_visible_close_approval_window",
                "instruction": CLOSE_APPROVAL_RUNTIME_INSTRUCTION,
            },
            {
                "code": "verify_automatic_close_backup",
                "instruction": CLOSE_BACKUP_RUNTIME_INSTRUCTION,
            },
            {
                "code": "ask_minimum_specific_question",
                "instruction": (
                    "确需询问时，先陈述已核对事实和当前结论，再说明一个准确缺口及其影响；"
                    "问题必须最少、具体、可回答。"
                ),
            },
            {
                "code": "submit_or_stop",
                "instruction": (
                    "事实唯一时提交类型化业务事实；仍不唯一时停止该项并返回 needs_information，"
                    "不得用默认值或主观判断补齐。"
                ),
            },
        ],
        "question_policy": {
            "provided_materials": (
                "默认AI尚未完成审阅，必须实际核对；不得假定用户再次说明才算提供。"
            ),
            "generic_questions": "禁止用泛泛询问替代对已提供材料的审阅和推理。",
            "final_fallback": (
                "完成材料核对后，如需完整性兜底，只能明确询问：除已提供并核对的材料外，"
                "是否另有尚未提供且会影响本次记账或报税的业务材料。"
            ),
        },
        "prohibitions": [
            "不得臆测会改变会计或税务处理的事实。",
            "不得把数据库空记录当作没有业务的证据。",
            "不得重复询问已由材料和内核事实唯一证明的事项。",
            "不得让用户代替AI完成银行流水、规范化数据和既有事件之间的核对。",
            "不得在隐藏或不可见的终端通道中等待负责人输入关账密码。",
            "不得绕过内核关账自动备份、以临时脚本或手工文件复制替代其完成状态。",
        ],
    }
