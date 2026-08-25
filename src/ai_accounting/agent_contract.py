"""Versioned operating contract for AI clients of the accounting core.

This is runtime product guidance, not a repository-development instruction.
Every MCP client receives the concise instruction at initialization and can
retrieve the structured protocol from ``finance_get_event_schema``.
"""

from __future__ import annotations

from typing import Any


AI_OPERATING_PROTOCOL_VERSION = "evidence_first_minimum_question_v1"

EVIDENCE_FIRST_RUNTIME_INSTRUCTION = (
    "先充分审阅和交叉核对用户已经提供的原始材料、规范化数据、银行流水及内核现有事实，"
    "能够由这些事实和冻结规则唯一确定的事项由AI直接形成结论，不得再次让用户确认。"
    "只有仍缺少会改变入账金额、会计分类、归属期间、税额或能否入账的关键事实时，"
    "才向用户提出最少且具体的问题；提问前必须说明已核对的事实、当前结论、"
    "缺少的准确事实及其影响。不得用“还有没有收入／费用”等泛泛询问代替材料核对，"
    "不得把数据库没有记录推断为没有业务，也不得臆测缺失事实。"
)

MCP_SERVER_INSTRUCTIONS = (
    "这是确定性记账内核，不是自由分录接口。调用企业数据工具前先调用 "
    "finance_get_event_schema，并遵守其 agent_operating_protocol。"
    f"{EVIDENCE_FIRST_RUNTIME_INSTRUCTION}"
    "无法唯一确定时让受控工作流返回 needs_information。"
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
        ],
    }
