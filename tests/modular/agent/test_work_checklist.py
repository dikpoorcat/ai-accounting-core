"""One acceptance source drives the human guide and the installed machine rules."""

import json
from copy import deepcopy

import pytest

from ai_accounting_modular.agent.public import INSTRUCTIONS, read_guide, read_work_checklist
from ai_accounting_modular.contracts.public import schema_document
from tests.modular.agent.test_guides import ROOT, generator


def test_checklist_conditions_and_actions_have_one_source_and_no_company_progress():
    source = json.loads((ROOT / "docs/redesign/work-checklist.json").read_text(encoding="utf-8"))
    installed = read_work_checklist()
    assert installed == source
    assert [stage["number"] for stage in installed["stages"]] == list(range(1, 9))
    guide = read_guide("work")
    identities = set()
    for stage in installed["stages"]:
        assert stage["completion"] in guide
        for check in stage["checks"]:
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
    assert set(installed) == {"format", "stages"}
    installed["stages"][0]["checks"].clear()
    assert read_work_checklist() == source


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
    assert "不需要就不生成" in checks["payment_export.scope"]["if_unmet"]
    assert "现有公司说明" in checks["payment_export.scope"]["if_unmet"]
    assert "文件不代表银行已付" in stages["payment_export"]["completion"]
    assert "经status核对为succeeded" in file["condition"]
    assert "task.path的绝对文件路径" in checks["payment_export.delivery"]["condition"]


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


@pytest.mark.parametrize("problem", ["order", "duplicate", "no_evidence", "action"])
def test_broken_checklist_cannot_silently_ship(tmp_path, problem):
    module = generator()
    source = deepcopy(read_work_checklist())
    if problem == "order":
        source["stages"][1]["number"] = 3
    elif problem == "duplicate":
        source["stages"][0]["checks"][1]["id"] = source["stages"][0]["checks"][0]["id"]
    elif problem == "no_evidence":
        source["stages"][0]["checks"][0]["evidence"] = []
    else:
        source["stages"][3]["checks"][2]["actions"][0]["command"] = ""
    path = tmp_path / "work-checklist.json"
    path.write_text(json.dumps(source, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(ValueError):
        module.work_checklist(path)
