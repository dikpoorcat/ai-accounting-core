"""One acceptance source drives the human guide and the installed machine rules."""

import json
from copy import deepcopy

import pytest

from ai_accounting_modular.agent.public import (
    INSTRUCTIONS,
    read_guide,
    read_work_checklist,
    read_work_stage,
)
from ai_accounting_modular.contracts.public import schema_document
from tests.modular.agent.test_guides import ROOT, generator, installed_checklist


def test_checklist_conditions_and_actions_have_one_source_and_no_company_progress():
    source = json.loads((ROOT / "docs/design/work-checklist.json").read_text(encoding="utf-8"))
    installed = installed_checklist()
    assert installed == source
    assert [stage["number"] for stage in installed["stages"]] == list(range(1, 9))
    guide = read_guide("work")
    identities = set()
    for stage in installed["stages"]:
        assert stage["completion"] in guide
        assert read_work_stage(str(stage["number"])) == stage
        assert f"accounting://guides/work/stages/{stage['number']}" in guide
    assert read_work_checklist()["common_checks"] == source["common_checks"]
    checks = installed["common_checks"] + [
        check for stage in installed["stages"] for check in stage["checks"]
    ]
    for check in checks:
        assert check["id"] not in identities
        identities.add(check["id"])
        assert check["condition"] not in guide
    assert "<!-- generated-work-checklist -->" not in guide
    assert "[内部核对规则](accounting://guides/work/checklist)" in guide
    # This payload is static rules, never a shadow workflow or an AI decision log.
    assert set(installed) == {"format", "common_checks", "stages"}
    installed["stages"][0]["checks"].clear()
    installed["common_checks"].clear()
    assert installed_checklist() == source


def test_payment_result_check_is_shared_before_the_stages_without_per_payment_actions():
    rules = installed_checklist()
    check = next(item for item in rules["common_checks"] if item["id"] == "common.payment_results")
    guide = read_guide("work")
    assert check["id"] == "common.payment_results"
    assert check in read_work_checklist()["common_checks"]
    assert check["condition"] not in guide
    assert "actions" not in check
    assert "本步骤、本批次" in check["condition"]
    assert "新办、变更或接续尚未核对" in check["condition"]
    assert all(state in check["condition"] for state in ("known", "not_applicable", "missing"))
    assert "对象为空不证明不适用" in check["condition"]
    assert "own_funds_channel" in check["if_unmet"]
    assert "不能把managed_reserve一律视为不适用" in check["if_unmet"]
    assert "仅计提不索取未来付款资料" in check["condition"]
    assert "仅生成代发表不视为已实际付款，仍按文件自身条件检查" in check["condition"]
    assert "不逐笔另起查询" in "；".join(check["evidence"])
    assert "只重核新增、变更或依据失效部分" in check["if_unmet"]
    assert "原行对方" in rules["stages"][0]["checks"][1]["condition"]
    assert "不汇集前面应已处理的遗漏" in rules["stages"][4]["completion"]


def test_work_scope_precedes_checks_without_disabling_cross_period_dependencies():
    rules = installed_checklist()
    scope = rules["common_checks"][0]
    assert scope["id"] == "common.work_scope"
    assert "actions" not in scope
    assert "work_period" in scope["condition"]
    assert "不机械要求其所属月等于工作月" in scope["condition"]
    assert "null只表示尚未选择，不意味着必须问老板" in "".join(scope["evidence"])
    assert "无关月份独立事项保留原范围" in scope["if_unmet"]
    assert "跨月依赖只核当前动作需要的部分，不改变已选月份" in scope["if_unmet"]
    assert "不为了少问缩小交付" in scope["condition"]
    assert "字段必填或专业上可能有关不证明必须问老板" in "".join(scope["evidence"])
    assert "先判断某项信息是否影响当前结果，再核对既有来源" in scope["if_unmet"]
    shared = next(item for item in rules["common_checks"] if item["id"] == "common.unfinished_work")
    assert "本轮工作范围内所有未完成项" in shared["condition"]
    assert "未答复或选择等待时不提前推进" in shared["condition"]
    financial = rules["stages"][5]["checks"]
    assert "工作月所在季度直接推断" in financial[0]["condition"]
    assert "季度中间月" in financial[0]["if_unmet"]
    assert "上年覆盖期" in financial[0]["if_unmet"]
    assert "本次已确定覆盖期" in financial[1]["if_unmet"]
    assert "当前季度" not in json.dumps(rules, ensure_ascii=False)
    assert "其他月份独立事项不问" in INSTRUCTIONS


def test_payment_file_is_an_action_and_actual_delivery_is_a_separate_condition():
    stages = {item["id"]: item for item in installed_checklist()["stages"]}
    checks = {item["id"]: item for item in stages["payment_export"]["checks"]}
    file = checks["payment_export.file"]
    assert [item["action"] for item in file["actions"]] == ["generate", "status"]
    assert all(item["command"] == "accounting_payment_export" for item in file["actions"])
    assert all(state in file["condition"] for state in ("pending", "running", "failed", "expired"))
    assert "实际交付" in stages["payment_export"]["completion"]
    assert "先实际交付当前有效文件" in checks["payment_export.delivery"]["if_unmet"]
    assert "空查询或没有文件任务仍需核查实际范围" in checks["payment_export.scope"]["condition"]
    assert "首次" in checks["payment_export.scope"]["if_unmet"]
    assert "没有待付金额不生成空表" in checks["payment_export.scope"]["if_unmet"]
    assert "公司说明" in checks["payment_export.scope"]["if_unmet"]
    assert "原约定完整发放范围的款项已实际发放" in stages["payment_export"]["completion"]
    assert "经status核对为succeeded" in file["condition"]
    delivery = checks["payment_export.delivery"]
    assert "copied或reused" in delivery["condition"]
    assert "实际桌面绝对路径" in delivery["condition"]
    assert "内部task.path用于任务定位" in delivery["if_unmet"]
    assert delivery["actions"][0]["action"] == "save"
    assert "destination固定为desktop" in delivery["if_unmet"]


def test_payroll_disbursement_requires_actual_payment_for_the_complete_scope():
    stage = read_work_stage("4")
    checks = {item["id"]: item for item in stage["checks"]}
    actual = checks["payment_export.actual_payment"]
    assert stage["title"] == "工资代发"
    assert list(checks) == [
        "payment_export.accounting",
        "payment_export.scope",
        "payment_export.file",
        "payment_export.delivery",
        "payment_export.actual_payment",
    ]
    assert "完整发放范围" in actual["condition"]
    assert "文件已交付但待付款、部分成功或失败待处理时保持🔄" in stage["completion"]
    assert "老板明确不用代发表只影响文件项目" in checks["payment_export.scope"]["condition"]
    assert "明确不用代发表时仍须核实必要核算和实际发放" in stage["completion"]
    assert all(
        state in actual["condition"]
        for state in ("已提交银行", "处理中", "部分成功", "失败", "退回")
    )
    assert all(
        term in actual["condition"]
        for term in ("无相关业务", "确无应发款项", "remaining_fen", "held_fen", "抵销", "转移")
    )
    assert "money.movement按实际付款月" in actual["if_unmet"]
    assert "已登记付款并复用有效核对" in actual["if_unmet"]
    assert "部分成功只办理成功部分" in actual["if_unmet"]
    assert "核对截止月覆盖已知实际付款月" in "".join(actual["evidence"])
    assert "required_before_close按现有独立语义判断" in actual["if_unmet"]
    assert "actions" not in actual


def test_paid_scope_reuses_delivery_and_payment_evidence_after_file_expiry():
    checks = {item["id"]: item for item in read_work_stage("4")["checks"]}
    file = checks["payment_export.file"]
    delivery = checks["payment_export.delivery"]
    assert "付款导致的expired或旧计划退出" in file["condition"]
    assert "无待执行款项时完成真实结果核对" in file["condition"]
    assert "本次尚有待执行款项" in file["actions"][0]["when"]
    assert "尚未提交银行或明确失败、退回需重办" in file["actions"][0]["when"]
    assert "已提交银行仍处理中的款项等待原批次结果" in file["actions"][0]["when"]
    assert "已实际发完的范围沿实付核验接续" in file["actions"][0]["when"]
    assert "已核实发完且无待执行款项时结束相应文件工作" in delivery["condition"]
    assert "已提交银行的款项复用提交时有效交付" in delivery["condition"]
    assert "付款后复用已核实交付" in delivery["actions"][0]["when"]


def test_unfinished_steps_and_contribution_feedback_share_the_installed_rules():
    rules = installed_checklist()
    shared = next(item for item in rules["common_checks"] if item["id"] == "common.unfinished_work")
    guide = read_guide("work")
    assert shared in read_work_checklist()["common_checks"]
    assert shared["condition"] not in guide
    assert "未答复或选择等待时不提前推进" in shared["condition"]
    assert "每次更新步骤清单都提醒本轮范围内未完成事项" in shared["if_unmet"]
    stages = {item["id"]: item for item in rules["stages"]}
    contribution = {item["id"]: item for item in stages["contributions"]["checks"]}
    assert "均不算本项完成" in stages["contributions"]["completion"]
    assert "not_declared零额事实" in contribution["contributions.result"]["if_unmet"]
    adoption = contribution["contributions.adoption"]
    assert "contribution_actual_id" in adoption["condition"]
    assert "已有引用则复用依赖重算" in adoption["if_unmet"]
    assert "按第4项的银行办理状态及待重办范围接续文件" in adoption["if_unmet"]
    payment = next(
        item for item in stages["payment_export"]["checks"] if item["id"] == "payment_export.scope"
    )
    assert "work_defaults.payment_export" in payment["condition"]
    assert "本月例外保存在本月安排" in payment["if_unmet"]


def test_payment_actions_match_the_public_contract_and_instructions_point_to_rules():
    branches = schema_document("payment-export.schema.json")["$defs"]["PaymentExportRequest"][
        "oneOf"
    ]
    supported = set()
    for branch in branches:
        if "$ref" in branch:
            branch = schema_document("core.schema.json")["$defs"][branch["$ref"].rsplit("/", 1)[-1]]
        supported.add(branch["properties"]["action"]["const"])
    payment = read_work_stage("4")
    declared = {
        action["action"] for check in payment["checks"] for action in check.get("actions", ())
    }
    assert declared == supported == {"inspect", "generate", "status", "save"}
    assert "在打勾前核对当前事项的完成条件" in INSTRUCTIONS
    assert "缺少成果时执行所列接口动作" in INSTRUCTIONS


@pytest.mark.parametrize(
    "problem",
    [
        "order",
        "duplicate",
        "no_evidence",
        "action",
        "missing_common",
        "empty_common",
        "common_duplicate",
        "common_scope",
        "common_no_evidence",
        "common_state",
    ],
)
def test_broken_checklist_cannot_silently_ship(tmp_path, problem):
    module = generator()
    source = installed_checklist()
    if problem == "order":
        source["stages"][1]["number"] = 3
    elif problem == "duplicate":
        source["stages"][0]["checks"][1]["id"] = source["stages"][0]["checks"][0]["id"]
    elif problem == "no_evidence":
        source["stages"][0]["checks"][0]["evidence"] = []
    elif problem == "action":
        source["stages"][3]["checks"][2]["actions"][0]["command"] = ""
    elif problem == "missing_common":
        del source["common_checks"]
    elif problem == "empty_common":
        source["common_checks"] = []
    elif problem == "common_duplicate":
        source["common_checks"].append(deepcopy(source["common_checks"][0]))
    elif problem == "common_scope":
        source["common_checks"][0]["id"] = "accounting.payment_results"
    elif problem == "common_no_evidence":
        source["common_checks"][0]["evidence"] = []
    else:
        source["common_checks"][0]["completed"] = True
    path = tmp_path / "work-checklist.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        module.work_checklist(path)
