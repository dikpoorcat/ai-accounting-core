"""Prepare isolated, natural, staged accounting cases; never run the operator's work.

The fixture seeds only prior accepted facts through the production CLI. Natural
round materials and the private numeric oracle are deliberately separate. A
fresh operator receives operator/START.md, not this script or private/.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
from hashlib import sha256
from http.cookiejar import CookieJar
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from urllib.request import HTTPCookieProcessor, Request, build_opener
from uuid import uuid4

from openpyxl import Workbook

from ai_accounting_modular.host.runtime._security import new_credentials
from ai_accounting_modular.host.runtime.public import create_runtime
from ai_accounting_modular.storage.public import CompanySpec, OwnerProfile, create_catalog

REPO = Path(__file__).resolve().parents[1]
CONTRACT = "ai-accounting-modular/1"
MARKER = "accounting-review-natural-acceptance/1"
VAT_OFFICIAL = "https://shanghai.chinatax.gov.cn/zzzb/zcwj/202602/t479283.html"
SMALL_BUSINESS_OFFICIAL = "https://fgk.chinatax.gov.cn/zcfgk/c102416/c5210453/content.html"


def write_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")


def text(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(value.strip() + "\n", encoding="utf-8")


def checked_root(path, *, new=False):
    selected = Path(path).resolve()
    if not selected.is_relative_to((REPO / ".tmp").resolve()) or selected == REPO / ".tmp":
        raise ValueError("Only an explicitly selected child of this repository's .tmp is allowed")
    if new:
        if selected.exists():
            raise ValueError("The fixture target must not already exist")
        selected.mkdir(parents=True)
    else:
        saved = json.loads((selected / "private" / "fixture.json").read_text(encoding="utf-8"))
        if saved["format"] != MARKER or Path(saved["root"]).resolve() != selected:
            raise ValueError("Not this fixture's isolated target")
    return selected


def cli(root, command, payload):
    request = root / "private" / "requests" / (str(uuid4()) + ".json")
    write_json(request, payload)
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            "-m",
            "ai_accounting_modular.host.runtime.cli",
            "--catalog",
            str(root / "state" / "catalog.sqlite3"),
            "call",
            command,
            "--input",
            str(request),
        ],
        capture_output=True,
        encoding="utf-8",
        timeout=120,
    )
    if not result.stdout:
        raise RuntimeError(result.stderr)
    answer = json.loads(result.stdout)
    if (
        result.returncode
        or answer.get("completion") == "not_saved"
        or answer.get("status") == "rejected"
    ):
        raise RuntimeError(json.dumps(answer, ensure_ascii=False))
    with (root / "private" / "seed-cli.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(
            json.dumps(
                {"command": command, "request": payload, "result": answer}, ensure_ascii=False
            )
            + "\n"
        )
    return answer


def login(runtime, password):
    request = runtime.security.request({"kind": "login"})
    result = runtime.security.native(
        "execute", {"request_id": request["request_id"], "password": password}
    )
    if not result["login_completed"]:
        raise RuntimeError("Synthetic owner login did not complete")


def source(value, period):
    value = value.strip()
    return {
        "source_id": str(uuid4()),
        "kind": "owner_statement",
        "text": value,
        "sha256": sha256(value.encode()).hexdigest(),
        "archive_month": period,
    }


def fact(kind, period, evidence, data, *, actual=None):
    return {
        "business_id": str(uuid4()),
        "kind": kind,
        "period": period,
        "evidence_ids": [evidence["source_id"]],
        "data": data,
        **({"effective_on": actual} if actual else {}),
    }


def apply(root, company, *facts, **extra):
    return cli(
        root,
        "accounting_apply",
        {
            "contract": CONTRACT,
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [{"op": "create", "expected_seq": 0, "fact": row} for row in facts],
            **extra,
        },
    )


def obj(identity, kind, name, data=None):
    return {
        "object_id": identity,
        "kind": kind,
        "name": name,
        "expected_seq": 0,
        "data": data or {},
    }


def workbook(path, rows, title="资料"):
    path.parent.mkdir(parents=True, exist_ok=True)
    book = Workbook()
    sheet = book.active
    sheet.title = title
    for row in rows:
        sheet.append(row)
    book.save(path)


def income_policy(evidence):
    return fact(
        "taxes.income_tax_policy",
        "2026-07",
        evidence,
        {
            "policy": {
                "version": "synthetic-income-tax-2026",
                "official_url": SMALL_BUSINESS_OFFICIAL,
                "effective_from": "2023-01-01",
                "effective_to": "2027-12-31",
            },
            "standard_rate": "0.25",
            "small_profit_rate": "0.20",
            "small_profit_taxable_fraction": "0.25",
            "small_profit_limit_fen": "300000000",
        },
    )


def vat_policies(evidence):
    policy = {
        "version": "synthetic-vat-2026",
        "official_url": VAT_OFFICIAL,
        "effective_from": "2026-01-01",
        "effective_to": "2027-12-31",
    }
    return (
        fact(
            "taxes.vat_policy",
            "2026-07",
            evidence,
            {
                "policy": policy,
                "nominal_rate": "0.03",
                "tax_base_rate": "0.01",
                "payable_rate": "0.01",
                "form_version": "small_taxpayer_2026",
                "threshold_fen": "30000000",
                "threshold_period": "quarter",
                "threshold_operator": "strictly_below",
                "exemption_scope": "eligible_non_excluded_sales",
                "special_invoice_may_waive_exemption": True,
                "special_invoice_may_waive_rate_reduction": True,
            },
        ),
        fact(
            "taxes.surtax_policy",
            "2026-07",
            evidence,
            {
                "policy": dict(
                    policy,
                    version="synthetic-surtax-2026",
                    official_url=SMALL_BUSINESS_OFFICIAL,
                    effective_from="2023-01-01",
                ),
                "urban_rate": "0.07",
                "education_rate": "0.03",
                "local_education_rate": "0.02",
                "payable_fraction": "0.5",
            },
        ),
    )


def prepayment(evidence, *, special=False):
    return fact(
        "taxes.prepayment_basis",
        "2026-09",
        evidence,
        {
            "tax_year": 2026,
            "quarter_end": "2026-09",
            "collection_method": "actual_profit",
            "treatment": "small_low_profit",
            "basis_source": {"source_id": evidence["source_id"]},
            "eligibility_source": {"source_id": evidence["source_id"]},
            "loss_deduction_fen": "0",
            "special_treatments": ["研发费用加计扣除"] if special else [],
        },
    )


def create_company(runtime, root, name, number):
    company = str(uuid4())
    directory = root / "state" / "companies" / str(number)
    runtime.kernel.storage.create_company(
        CompanySpec(
            company,
            str(uuid4()),
            f"91310000000000000{number}",
            name,
            "2026-07",
            directory,
            root / "state" / "backups" / str(number),
        )
    )
    return company, directory


def main_company(runtime, root):
    company, directory = create_company(runtime, root, "澄溪技术服务（隔离验收）", 1)
    employee, owner, bank, vendor, customer, authority = (str(uuid4()) for _ in range(6))
    background = source(
        "澄溪技术服务公司2026年7月新设，全部期初为零。仅有一个公司银行账户，合成银行SYNTH-CX-001；"
        "没有现金、平台或其他账户。股东陈松7月投入10万元。林青8月1日起为唯一正式行政员工；"
        "参加社保，不参加公积金。劳动月计提工资，次月发放；社保也在次月银行扣缴。"
        "公司只做境内普通技术咨询，不经营限制行业，没有分支、境外所得、其他优惠或特殊税务事项；"
        "资产总额低于500万元，从业人数1人，年度所得不到300万元；没有以前年度亏损。"
        "税务登记为按季申报的小规模纳税人，适用小型微利企业安排，优惠未放弃。公司不需要银行代发表。",
        "2026-07",
    )
    august = source(
        "已核对的8月资料：林青税前工资8000元，个人社保700元、单位社保1710元；无公积金。"
        "本次未实际扣缴个税，按当前可付额发放，不另行暂留。8月工资和8月社保在8月末均未支付。"
        "青竹文印的8月办公室资料排版费1000元已确认管理费用，月底未付。7、8月银行流水已齐，"
        "7月只有股东投入10万元，8月没有银行收付；没有其他业务、渠道、资产、借款或投资。",
        "2026-08",
    )
    opening = fact(
        "opening.package", "2026-07", background, {"basis": "newly_established", "members": []}
    )
    employment = fact(
        "people.employment",
        "2026-08",
        background,
        {
            "employee_id": employee,
            "effective_from": "2026-08",
            "social_insurance_participating": True,
            "housing_fund_participating": False,
            "contribution_shortfall": "reject",
        },
    )
    relationship = fact(
        "people.relationship",
        "2026-08",
        background,
        {
            "person_id": employee,
            "segments": [{"state": "regular", "effective_from": "2026-08"}],
        },
    )
    actual = fact(
        "people.contribution_actual",
        "2026-08",
        august,
        {
            "employee_id": employee,
            "items": [
                {
                    "code": "social",
                    "base_kind": "social_insurance",
                    "state": "declared",
                    "employee_fen": "70000",
                    "employer_fen": "171000",
                }
            ],
        },
    )
    wage = fact(
        "people.remuneration",
        "2026-08",
        august,
        {
            "type": "wage",
            "person_id": employee,
            "gross_fen": "800000",
            "expense_area": "administration",
            "employment_id": employment["business_id"],
            "contribution_basis": "actual_required",
            "contribution_actual_id": actual["business_id"],
            "disbursement_basis": {
                "type": "current_payable",
                "source": {"source_id": august["source_id"]},
            },
        },
    )
    fee_aug = fact(
        "operations.expense",
        "2026-08",
        august,
        {
            "creditor_id": vendor,
            "creditor_kind": "supplier",
            "amount_fen": "100000",
            "expense_class": "administration",
        },
    )
    capital = fact(
        "financing.owner_funding",
        "2026-07",
        background,
        {
            "owner_id": owner,
            "funding_kind": "capital",
            "amount_fen": "10000000",
            "funds_account_id": bank,
        },
        actual="2026-07-01",
    )
    objects = [
        obj(employee, "person", "林青"),
        obj(owner, "person", "陈松"),
        obj(vendor, "organization", "青竹文印"),
        obj(customer, "organization", "远山商贸"),
        obj(authority, "organization", "国库征收机关"),
        obj(
            bank,
            "bank_account",
            "合成银行公司账户",
            {
                "bank_name": "合成银行",
                "account_number": "SYNTH-CX-001",
                "currency": "CNY",
                "owner": "company",
                "opened_period": "2026-07",
                "zero_at_opening": True,
            },
        ),
    ]
    apply(
        root,
        company,
        opening,
        employment,
        relationship,
        actual,
        wage,
        fee_aug,
        capital,
        income_policy(background),
        prepayment(background),
        *vat_policies(background),
        objects=objects,
        sources=[background, august],
        notes=[
            {
                "scope": "company",
                "expected_seq": 0,
                "text": background["text"],
                "evidence_ids": [background["source_id"]],
            }
        ],
    )
    july_row = str(uuid4())
    july_statement = fact(
        "money.bank_statement",
        "2026-07",
        background,
        {
            "funds_account_id": bank,
            "opening_fen": "0",
            "closing_fen": "10000000",
            "rows": [
                {
                    "row_id": july_row,
                    "source": {
                        "source_id": background["source_id"],
                        "location": "7月完整流水第1行",
                    },
                    "actual_on": "2026-07-01",
                    "direction": "inflow",
                    "amount_fen": "10000000",
                    "counterparty_text": "陈松",
                    "memo": "股东投入资本",
                }
            ],
        },
    )
    july_check = fact(
        "money.reconciliation",
        "2026-07",
        background,
        {
            "statement_id": july_statement["business_id"],
            "matches": [
                {
                    "statement_row_ids": [july_row],
                    "fund_movement_ids": [
                        runtime.kernel.money.stable_occurrence_id(capital["business_id"], "funds")
                    ],
                }
            ],
        },
    )
    august_statement = fact(
        "money.bank_statement",
        "2026-08",
        august,
        {
            "funds_account_id": bank,
            "opening_fen": "10000000",
            "closing_fen": "10000000",
            "rows": [],
        },
    )
    august_check = fact(
        "money.reconciliation",
        "2026-08",
        august,
        {
            "statement_id": august_statement["business_id"],
            "matches": [],
        },
    )
    apply(root, company, july_statement, july_check, august_statement, august_check)
    inspected = cli(
        root, "accounting_period", {"company_id": company, "period": "2026-08", "action": "check"}
    )
    if not inspected.get("ready"):
        raise RuntimeError(json.dumps(inspected, ensure_ascii=False))
    cli(
        root,
        "accounting_period",
        {
            "company_id": company,
            "period": "2026-08",
            "action": "close",
            "expected_head": inspected["head"],
        },
    )
    current = source(
        "9月完整流水已取得，只有四笔：9月5日付林青7300元；9月8日国库扣2410元，摘要为社保；"
        "9月16日远山商贸付技术咨询费50000元；9月25日付青竹文印1000元，银行摘要只有付款。"
        "远山商贸的本次咨询服务9月已全部交付，合同总价50000元，未开票，没有其他收入；无预收或跨月履约。"
        "青竹文印另有9月办公室资料排版费1000元，已确认管理费用；9月25日转账对应哪张费用，银行原文没有写。"
        "9月没有其他银行、渠道、业务、资产、借款或投资。本期资料都在公司目录，9月人员安排稍后补充。",
        "2026-09",
    )
    fee_sep = fact(
        "operations.expense",
        "2026-09",
        current,
        {
            "creditor_id": vendor,
            "creditor_kind": "supplier",
            "amount_fen": "100000",
            "expense_class": "administration",
        },
    )
    paid_wage = fact(
        "money.movement",
        "2026-09",
        current,
        {
            "type": "settlement",
            "funds_account_id": bank,
            "direction": "outflow",
            "amount_fen": "730000",
            "payment_method": "individual",
            "counterparty_id": employee,
            "allocations": [
                {
                    "source": {"business_id": wage["business_id"], "slot": "net_pay"},
                    "amount_fen": "730000",
                }
            ],
        },
        actual="2026-09-05",
    )
    apply(root, company, fee_sep, paid_wage, sources=[current])
    path = directory / "originals" / "unassigned" / "2026年9月银行流水.xlsx"
    workbook(
        path,
        [
            ["公司", "澄溪技术服务（隔离验收）"],
            ["账户", "SYNTH-CX-001"],
            ["期间", "2026-09-01至2026-09-30"],
            ["期初余额（元）", 100000],
            ["期末余额（元）", 139290],
            ["日期", "收入（元）", "支出（元）", "对方户名", "摘要", "流水号"],
            ["2026-09-05", None, 7300, "林青", "8月工资", "CX-0905"],
            ["2026-09-08", None, 2410, "国库征收机关", "社保", "CX-0908"],
            ["2026-09-16", 50000, None, "远山商贸", "技术咨询费", "CX-0916"],
            ["2026-09-25", None, 1000, "青竹文印", "付款", "CX-0925"],
            ["说明", "本月完整流水，只有以上四笔"],
        ],
    )
    imported = cli(
        root,
        "accounting_sources",
        {
            "company_id": company,
            "action": "import",
            "sources": [
                {
                    "kind": "file",
                    "relative_path": path.relative_to(directory).as_posix(),
                    "archive_month": "2026-09",
                }
            ],
        },
    )["sources"][0]
    row_ids = [str(uuid4()) for _ in range(4)]
    statement = fact(
        "money.bank_statement",
        "2026-09",
        imported,
        {
            "funds_account_id": bank,
            "opening_fen": "10000000",
            "closing_fen": "13929000",
            "rows": [
                {
                    "row_id": row_id,
                    "source": {"source_id": imported["source_id"], "location": f"资料!{i + 7}"},
                    "actual_on": day,
                    "direction": direction,
                    "amount_fen": amount,
                    "counterparty_text": party,
                    "memo": memo,
                    "reference": reference,
                }
                for i, (row_id, day, direction, amount, party, memo, reference) in enumerate(
                    zip(
                        row_ids,
                        ["2026-09-05", "2026-09-08", "2026-09-16", "2026-09-25"],
                        ["outflow", "outflow", "inflow", "outflow"],
                        ["730000", "241000", "5000000", "100000"],
                        ["林青", "国库征收机关", "远山商贸", "青竹文印"],
                        ["8月工资", "社保", "技术咨询费", "付款"],
                        ["CX-0905", "CX-0908", "CX-0916", "CX-0925"],
                        strict=True,
                    )
                )
            ],
        },
    )
    apply(root, company, statement)
    text(
        root / "operator" / "materials" / "公司经营说明.md",
        background["text"] + "\n\n" + current["text"],
    )
    text(
        root / "operator" / "messages" / "01.md",
        "开始记账 澄溪技术服务 2026年9月。整月流水我已补齐，继续把现有资料办理好。",
    )
    text(
        root / "private" / "rounds" / "02.md",
        "9月25日付青竹文印的1000元，是付8月份那张，9月份那张还没有付。"
        "9月人员、工资、社保都按8月安排，只有林青税前工资增加500元。9月工资和9月社保都还没付，"
        "个税还没实际办理，也没有暂留其他款；工资按当前可付额安排。不需要银行代发表。",
    )
    text(
        root / "private" / "rounds" / "03.md",
        "目前给你的9月资料已经完整，没有其他业务或调整。请继续把本期账务和第三季度报表准备好。"
        "本期报表还没报送，第三季度各项税都尚未申报、缴纳。公司经营和税务基本情况没有变化。",
    )
    text(
        root / "private" / "rounds" / "04.md",
        "补上后续实际资料：10月20日第三季度企业所得税申报完成，申报表累计应纳税额1500元，"
        "本年以前季度已预缴0元，本次应补1500元。10月22日同一公司银行账户已扣1500元，"
        "对方国库征收机关，摘要企业所得税，流水号CX-1022。这是该笔税款回单，不是10月完整流水。"
        "请先处理这次实际申报和扣款，与已有第三季度账务核对；10月其他月度工作稍后再办。",
    )
    return {
        "company_id": company,
        "root": str(directory),
        "employee_id": employee,
        "bank_id": bank,
        "wage_aug": wage["business_id"],
        "fee_aug": fee_aug["business_id"],
        "fee_sep": fee_sep["business_id"],
        "paid_wage": paid_wage["business_id"],
        "statement": statement["business_id"],
        "expected": {
            "september_bank_fen": "13929000",
            "august_social_remaining_fen": "0",
            "september_net_wage_fen": "780000",
            "september_social_fen": "241000",
            "quarter_profit_before_tax_fen": "2808000",
            "computed_income_tax_fen": "140400",
            "actual_cumulative_income_tax_fen": "150000",
            "actual_tax_payment_fen": "150000",
            "actual_tax_adjustment_fen": "9600",
        },
    }


def counterexample(runtime, root, *, special):
    label = "杉木研发（隔离验收）" if special else "清禾咨询（隔离验收）"
    company, directory = create_company(runtime, root, label, 3 if special else 2)
    evidence = source(
        (
            "杉木研发7月新设，期初为零。本季向合成客户提供普通境内技术服务20000元、已经交付尚未收款，"
            "另有启明实验材料商的研发辅助资料费1000元，全部当期耗用、尚未付款，属于管理费用。"
            "没有其他业务；增值税按季申报的小规模纳税人，普通收入未放弃优惠。符合小型微利企业人数、资产和行业条件，"
            "本年无以前年度亏损。公司本季有研发费用加计扣除事项，已有事实已列入企业适用资料，"
            "尚无顾问确认的季度所得税暂计额，也没有申报或缴税。请先准备第三季度报表。"
        )
        if special
        else (
            "清禾咨询7月新设，期初为零。林夏9月1日起为唯一正式行政员工，不参加社保和公积金。"
            "9月工资尚未发放，税前工资还没有最终确定，现有资料没有这个金额。没有其他本期业务。"
        ),
        "2026-09",
    )
    opening = fact(
        "opening.package", "2026-07", evidence, {"basis": "newly_established", "members": []}
    )
    if special:
        customer = str(uuid4())
        # This case needs only the known tax-policy/basis inputs. Business is
        # supplied naturally to the operator; unsupported provision stays unseeded.
        apply(
            root,
            company,
            opening,
            income_policy(evidence),
            prepayment(evidence, special=True),
            *vat_policies(evidence),
            objects=[
                obj(customer, "organization", "合成客户"),
                obj(str(uuid4()), "organization", "启明实验材料商"),
            ],
            sources=[evidence],
            notes=[
                {
                    "scope": "company",
                    "expected_seq": 0,
                    "text": evidence["text"],
                    "evidence_ids": [evidence["source_id"]],
                }
            ],
        )
    else:
        employee = str(uuid4())
        employment = fact(
            "people.employment",
            "2026-09",
            evidence,
            {
                "employee_id": employee,
                "effective_from": "2026-09",
                "social_insurance_participating": False,
                "housing_fund_participating": False,
                "contribution_shortfall": "reject",
            },
        )
        relationship = fact(
            "people.relationship",
            "2026-09",
            evidence,
            {
                "person_id": employee,
                "segments": [{"state": "regular", "effective_from": "2026-09"}],
            },
        )
        apply(
            root,
            company,
            opening,
            employment,
            relationship,
            objects=[obj(employee, "person", "林夏")],
            sources=[evidence],
            notes=[
                {
                    "scope": "company",
                    "expected_seq": 0,
                    "text": evidence["text"],
                    "evidence_ids": [evidence["source_id"]],
                }
            ],
        )
    text(
        root / "operator" / "counterexamples" / ("special.md" if special else "missing.md"),
        evidence["text"],
    )
    return {"company_id": company, "name": label, "root": str(directory)}


def prepare(output):
    root = checked_root(output, new=True)
    password = secrets.token_urlsafe(24)
    password_digest, recovery_digest, _ = new_credentials(password)
    catalog = root / "state" / "catalog.sqlite3"
    catalog.parent.mkdir()
    create_catalog(
        catalog, OwnerProfile(str(uuid4()), "隔离验收负责人", password_digest, recovery_digest)
    )
    config = {"format": MARKER, "root": str(root), "password": password}
    write_json(root / "private" / "fixture.json", config)
    with create_runtime(catalog, window_opener=lambda _: None) as runtime:
        login(runtime, password)
        expected = {
            "format": MARKER,
            "main": main_company(runtime, root),
            "missing": counterexample(runtime, root, special=False),
            "special": counterexample(runtime, root, special=True),
        }
        expected["behavior"] = {
            "round1": [
                "自行查明8月社保义务与9月2410元扣款；不要求老板重新说明该笔社保",
                "识别9月5日工资已登记但原行尚未核对；不得重复付款",
                "青竹两张1000元义务仍无法区分时，只询问实际付的是哪张",
            ],
            "round2": [
                "采用老板给定范围复用8月人员/社保基准，只增加林青500元工资",
                "9月25日实际付款只清偿8月排版费，保留9月费未付",
            ],
            "round3": [
                "使用现有适用依据和程序计算所得税暂计，不要求未来申报回执",
                "报表技术ready不代替业务覆盖核查；不以所得税尚未申报否定有据暂计报表",
            ],
            "round4": [
                "实际申报采用1500元，与暂计只形成96元必要差异",
                "10月22日真实税款独立登记1500元，不能把申报本身当付款",
            ],
            "missing": ["只问林夏9月必要工资金额，不填零或沿用任意金额"],
            "special": ["明确研发加计扣除超出当前计算支持，不填零、不要求先申报来解锁报表"],
        }
    write_json(root / "private" / "expected.json", expected)
    prefix = (
        f'& "{sys.executable}" -I -X utf8 -m ai_accounting_modular.host.runtime.cli '
        f'--catalog "{catalog}"'
    )
    guide_command = (
        f'& "{sys.executable}" -I -X utf8 -c '
        '"from ai_accounting_modular.agent.public import read_guide; '
        "print(read_guide('work'))\""
    )
    text(
        root / "operator" / "START.md",
        f"""
这是隔离合成公司的真实会计办理验收。你担任会计AI，不修改项目，不运行准备脚本，不读取private目录、测试预期或其他验收会话。
只处理本轮老板消息；以后收到的新材料沿同一会话继续，不预读未来轮次。

先读取仓库 accounting-operator Skill，再读取安装包工作指南。
指南可通过公开Python函数read_guide读取；业务操作必须走以下真实CLI。
只读指南示例（PowerShell）：
{guide_command}
指南目录使用同一公开模块的guide_index()，按需调用read_guide读取专项指南。
安装包合同使用ai_accounting_modular.contracts.public.schema_document("core.schema.json")；
按需要选择其中的$defs，所引用合同也由schema_document读取，不加载全部业务合同。
CLI没有contracts子命令；accounting://contracts属于MCP资源，本次用上述公开只读函数取得合同。
需要报表文件时，公开HTTP合同见docs/design/06-dashboard-adapter.md第8节；
预览为GET /api/dashboard/quarterly-report?company_id=...&year=...&quarter=...。
本隔离服务启动后，http-access.json提供base_url与已登录headers，只在请求中使用，不打印连接值。
命令前缀（PowerShell）：{prefix}
例如：前缀 call accounting-context --input <绝对请求JSON文件>。
先查公司选择与当前已有资料；公司经营说明位于materials/公司经营说明.md，老板本轮消息位于messages/01.md。
使用真实accounting_*命令。无需登录真实负责人，不连接其他目录，不换库。不要读取公司SQLite或私有实现猜数据。
问题先用现有资料自行核查；确需老板补充时给出剩余现实问题，等待本会话提供后续材料。
本轮无需提交Git、修改代码或重启真实服务。最终如实报告办理结果和剩余工作。
""",
    )
    return {
        "state": "prepared",
        "operator_entry": str(root / "operator" / "START.md"),
        "private_expected": str(root / "private" / "expected.json"),
        "root": str(root),
    }


def serve(root):
    root = checked_root(root)
    saved = json.loads((root / "private" / "fixture.json").read_text(encoding="utf-8"))
    with create_runtime(
        root / "state" / "catalog.sqlite3", window_opener=lambda _: None
    ) as runtime:
        login(runtime, saved["password"])
        # Provide a normal authenticated browser surface for public file delivery.
        # These credentials belong only to this marker-checked synthetic runtime.
        address = urlsplit(runtime.browser_url())
        base = f"{address.scheme}://{address.netloc}"
        cookies = CookieJar()
        http = build_opener(HTTPCookieProcessor(cookies))
        request = Request(
            base + "/api/browser-session",
            data=json.dumps({"ticket": parse_qs(address.fragment)["ticket"][0]}).encode(),
            headers={"Content-Type": "application/json", "Origin": base},
        )
        with http.open(request, timeout=30) as response:
            if not json.load(response)["authenticated"]:
                raise RuntimeError("Synthetic browser login did not complete")
        write_json(
            root / "operator" / "http-access.json",
            {
                "base_url": base,
                "headers": {
                    "Cookie": "; ".join(f"{cookie.name}={cookie.value}" for cookie in cookies),
                    "Origin": base,
                    "Content-Type": "application/json",
                },
            },
        )
        print(json.dumps({"state": "ready", "catalog": str(runtime.catalog_path)}), flush=True)
        runtime.thread.join()


def release(root, number):
    root = checked_root(root)
    original = root / "private" / "rounds" / f"{number:02d}.md"
    target = root / "operator" / "messages" / original.name
    if number not in (2, 3, 4) or target.exists():
        raise ValueError("Only a new natural round 2, 3 or 4 may be released")
    text(target, original.read_text(encoding="utf-8"))
    return {"owner_message": target.read_text(encoding="utf-8"), "path": str(target)}


def main():
    if Path(sys.prefix).resolve() != (REPO / ".tmp-kernel-venv").resolve():
        raise ValueError("Use the repository virtual environment")
    parser = argparse.ArgumentParser(description=__doc__)
    actions = parser.add_subparsers(dest="action", required=True)
    actions.add_parser("prepare").add_argument("output", type=Path)
    actions.add_parser("serve").add_argument("root", type=Path)
    released = actions.add_parser("release")
    released.add_argument("root", type=Path)
    released.add_argument("number", type=int)
    args = parser.parse_args()
    if args.action == "serve":
        serve(args.root)
        return
    result = prepare(args.output) if args.action == "prepare" else release(args.root, args.number)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
