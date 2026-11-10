"""Prepare natural, isolated work-discovery and closing-coverage acceptance cases.

The operator receives only START.md and the current owner message. Future owner
facts and the observer's checks stay private. All business seeding uses the CLI.
"""

from __future__ import annotations

import argparse
import json
import secrets
import sys
from pathlib import Path
from uuid import uuid4

import prepare_accounting_review_acceptance as base

from ai_accounting_modular.host.runtime._client import Client
from ai_accounting_modular.host.runtime._security import new_credentials
from ai_accounting_modular.host.runtime.public import create_runtime
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog

SCENARIO = "autonomy-close-coverage/1"


def checked_root(value, *, new=False):
    path = Path(value).resolve()
    if not path.name.startswith("autonomy-close-"):
        raise ValueError("Use a new autonomy-close-* child of the repository .tmp")
    root = base.checked_root(path, new=new)
    if not new:
        config = json.loads((root / "private/fixture.json").read_text(encoding="utf-8"))
        if config.get("scenario") != SCENARIO:
            raise ValueError("This is not an autonomy and closing fixture")
    return root


def material(root, company, directory, name, body, month):
    path = directory / "originals/incoming" / name
    base.text(path, body)
    result = base.cli(
        root,
        "accounting_sources",
        {
            "company_id": company,
            "action": "import",
            "sources": [
                {
                    "kind": "file",
                    "relative_path": path.relative_to(directory).as_posix(),
                    "archive_month": month,
                }
            ],
        },
    )
    return result["sources"][0]


def seed(runtime, root, *, number, name, start):
    company, bank, vendor = (str(uuid4()) for _ in range(3))
    directory = root / "state/companies" / str(number)
    runtime.kernel.storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            f"91310000000000000{number}",
            name,
            start,
            directory,
            root / "state/backups" / str(number),
        )
    )
    background = base.source(
        f"{name}于{start}新设，期初为零。唯一资金账户为合成银行 SYNTH-AUTO-{number}，"
        "没有现金或平台资金。公司只做境内普通维修服务，没有员工、劳务人员、工资、社保、公积金，"
        "没有固定资产、存货、投资或借款。公司长期不用银行代发表。"
        "税务登记按季申报，小规模纳税人，享受小型微利企业安排且不放弃适用优惠。"
        "本年没有收入，无以前年度亏损、境外所得、分支或特殊税务处理；资产和人数低于小微限额。"
        "财报报送及季度纳税申报在10月由老板办理，尚未报送或申报。",
        start,
    )
    arrangements = base.source(
        "办公室维修费已经登记，供应商尚未收款；本月不需要代发表，沿用公司长期安排。"
        "财报与季度申报由老板在下月统一办理。",
        "2026-09",
    )
    receipt = material(
        root,
        company,
        directory,
        "维修结算单.txt",
        f"{name}办公室维修结算单\n服务方：青竹维修\n完工日期：2026-09-16\n"
        "维修费：1000元\n截止9月30日尚未支付。",
        "2026-09",
    )
    statement_sources = {}
    for month in dict.fromkeys((start, "2026-09")):
        statement_sources[month] = material(
            root,
            company,
            directory,
            month + "银行对账单.txt",
            f"合成银行\n户名：{name}\n账号：SYNTH-AUTO-{number}\n"
            f"完整对账期间：{month}-01至{month}-{'31' if month.endswith('08') else '30'}\n"
            "期初余额：0.00元\n期末余额：0.00元\n本期无收付款流水。",
            month,
        )
    material(
        root,
        company,
        directory,
        "供应商重开票邮件.txt",
        "2026-10-03 青竹维修邮件：9月16日那笔办公室维修费的发票抬头已更正，"
        "金额仍为1000元，未收到付款。这里只是重开原票，没有新增维修或费用。",
        "2026-10",
    )
    opening = base.fact(
        "opening.package",
        start,
        background,
        {
            "basis": "newly_established",
            "members": [],
        },
    )
    expense = base.fact(
        "operations.expense",
        "2026-09",
        receipt,
        {
            "creditor_id": vendor,
            "creditor_kind": "supplier",
            "amount_fen": "100000",
            "expense_class": "administration",
            "report_details": [{"code": "management_other", "amount_fen": "100000"}],
        },
    )
    income_policy = base.income_policy(background) | {"period": start}
    prepayment = base.prepayment(background)
    vat_policy, surtax_policy = [
        value | {"period": start} for value in base.vat_policies(background)
    ]
    income = base.fact(
        "taxes.assessment",
        "2026-09",
        background,
        {
            "type": "income_tax",
            "method": "computed_provision",
            "tax_year": 2026,
            "quarter_end": "2026-09",
            "policy_id": income_policy["business_id"],
            "prepayment_basis_id": prepayment["business_id"],
        },
    )
    vat = base.fact(
        "taxes.assessment",
        "2026-09",
        background,
        {
            "type": "vat",
            "start_month": "2026-07",
            "end_month": "2026-09",
            "filing_profile": {
                "taxpayer_type": "small_scale",
                "form_scope": "nonpilot",
                "source": {"source_id": background["source_id"]},
            },
            "vat_policy_id": vat_policy["business_id"],
            "surtax_policy_id": surtax_policy["business_id"],
        },
    )
    statements = []
    for month, evidence in statement_sources.items():
        statement = base.fact(
            "money.bank_statement",
            month,
            evidence,
            {
                "funds_account_id": bank,
                "opening_fen": "0",
                "closing_fen": "0",
                "rows": [],
            },
        )
        statements.extend(
            (
                statement,
                base.fact(
                    "money.reconciliation",
                    month,
                    evidence,
                    {
                        "statement_id": statement["business_id"],
                        "matches": [],
                    },
                ),
            )
        )
    external = [
        {
            "issue_id": str(uuid4()),
            "scope": "company",
            "period": "2026-10",
            "required_before_close": "2026-09",
            "evidence_ids": [background["source_id"]],
            "missing_fields": [],
            "question": title,
            "assignee": "owner",
            "action": "external_filing",
        }
        for title in (
            "第三季度财报尚未向外报送，需老板反馈实际办理结果。",
            "第三季度增值税及企业所得税尚未申报，需老板反馈实际办理结果。",
        )
    ]
    base.apply(
        root,
        company,
        opening,
        expense,
        income_policy,
        prepayment,
        vat_policy,
        surtax_policy,
        income,
        vat,
        *statements,
        sources=[background, arrangements],
        objects=[
            base.obj(vendor, "organization", "青竹维修"),
            base.obj(
                bank,
                "bank_account",
                "合成银行公司账户",
                {
                    "bank_name": "合成银行",
                    "account_number": f"SYNTH-AUTO-{number}",
                    "currency": "CNY",
                    "owner": "company",
                    "opened_period": start,
                    "zero_at_opening": True,
                },
            ),
        ],
        notes=[
            {
                "scope": "company",
                "expected_seq": 0,
                "text": background["text"],
                "evidence_ids": [background["source_id"]],
            },
            {
                "scope": "work:2026-09",
                "expected_seq": 0,
                "text": arrangements["text"],
                "evidence_ids": [arrangements["source_id"]],
            },
        ],
        unresolved=external,
        unresolved_scope="company",
        expected_scope_head=0,
    )
    return {
        "company_id": company,
        "name": name,
        "root": str(directory),
        "start": start,
        "expense_id": expense["business_id"],
        "bank_id": bank,
        "vendor_id": vendor,
        "external_issue_ids": [item["issue_id"] for item in external],
        "baseline_payable_fen": "100000",
        "income_tax_fen": "0",
        "september_bank_source": statement_sources["2026-09"]["source_id"],
    }


def prepare(output):
    root = checked_root(output, new=True)
    password = secrets.token_urlsafe(24)
    digest, recovery, _ = new_credentials(password)
    catalog = root / "state/catalog.sqlite3"
    catalog.parent.mkdir()
    create_catalog(catalog, OwnerProfile(str(uuid4()), "自主办理隔离负责人", digest, recovery))
    base.write_json(
        root / "private/fixture.json",
        {
            "format": base.MARKER,
            "scenario": SCENARIO,
            "root": str(root),
            "password": password,
        },
    )
    with create_runtime(catalog, window_opener=lambda _: None) as runtime:
        base.login(runtime, password)
        companies = {
            "coverage": seed(runtime, root, number=7, name="清禾维修服务", start="2026-09"),
            "range": seed(runtime, root, number=8, name="松岚维修服务", start="2026-08"),
        }
        return finish(root, companies)


def finish(root, companies):
    catalog = root / "state/catalog.sqlite3"
    base.write_json(root / "private/companies.json", companies)
    observation = observe(root, companies)
    assert all(not item["closed_periods"] for item in observation.values())
    assert all(item["report_ready"] and not item["close_ready"] for item in observation.values())
    base.write_json(root / "private/seed-observation.json", observation)
    prefix = (
        f'& "{sys.executable}" -I -X utf8 -m ai_accounting_modular.host.runtime.cli '
        f'--catalog "{catalog}"'
    )
    base.text(
        root / "operator/START.md",
        f"""
这是隔离合成公司的会计办理。你担任会计AI，只处理本轮老板消息，后续由同一对话补充。
先读取仓库 accounting-operator Skill 和实际安装指南。只操作本隔离实例，不连接真实 MCP，
不读 SQLite、私有实现、准备脚本、private目录或未来消息，不修改代码或运行测试。
当前安装指南使用 ai_accounting_modular.agent.public.read_guide 和 guide_index 读取；
命令与种类合同使用公开 accounting_schema 命令按需取得。
所有业务操作通过实际 CLI，PowerShell 前缀：
{prefix}
例如在前缀后使用 call accounting_context --input <绝对JSON路径>。请求文件存入本 operator 目录。
资料位置通过公司公开资料入口发现。需要真实文件交付时，隔离服务的 http-access.json
提供已登录HTTP入口，仅在请求中使用，不打印连接值。无需登录真实负责人。
本轮老板消息位于 messages/01.md。确需老板回答时提出问题并等待后续消息。
""",
    )
    base.text(root / "operator/messages/01.md", "开始记账 清禾维修服务")
    base.text(root / "operator-b/messages/01.md", "开始记账 松岚维修服务")
    operator_entry(root, "operator-b", root / "operator-b/messages/01.md")
    cards = {
        "coverage-bank": "合成银行这份就是9月从月初到月底的全部流水，期初、期末余额都没问题。",
        "coverage-complete": "9月的业务资料已经全部交给你了：只有那笔1000元办公室维修费，"
        "还没付。没有其他收支、没走银行的业务、未付款费用或需要调整的事项。"
        "公司和人员情况没变。第三季度财报和税务还没报。",
        "coverage-continue": "可以，先把后面能做的核对继续办，外部报送和申报办好我再告诉你。",
        "coverage-delta": "刚翻出一张9月25日的材料：青竹维修另修了门锁，费用500元，也还没付。"
        "这是9月唯一新增的资料，其他情况仍按我前面的答复。把这张补进去。",
        "coverage-resume": "继续清禾维修服务。先接着现有安排核对，告诉我还剩哪些事情。",
        "range-start": "开始记账 松岚维修服务",
        "range-september": "9月全部业务资料已经交齐，就1000元维修费未支付，没有其他业务或调整。"
        "第三季度财报和税务还没有报。",
        "range-continue": "可以先把后面能做的核对办了，报送和申报等我稍后办完再告诉你。",
        "range-august": "8月是新设公司，没有收入、费用、收付款、人员、资产或其他业务，"
        "没有漏交的资料；8月和9月的资料现在都已交齐。",
        "range-close-first": "第三季度财报和税务确实还没报。现在先把8月和9月关账，"
        "报送和申报办完我再反馈，请继续提醒我这两件事。",
    }
    for name, value in cards.items():
        base.text(root / "private/owner" / (name + ".txt"), value)
    base.text(
        root / "private/materials/门锁维修结算单.txt",
        "清禾维修服务门锁维修结算单\n服务方：青竹维修\n完工日期：2026-09-25\n"
        "费用：500元\n截至9月30日未付款。此单为另一项维修，不替换办公室维修1000元。",
    )
    return {
        "state": "prepared",
        "root": str(root),
        "operator_entry": str(root / "operator/START.md"),
        "owner_cards": str(root / "private/owner"),
        "companies": companies,
    }


def observe(root, companies=None):
    if companies is None:
        companies = json.loads((root / "private/companies.json").read_text(encoding="utf-8"))
    values = {}
    for key, company in companies.items():
        identity = company["company_id"]
        context = base.cli(root, "accounting_context", {"company_id": identity})
        checked = base.cli(
            root,
            "accounting_period",
            {
                "company_id": identity,
                "period": "2026-09",
                "action": "check",
            },
        )
        report = base.cli(
            root,
            "accounting_report",
            {
                "company_id": identity,
                "year": 2026,
                "quarter": 3,
            },
        )
        businesses = base.cli(
            root,
            "accounting_find",
            {
                "company_id": identity,
                "family": "operations",
                "period_from": company["start"],
                "period_to": "2026-09",
                "status": "active",
                "limit": 100,
            },
        )
        saved = base.cli(
            root,
            "accounting_get",
            {
                "company_id": identity,
                "business_ids": [item["business_id"] for item in businesses["items"]],
                "include": ["facts", "results"],
            },
        )
        values[key] = {
            "company": company["name"],
            "work_period": context["work_period"],
            "closed_periods": context["closed_periods"],
            "notes": context["notes"],
            "issues": context["issues"],
            "report_ready": report["status"] == "ready",
            "close_ready": checked["ready"],
            "period_check": checked,
            "businesses": saved["businesses"],
        }
    return values


def login(root):
    """Renew only this marker-checked synthetic owner's normal local session."""
    saved = json.loads((root / "private/fixture.json").read_text(encoding="utf-8"))
    client = Client(root / "state/catalog.sqlite3", autostart=False)
    window = client.security("request", {"kind": "login"})
    result = client.call(
        "/private/native",
        {
            "operation": "execute",
            "payload": {"request_id": window["request_id"], "password": saved["password"]},
        },
    )
    if not result["login_completed"]:
        raise RuntimeError("Synthetic owner login did not complete")
    return {"state": "logged_in", "root": str(root)}


def operator_entry(root, directory, message):
    body = (root / "operator/START.md").read_text(encoding="utf-8")
    body = body.replace("messages/01.md", str(message))
    body = body.replace("http-access.json", str(root / "operator/http-access.json"))
    target = root / directory / "START.md"
    base.text(target, body)
    return str(target)


def future_clue(root):
    """An explicitly deferred future note must not displace unfinished current work."""
    company = json.loads((root / "private/companies.json").read_text(encoding="utf-8"))["coverage"]
    evidence = base.source(
        "明年1月准备整理新年度的资料目录。这是以后再办的安排，现在暂时不办理。", "2027-01"
    )
    base.apply(
        root,
        company["company_id"],
        sources=[evidence],
        notes=[
            {
                "scope": "work:2027-01",
                "expected_seq": 0,
                "text": evidence["text"],
                "evidence_ids": [evidence["source_id"]],
            }
        ],
    )
    message = root / "operator-future/messages/01.md"
    base.text(message, "继续清禾维修服务，告诉我目前还有什么没办完。")
    return {"operator_entry": operator_entry(root, "operator-future", message)}


def release(root, card):
    source = root / "private/owner" / (card + ".txt")
    if source.parent.resolve() != (root / "private/owner").resolve() or not source.is_file():
        raise ValueError("Select one existing owner fact card")
    directory = "operator-b" if card.startswith("range-") else "operator"
    destination = root / directory / "messages" / source.name
    if destination.exists():
        raise ValueError("This owner fact card is already released")
    if card == "coverage-delta":
        companies = json.loads((root / "private/companies.json").read_text(encoding="utf-8"))
        company = companies["coverage"]
        evidence = material(
            root,
            company["company_id"],
            Path(company["root"]),
            "门锁维修结算单.txt",
            (root / "private/materials/门锁维修结算单.txt").read_text(encoding="utf-8"),
            "2026-09",
        )
        base.write_json(root / "private/delta-source.json", evidence)
    base.text(destination, source.read_text(encoding="utf-8"))
    result = {"owner_message": destination.read_text(encoding="utf-8"), "path": str(destination)}
    if card == "coverage-resume":
        result["operator_entry"] = operator_entry(root, "operator-resume", destination)
    return result


def main():
    if Path(sys.prefix).resolve() != (base.REPO / ".tmp-kernel-venv").resolve():
        raise ValueError("Use the repository virtual environment")
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    commands.add_parser("prepare").add_argument("root", type=Path)
    commands.add_parser("serve").add_argument("root", type=Path)
    commands.add_parser("observe").add_argument("root", type=Path)
    commands.add_parser("login").add_argument("root", type=Path)
    commands.add_parser("future-clue").add_argument("root", type=Path)
    released = commands.add_parser("release")
    released.add_argument("root", type=Path)
    released.add_argument("card")
    args = parser.parse_args()
    if args.action == "prepare":
        result = prepare(args.root)
    else:
        root = checked_root(args.root)
        if args.action == "serve":
            base.serve(root)
            return
        if args.action == "login":
            result = login(root)
        elif args.action == "future-clue":
            result = future_clue(root)
        else:
            result = observe(root) if args.action == "observe" else release(root, args.card)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
