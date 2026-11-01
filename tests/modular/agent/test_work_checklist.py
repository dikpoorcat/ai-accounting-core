"""One acceptance source drives the human guide and the installed machine rules."""

import json
from copy import deepcopy

import pytest

from ai_accounting_modular.agent.public import INSTRUCTIONS, read_guide, read_work_checklist
from ai_accounting_modular.contracts.public import schema_document
from tests.modular.agent.test_guides import ROOT, generator


def test_checklist_conditions_and_actions_have_one_source_and_no_company_progress():
    source = json.loads((ROOT / "docs/design/work-checklist.json").read_text(encoding="utf-8"))
    installed = read_work_checklist()
    assert installed == source
    assert [stage["number"] for stage in installed["stages"]] == list(range(1, 9))
    guide = read_guide("work")
    identities = set()
    for stage in installed["stages"]:
        assert stage["completion"] in guide
    checks = installed["common_checks"] + [
        check for stage in installed["stages"] for check in stage["checks"]
    ]
    for check in checks:
        assert check["id"] not in identities
        identities.add(check["id"])
        assert check["condition"] in guide
        assert check["if_unmet"] in guide
        assert all(item in guide for item in check["evidence"])
        for action in check.get("actions", ()):
            assert f"{action['command']}(action={action['action']})" in guide
    assert "<!-- generated-work-checklist -->" not in guide
    assert "[内部核对规则](accounting://guides/work/checklist)" in guide
    # This payload is static rules, never a shadow workflow or an AI decision log.
    assert set(installed) == {"format", "common_checks", "stages"}
    installed["stages"][0]["checks"].clear()
    installed["common_checks"].clear()
    assert read_work_checklist() == source


def test_payment_result_check_is_shared_before_the_stages_without_per_payment_actions():
    rules = read_work_checklist()
    check = next(item for item in rules["common_checks"] if item["id"] == "common.payment_results")
    guide = read_guide("work")
    assert check["id"] == "common.payment_results"
    assert guide.count(f"`{check['id']}`") == 1
    assert guide.index(check["condition"]) < guide.index("1. **流水与基础资料**")
    assert "actions" not in check
    assert "本步骤、本批次" in check["condition"]
    assert "新办、变更或接续尚未核对" in check["condition"]
    assert "仅计提不索取未来付款资料" in check["condition"]
    assert "仅生成代发表不视为已实际付款，仍按文件自身条件检查" in check["condition"]
    assert "不逐笔另起查询" in "；".join(check["evidence"])
    assert "只重核新增、变更或依据失效部分" in check["if_unmet"]
    assert "原行对方" in rules["stages"][0]["checks"][1]["condition"]
    assert "不汇集前面应已处理的遗漏" in rules["stages"][4]["completion"]


def test_payment_file_is_an_action_and_actual_delivery_is_a_separate_condition():
    stages = {item["id"]: item for item in read_work_checklist()["stages"]}
    checks = {item["id"]: item for item in stages["payment_export"]["checks"]}
    file = checks["payment_export.file"]
    assert [item["action"] for item in file["actions"]] == ["generate", "status"]
    assert all(item["command"] == "accounting_payment_export" for item in file["actions"])
    assert all(state in file["condition"] for state in ("pending", "running", "failed", "expired"))
    assert "实际交付" in stages["payment_export"]["completion"]
    assert "先实际交付当前有效文件" in checks["payment_export.delivery"]["if_unmet"]
    assert "不是无需代发的依据" in checks["payment_export.scope"]["condition"]
    assert "首次" in checks["payment_export.scope"]["if_unmet"]
    assert "没有待付金额不生成空表" in checks["payment_export.scope"]["if_unmet"]
    assert "公司说明" in checks["payment_export.scope"]["if_unmet"]
    assert "文件不代表实际付款" in stages["payment_export"]["completion"]
    assert "经status核对为succeeded" in file["condition"]
    assert "task.path的绝对文件路径" in checks["payment_export.delivery"]["condition"]


def test_unfinished_steps_and_contribution_feedback_share_the_installed_rules():
    rules = read_work_checklist()
    shared = next(item for item in rules["common_checks"] if item["id"] == "common.unfinished_work")
    guide = read_guide("work")
    assert guide.count(f"`{shared['id']}`") == 1
    assert shared["condition"] in guide and shared["if_unmet"] in guide
    assert "未答复或选择等待时不提前推进" in shared["condition"]
    assert "每次更新步骤清单都提醒未完成事项" in shared["if_unmet"]
    stages = {item["id"]: item for item in rules["stages"]}
    contribution = {item["id"]: item for item in stages["contributions"]["checks"]}
    assert "均不算本项完成" in stages["contributions"]["completion"]
    assert "not_declared零额事实" in contribution["contributions.result"]["if_unmet"]
    assert "contribution_actual_id" in contribution["contributions.adoption"]["condition"]
    assert "已有引用则复用依赖重算" in contribution["contributions.adoption"]["if_unmet"]
    payment = next(
        item for item in stages["payment_export"]["checks"] if item["id"] == "payment_export.scope"
    )
    assert "work_defaults.payment_export" in payment["condition"]
    assert "本月例外只保存本月" in payment["if_unmet"]


def test_payment_actions_match_the_public_contract_and_instructions_point_to_rules():
    branches = schema_document("payment-export.schema.json")["$defs"]["PaymentExportRequest"][
        "oneOf"
    ]
    supported = {item["properties"]["action"]["const"] for item in branches}
    payment = read_work_checklist()["stages"][3]
    declared = {
        action["action"] for check in payment["checks"] for action in check.get("actions", ())
    }
    assert declared == supported == {"inspect", "generate", "status"}
    assert "打勾前逐项核对工作指南中的内部条件" in INSTRUCTIONS
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
    source = deepcopy(read_work_checklist())
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
