"""The discovered bank-file rules cover scope discovery and fourth-step completion."""

import pytest

from ai_accounting.kernel.operating_protocol import OPERATING_PROTOCOL, topic_names_for


@pytest.mark.parametrize("command", ["export_sources", "preview_export", "confirm_export"])
def test_bank_file_commands_include_scope_and_delivery_rules(command):
    assert topic_names_for([], [command], []) == {"payroll", "deliveries"}


@pytest.mark.parametrize("response", ["export_sources", "export_preview", "export_confirmation"])
def test_bank_file_response_discovery_includes_scope_and_delivery_rules(response):
    assert topic_names_for([], [], [response]) == {"payroll", "deliveries"}


def test_fourth_step_covers_bank_files_without_later_stage_backfill():
    work_entry = OPERATING_PROTOCOL["core"]["work_entry"]
    assert "4银行代发；5账务补齐与报表准备" in work_entry["start"]
    assert "工资、奖金、劳务、报销及代付款范围" in work_entry["start"]
    assert "第4步银行代发的必要资料和核算均在本步完成" in work_entry["sequence"]
    assert "不设置等待后续核算再回补代发的安排" in work_entry["sequence"]
    rules = OPERATING_PROTOCOL["topics"]["payroll"]["rules"]
    assert "本次代发文件所需资料与核算均在第4步处理" in rules["payment_file"]
    assert "不等待后续核算再回补代发" in rules["payment_file"]
    assert "已核实的真实后阶段结果依赖按通用顺序规则记录并回补" not in rules["payment_file"]


def test_bank_scope_discovery_does_not_replace_facts_or_current_delivery():
    rules = OPERATING_PROTOCOL["topics"]["payroll"]["rules"]
    discovery = rules["source_discovery"]
    assert "完整读取全部分页" in discovery
    assert "未发布来源不填写猜测金额" in discovery
    assert "不把候选为空当作没有业务" in discovery
    assert "代收债权不能导出" in discovery
    assert "用途为代付款" in discovery and "用途为代收款退回" in discovery
    assert "真实报销用途为报销" in discovery
    payment = rules["payment_file"]
    assert "所有显式source_ids统一按所选业务及直接、传递采用依据核验" in payment
    assert "混合范围中的报销月份不额外扩大工资原件检查范围" in payment
    assert "省略source_ids的完整代发保持整月范围检查" in payment
    assert "已有任务必须核对是否覆盖本次来源和金额" in payment
    assert "范围已保存或旧任务成功都不能代替本次文件交付" in payment
