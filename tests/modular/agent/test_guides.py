"""Installed operating text stays synchronized, explicit and independent of workspace docs."""

import importlib.util
import json
import re
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from ai_accounting_modular.agent import _guides
from ai_accounting_modular.agent.public import guide_index, read_guide, read_work_checklist
from ai_accounting_modular.contracts.public import ContractError, schema_document

ROOT = Path(__file__).resolve().parents[3]


def generator():
    spec = importlib.util.spec_from_file_location(
        "guide_generator", ROOT / "tools/generate_modular_contracts.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_all_actual_fact_inputs_have_exact_guides_and_generated_bytes():
    module = generator()
    document = schema_document("business-facts.schema.json")
    generated = module.guide_outputs(ROOT, document)
    assert all(
        path.read_bytes().replace(b"\r\n", b"\n") == content for path, content in generated.items()
    )
    book = json.loads(next(iter(generated.values())))
    assert set(book["business"]["kinds"]) == module.fact_kinds(document)
    assert len(guide_index()["topics"]) == 5
    for kind in module.fact_kinds(document):
        text = read_guide("business", kind=kind)
        assert text.startswith("# 业务规则：" + kind)
        assert "公共事实与计算条件" in text and "义务转移与多付重分类" in text
    assert "### 货币基金" in read_guide("business", kind="investment.money_fund_purchase")
    assert "### 合作项目投资" not in read_guide("business", kind="investment.money_fund_purchase")
    assert "direct_joint_operation_cash_labor_v1" in read_guide(
        "business", kind="investment.cooperation_contract"
    )
    assert "## 税费和期间口径" in read_guide("business", kind="taxes.income_tax_policy")
    assert "## 期初与报表接续" in read_guide("business", kind="reports.continuation")
    assert "## 撤回、整月撤销与重新记账" in read_guide("work")
    assert (
        "[合作项目投资](accounting://guides/business/investment.cooperation_contract)"
        in read_guide("business", kind="operations.expense")
    )
    assert "## 模块之间的公开能力" not in read_guide("commands")
    assert "## agent 模块的交付验证" not in read_guide("work")
    assert "## 开发验收" not in read_guide("reporting")


def test_new_fact_kind_requires_an_explicit_mapping_instead_of_family_fallback():
    document = deepcopy(schema_document("business-facts.schema.json"))
    document["$defs"]["Fact"]["oneOf"].append(
        {"properties": {"kind": {"const": "operations.new_unmapped_kind"}}}
    )
    with pytest.raises(ValueError, match="missing=.*operations.new_unmapped_kind"):
        generator().guide_outputs(ROOT, document)


def test_shipped_material_guidance_separates_owner_facts_from_dialogue_and_cleanup():
    work = read_guide("work")
    assert "owner_statement" in work
    assert "现实会计事实和必要确认" in work
    assert "整段聊天、程序报错或恢复、重复尝试和操作指令" in work
    assert "真正的原始文件不改写" in work
    assert "accounting_sources(action=discard)" in work
    assert "不能自动扫删尚未处理的原件" in work
    assert "实际文件清理仍待后台接续" in work
    commands = read_guide("commands")
    assert "SourcesCommandResult" in commands and "SourceDiscardResult" in commands
    assert "登记已退出但文件清理尚待后台接续" in commands
    assert "call accounting-sources" in read_guide("runtime")


def test_installed_work_guide_includes_month_cancel_and_local_withdrawal_distinction():
    work = read_guide("work")
    operations = generator().markdown_section(work, "## 撤回、整月撤销与重新记账")
    assert "accounting_cancel_month" in operations and "expected_head" in operations
    assert "单笔或指定多笔撤回是局部自纠" in operations
    assert "继续原办理位置，不把整月退回第1项" in operations
    assert "整月撤销后重新开始，才从第1项收集和核对本期资料" in operations
    assert "retained_source_ids" in operations
    assert "文件 pending 由后台自动接续旧清理，不能重复discard来催清理" in operations
    assert "保护重新导入的登记与原件" in work
    assert "pending由后台沿旧清理标识接续" in read_guide("runtime")
    assert "不要求恢复已删除的旧正文或旧编号映射" in operations


def test_installed_work_guide_preserves_owner_checklist_and_evidence_boundaries():
    work = read_guide("work")
    checklist = generator().markdown_section(work, "## 给老板看的办理清单")
    stages = (
        "流水与基础资料",
        "工资、劳务与个税",
        "社保、公积金",
        "银行代发表",
        "账务补齐与报表准备",
        "财务报表报送",
        "所得税及其他适用申报",
        "收尾核对与关账",
    )
    assert re.findall(r"^(\d+)\. \*\*(.+?)\*\*：", checklist, re.MULTILINE) == [
        (str(number), stage) for number, stage in enumerate(stages, 1)
    ]
    assert "每轮会计办理回复" in checklist and "没有推进时也保留" in checklist
    assert "一次保存成功" in checklist and "不能代替整项完成" in checklist
    assert "不为展示清单反复查全库" in checklist
    assert "note_scopes" in checklist and "issue_scopes" in checklist
    assert "ready" in checklist and "不证明所有现实业务" in checklist
    assert "接续位置" in checklist and "逐项批准" in checklist
    assert "先展示已经查明的原有基准" in work
    assert "资料从哪里取得" in work
    assert all(icon in checklist for icon in ("✅", "🔄", "⬜", "⏰", "➖"))
    assert "1. 🔄 流水与基础资料：当前待补本月完整流水。" in checklist
    assert "八行编号列表" in checklist and "不用表格" in checklist
    assert "图标后保留直白文字" in checklist and "加粗标记成对" in checklist
    assert "已有明确期限且确实到期未办" in checklist
    assert "当前缺资料仍保留🔄" in checklist
    assert "**⬜ 待补本月完整流水**" not in checklist


def test_stage_completion_owns_only_its_work_and_drives_both_progress_and_next_step():
    work = read_guide("work")
    checklist = generator().markdown_section(work, "## 给老板看的办理清单")
    stages = dict(re.findall(r"^([1-8])\. \*\*.+?\*\*：(.*)$", checklist, re.MULTILINE))
    assert set(stages) == set("12345678")
    assert "不要求每笔正式入账" in stages["1"]
    assert "后阶段专属资料" in stages["1"] and "第8项正式整月核对" in stages["1"]
    assert "性质完全未知" in stages["1"] and "渠道或流水有缺口" in stages["1"]
    assert "第3项" in stages["2"] and "第4项" in stages["2"]
    assert "计提不必等待后来扣税、申报和付款" in stages["2"]
    assert "允许政策估算时不强求" in stages["3"]
    assert "不需代发表也不能漏掉必要核算" in stages["4"]
    assert "文档已完成" in stages["4"] and "文件不代表银行已付" in stages["4"]
    assert "文件已生成" in stages["6"] and "不能称为已报送" in stages["6"]
    assert "后月份付款不强行登记在本期" in stages["7"]
    assert "程序已实际关账" in stages["8"] and "检查通过不等于已经关账" in stages["8"]
    assert "同时决定图标与接续位置" in checklist
    assert "同阶段条件满足的独立事项" in checklist and "不据此跳步" in checklist
    assert "原项保持未完成" in checklist and "先保全归期" in checklist
    assert "后期办理安排" in checklist and "原项保持🔄" in checklist
    assert "现实资料缺口也不能改称后期待办绕过" in checklist
    assert "保留其他仍未解决的事项" in work
    assert "工资一人的核算资料不足，不阻止无关的银行对账" not in work


def test_removed_or_ambiguous_source_section_fails_generation(tmp_path):
    source = tmp_path / "docs/redesign"
    source.mkdir(parents=True)
    for name in (
        "03-contracts.md",
        "05-business-rules.md",
        "05b-financial-reporting.md",
        "08-ai-work-and-materials.md",
        "09-runtime-and-ai.md",
        "work-checklist.json",
    ):
        shutil.copyfile(ROOT / "docs/redesign" / name, source / name)
    file = source / "05-business-rules.md"
    text = file.read_text(encoding="utf-8")
    for changed in (
        text.replace("### 合作项目投资", "### 标题已变更"),
        text + "\n### 合作项目投资\n",
    ):
        file.write_text(changed, encoding="utf-8")
        with pytest.raises(ValueError, match="heading must exist exactly once"):
            generator().guide_outputs(tmp_path, schema_document("business-facts.schema.json"))
    shutil.copyfile(ROOT / "docs/redesign/05-business-rules.md", file)
    work = source / "08-ai-work-and-materials.md"
    text = work.read_text(encoding="utf-8")
    heading = "## 撤回、整月撤销与重新记账"
    for changed in (text.replace(heading, "## 尚未打包的撤销规则"), text + "\n" + heading + "\n"):
        work.write_text(changed, encoding="utf-8")
        with pytest.raises(ValueError, match="heading must exist exactly once"):
            generator().guide_outputs(tmp_path, schema_document("business-facts.schema.json"))


@pytest.mark.parametrize(
    "topic,kind",
    [
        ("../../README.md", None),
        ("not_installed", None),
        (True, None),
        ("business", "operations.not_installed"),
        ("business", "../../README.md"),
        ("business", ["operations.expense"]),
        ("work", "operations.expense"),
    ],
)
def test_only_explicit_topics_and_complete_fact_kinds_are_readable(topic, kind):
    with pytest.raises(ContractError) as failure:
        read_guide(topic, kind=kind)
    assert failure.value.code == "unsupported_guide"


def test_directory_is_small_and_mutation_cannot_change_shared_text():
    index = guide_index()
    assert len(json.dumps(index, ensure_ascii=False)) < 2500
    before = read_guide("business", kind="operations.expense")
    index["topics"][0]["title"] = "调用方改写"
    index["topics"].clear()
    assert len(guide_index()["topics"]) == 5
    assert read_guide("business", kind="operations.expense") == before
    assert "accounting_schema" in guide_index()["message"]
    assert read_work_checklist()["stages"][3]["id"] == "payment_export"


def test_copied_package_reads_guides_without_workspace_application_or_docs(tmp_path):
    package = tmp_path / "installed/ai_accounting_modular"
    package.mkdir(parents=True)
    source = ROOT / "src/ai_accounting_modular"
    shutil.copyfile(source / "__init__.py", package / "__init__.py")
    for name in ("agent", "contracts"):
        shutil.copytree(source / name, package / name, ignore=shutil.ignore_patterns("__pycache__"))
    # This installation slice has no docs, Application or source workspace lookup available.
    script = """
import json, sys
from pathlib import Path
sys.path.insert(0, sys.argv[1])
from ai_accounting_modular.agent.public import guide_index, read_guide, read_work_checklist
import ai_accounting_modular.agent.public as public
assert Path(public.__file__).is_relative_to(Path(sys.argv[1]))
assert not (Path(sys.argv[1]) / 'docs').exists()
assert 'ai_accounting_modular.application.public' not in sys.modules
assert '实际收付款另记' in read_guide('business', kind='operations.expense')
assert 'company-create' in read_guide('runtime')
assert 'owner_statement' in read_guide('work')
assert '## 给老板看的办理清单' in read_guide('work')
assert '一次保存成功' in read_guide('work')
assert '当前缺资料仍保留🔄' in read_guide('work')
assert '同时决定图标与接续位置' in read_guide('work')
assert 'SourceDiscardResult' in read_guide('commands')
assert 'call accounting-sources' in read_guide('runtime')
assert read_work_checklist()['stages'][3]['checks'][2]['id'] == 'payment_export.file'
print(json.dumps(guide_index(), ensure_ascii=False))
"""
    process = subprocess.run(
        [sys.executable, "-I", "-X", "utf8", "-c", script, str(package.parent)],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )
    assert process.returncode == 0, process.stderr
    assert len(json.loads(process.stdout)["topics"]) == 5


@pytest.mark.parametrize("raw", [b"{}", b"[]", b'{"format":1,"format":2}', b"not JSON"])
def test_missing_or_invalid_installed_bundle_is_an_explicit_error(tmp_path, monkeypatch, raw):
    (tmp_path / "_guides.json").write_bytes(raw)
    _guides._book.cache_clear()
    monkeypatch.setattr(_guides, "files", lambda _: tmp_path)
    try:
        with pytest.raises(ContractError) as failure:
            guide_index()
        assert failure.value.code == "storage_corrupt"
    finally:
        _guides._book.cache_clear()
