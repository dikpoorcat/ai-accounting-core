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
from ai_accounting_modular.agent.public import (
    guide_index,
    read_guide,
    read_work_checklist,
    read_work_stage,
)
from ai_accounting_modular.contracts.public import ContractError, schema_document

ROOT = Path(__file__).resolve().parents[3]


def installed_checklist():
    directory = read_work_checklist()
    return {
        **directory,
        "stages": [read_work_stage(str(stage["number"])) for stage in directory["stages"]],
    }


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
        assert "公共事实与计算条件" not in text and "义务转移与多付重分类" not in text
    assert "公共事实与计算条件" in read_guide("business")
    assert "义务转移与多付重分类" in read_guide("corrections")
    assert "### 货币基金" in read_guide("business", kind="investment.money_fund_purchase")
    assert "### 合作项目投资" not in read_guide("business", kind="investment.money_fund_purchase")
    assert "direct_joint_operation_cash_labor_v1" in read_guide(
        "business", kind="investment.cooperation_contract"
    )
    assert "## 税费和期间口径" in read_guide("business", kind="taxes.income_tax_policy")
    assert "## 期初与报表接续" in read_guide("business", kind="reports.continuation")
    assert "## 资料与撤回" in read_guide("work")
    assert (
        "[合作项目投资](accounting://guides/business/investment.cooperation_contract)"
        in read_guide("business", kind="operations.expense")
    )
    assert "## 模块之间的公开能力" not in read_guide("commands")
    assert "## 开发与行为验收" not in read_guide("work")
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
    assert "只保存老板实际表达的现实事实和范围" in work
    assert "不能把AI判断、程序报错或操作聊天写成老板确认" in work
    assert "银行摘要、户名和备注保持原义" in work
    assert "清理只走受保护入口，不扫删未处理资料" in work
    assert "文档与物理清理pending沿原状态接续" in work
    commands = read_guide("commands")
    assert "SourcesCommandResult" in commands and "SourceDiscardResult" in commands
    assert "登记已退出但文件清理尚待后台接续" in commands
    assert "call accounting-sources" in read_guide("runtime")


def test_managed_reserve_guide_preserves_self_owned_channel_as_sourced_management_fact():
    guide = read_guide("business", kind="money.movement")
    assert "own_funds_channel={name,source}" in guide
    assert "两者互斥" in guide and "source采用SourceLocator" in guide
    assert "不为补充渠道额外追加evidence_ids" in guide
    assert "不进入冻结核算输入、正式资金或凭证辅助对象" in guide
    assert "补充、修改和清空沿无核算影响更正办理" in guide


def test_installed_work_guide_includes_month_cancel_and_local_withdrawal_distinction():
    work = read_guide("work")
    operations = generator().markdown_section(work, "## 资料与撤回")
    assert "先inspect范围与head再cancel" in operations
    assert "单笔或指定多笔误记用withdraw并保留原件" in operations
    assert "局部纠错只重核受影响项" in operations
    assert "整月撤销后重新办理才从第1项收集核查" in operations
    assert "保留的跨月、长期、模板或冻结来源如实报告" in operations
    assert "其他有效资料可以复用，被退出资料需重新提供" in operations
    assert "pending由后台沿旧清理标识接续" in read_guide("runtime")
    assert "不继承旧勾选或恢复旧进度" in operations
    assert "只交办撤回时办完即止" in operations


def test_installed_work_guide_preserves_owner_checklist_and_evidence_boundaries():
    work = read_guide("work")
    checklist = generator().markdown_section(work, "## 内部核对与老板看到的进度")
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
    assert "月度办理每轮" in checklist and "没有推进或技术受阻也如实保留进度" in checklist
    assert "保存成功、技术ready和旧勾选不代替核验" in checklist
    assert "已有有效结果复用，必要时批量查证" in checklist
    assert "不能证明现实业务齐全" in checklist
    assert "正常完成后直接顺序推进，不逐项批准" in checklist
    assert "主动询问变化先展示相关基准，只问差异" in work
    assert all(icon in checklist for icon in ("✅", "🔄", "⬜", "⏰", "➖"))
    assert "3. 🔄 社保、公积金：待您反馈本期申报结果" in checklist
    assert "八行编号列表" in checklist and "不用表格" in checklist
    assert "图标后保留文字" in checklist and "加粗成对闭合" in checklist
    assert "明确到期未办" in checklist and "🔄办理、等待或接续" in checklist
    assert "**⬜ 待补本月完整流水**" not in checklist


def test_stage_completion_owns_only_its_work_and_drives_both_progress_and_next_step():
    work = read_guide("work")
    checklist = generator().markdown_section(work, "## 内部核对与老板看到的进度")
    stages = dict(re.findall(r"^([1-8])\. \*\*.+?\*\*：(.*)$", checklist, re.MULTILINE))
    assert set(stages) == set("12345678")
    assert "不要求每笔正式入账" in stages["1"]
    assert "后阶段专属资料" in stages["1"] and "第8项正式整月核对" in stages["1"]
    assert "性质完全未知" in stages["1"] and "渠道或流水有缺口" in stages["1"]
    assert "第3项" in stages["2"] and "第4项" in stages["2"]
    assert "计提不必等待后来扣税、申报和付款" in stages["2"]
    assert "实际申报或核定反馈" in stages["3"] and "均不算本项完成" in stages["3"]
    assert "不需要文件也不能漏掉必要核算" in stages["4"]
    assert "文档已完成" in stages["4"] and "文件不代表实际付款" in stages["4"]
    assert "文件已生成" in stages["6"] and "不能称为已报送" in stages["6"]
    assert "后月份付款不强行登记在本期" in stages["7"]
    assert "相同范围实际关账" in stages["8"] and "不把冻结成功说成现实事项已办完" in stages["8"]
    assert "适用内部项全部满足，外层阶段才打勾" in checklist
    assert "先说明缺口、拟做范围和采用依据，由老板决定" in checklist
    assert "不隐藏、不打勾、不重置" in checklist
    assert "未答复或选择等待时不提前推进" in checklist
    assert "保留本轮未完成项、具体缺口和执行者" in checklist
    assert "工资一人的核算资料不足，不阻止无关的银行对账" not in work


def test_removed_or_ambiguous_source_section_fails_generation(tmp_path):
    source = tmp_path / "docs/design"
    source.mkdir(parents=True)
    for name in (
        "03-contracts.md",
        "05-business-rules.md",
        "05b-financial-reporting.md",
        "08-ai-work-and-materials.md",
        "09-runtime-and-ai.md",
        "work-checklist.json",
    ):
        shutil.copyfile(ROOT / "docs/design" / name, source / name)
    file = source / "05-business-rules.md"
    text = file.read_text(encoding="utf-8")
    for changed in (
        text.replace("### 合作项目投资", "### 标题已变更"),
        text + "\n### 合作项目投资\n",
    ):
        file.write_text(changed, encoding="utf-8")
        with pytest.raises(ValueError, match="heading must exist exactly once"):
            generator().guide_outputs(tmp_path, schema_document("business-facts.schema.json"))
    shutil.copyfile(ROOT / "docs/design/05-business-rules.md", file)
    work = source / "08-ai-work-and-materials.md"
    text = work.read_text(encoding="utf-8")
    heading = "## 资料与撤回"
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
    stage = read_work_stage("4")
    stage["checks"].clear()
    assert read_work_stage("4")["checks"]
    index = read_work_checklist()
    assert all("checks" not in item for item in index["stages"])
    assert [item["uri"] for item in index["stages"]] == [
        f"accounting://guides/work/stages/{number}" for number in range(1, 9)
    ]
    index["common_checks"].clear()
    assert read_work_checklist()["common_checks"]


@pytest.mark.parametrize("stage", ["0", "9", "04", "payment_export", "../work", 4, True])
def test_stage_resources_require_an_exact_number(stage):
    with pytest.raises(ContractError) as failure:
        read_work_stage(stage)
    assert failure.value.code == "unsupported_guide"


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
from ai_accounting_modular.agent.public import (
    guide_index, read_guide, read_work_checklist, read_work_stage,
)
import ai_accounting_modular.agent.public as public
assert Path(public.__file__).is_relative_to(Path(sys.argv[1]))
assert not (Path(sys.argv[1]) / 'docs').exists()
assert 'ai_accounting_modular.application.public' not in sys.modules
assert '实际收付款另记' in read_guide('business', kind='operations.expense')
assert 'company-create' in read_guide('runtime')
assert 'owner_statement' in read_guide('work')
assert '## 内部核对与老板看到的进度' in read_guide('work')
assert '保存成功、技术ready和旧勾选不代替核验' in read_guide('work')
assert '🔄办理、等待或接续' in read_guide('work')
assert '适用内部项全部满足，外层阶段才打勾' in read_guide('work')
assert 'SourceDiscardResult' in read_guide('commands')
assert 'call accounting-sources' in read_guide('runtime')
assert read_work_stage('4')['checks'][2]['id'] == 'payment_export.file'
check = next(
    item for item in read_work_checklist()['common_checks']
    if item['id'] == 'common.payment_results'
)
assert check in read_work_checklist()['common_checks']
assert check['condition'] not in read_guide('work')
scope = read_work_checklist()['common_checks'][0]
assert scope['id'] == 'common.work_scope'
assert scope in read_work_checklist()['common_checks']
assert read_work_stage('4')['id'] == read_work_checklist()['stages'][3]['id']
assert 'work_period=null' in read_guide('work')
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
