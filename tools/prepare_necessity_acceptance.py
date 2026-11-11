"""Create isolated natural cases for necessary facts, without coaching the operator."""

from __future__ import annotations

import argparse
import secrets
import sys
from pathlib import Path
from uuid import uuid4

import prepare_accounting_review_acceptance as base

from ai_accounting_modular.host.runtime._security import new_credentials
from ai_accounting_modular.host.runtime.public import create_runtime
from ai_accounting_modular.storage.public import OwnerProfile, create_catalog

SCENARIO = "current-action-necessity/1"


def seed(runtime, root, number, name):
    company, directory = base.create_company(runtime, root, name, number)
    bank, owner = str(uuid4()), str(uuid4())
    background = base.source(
        f"{name}在2026年7月新设，期初全部为零，唯一公司银行账户是合成银行 SYNTH-NEED-{number}。"
        "股东林远7月1日借给公司50万元，已经全部到账。公司本期只交办指定业务登记，"
        "不是要求完成全月记账、报表或关账。没有公司代持安排。本公司不需要银行代发文件。",
        "2026-07",
    )
    base.apply(
        root,
        company,
        base.fact(
            "opening.package", "2026-07", background, {"basis": "newly_established", "members": []}
        ),
        base.fact(
            "financing.owner_funding",
            "2026-07",
            background,
            {
                "owner_id": owner,
                "funding_kind": "loan",
                "amount_fen": "50000000",
                "funds_account_id": bank,
            },
            actual="2026-07-01",
        ),
        sources=[background],
        objects=[
            base.obj(owner, "person", "林远"),
            base.obj(
                bank,
                "bank_account",
                "公司银行账户",
                {
                    "bank_name": "合成银行",
                    "account_number": f"SYNTH-NEED-{number}",
                    "currency": "CNY",
                    "owner": "company",
                    "opened_period": "2026-07",
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
            }
        ],
    )
    return {"company_id": company, "name": name, "root": str(directory)}


def seed_policy(root, company):
    evidence = base.source(
        "澄源已核实的长期税务安排：按季申报的小规模纳税人，普通技术服务适用现有1%减征及季度优惠，"
        "未放弃优惠；本来源的政策参数已由程序登记，后续业务直接复用有效政策。",
        "2026-07",
    )
    base.apply(root, company, *base.vat_policies(evidence), sources=[evidence])


def prepare(output):
    root = base.checked_root(output, new=True)
    password = secrets.token_urlsafe(24)
    password_digest, recovery_digest, _ = new_credentials(password)
    catalog = root / "state/catalog.sqlite3"
    catalog.parent.mkdir()
    create_catalog(
        catalog, OwnerProfile(str(uuid4()), "隔离负责人", password_digest, recovery_digest)
    )
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
        main = seed(runtime, root, 1, "澄源技术服务（隔离验收）")
        missing = seed(runtime, root, 2, "青禾技术服务（隔离验收）")
        seed_policy(root, main["company_id"])
    base.text(
        root / "operator/messages/01.md",
        """请处理澄源技术服务公司7月这几件事：

7月16日公司认购月影基金，本金20万元，53868.87份，手续费零，不含另行购买的已宣告收益。是公司自己的基金份额投资，我们计划长期拿着。交易相对方为月影基金，银行当天向月影基金付了20万元。

7月18日青竹维修为办公室修空调，1200元已经确认应付，公司还没付款；票据暂时没有。

7月20日从旭日办公买了一台打印机5000元，已验收取得，未付款，暂未投入使用。不是员工买来报销。

7月行政员工周青税前工资8000元，应归公司管理费用。他7月1日入职，是本公司全职员工，本期一直在职；社保申报已确认个人700元、单位1710元，无公积金。没有另外的工资调整，实际个税还没扣。工资和社保都尚未支付。

先把这些业务办理好。
""",
    )
    base.text(
        root / "private/rounds/02.md",
        """还是澄源，刚才那个投资7月本身没有收益或退出。此前说的长期持有安排不变。
现在再登记一笔7月23日青竹维修空调保养费300元，尚未付，费用归管理；发票还没拿到。
""",
    )
    base.text(
        root / "private/rounds/03.md",
        """现在处理青禾技术服务公司7月这几笔：

7月16日公司认购星澜基金10万元、10000份，手续费零，没有另买已宣告未发的收益。是公司自己的基金份额，我们打算长期拿着。交易对方是星澜基金，银行当天10万元实际收款人是张海。

员工陈平7月1日起全职入职，本月一直在职，行政工作，工资500元。已确认的7月社保申报个人700元、单位1710元，无公积金；没有其他工资补扣，也没实际扣个税，工资、社保尚未支付。

另有青竹维修7月18日完成办公室维修，应计入管理费用，但我这次发来的材料里没有金额。
""",
    )
    base.text(
        root / "private/rounds/04.md",
        """张海是星澜基金指定代收人，基金已经确认认购款收妥，不是我们替张海投资。
陈平工资不够扣的部分先记为他欠公司的，以后再还。办公室维修是1200元。
""",
    )
    base.text(
        root / "private/rounds/04b.md",
        """
青禾陈平这200元我改一下安排：由公司承担，不再让员工归还。请接着把他的7月工资和社保账务办理好，仍未实际付款。
""",
    )
    base.text(
        root / "private/rounds/05.md",
        """
继续澄源7月。客户竹溪咨询7月25日的普通技术服务已经完成，价税合计1010元，普通发票已开，尚未收款。
本公司的小规模季度申报、现有1%政策和未放弃优惠安排照旧。
另有7月26日从旭日办公取得的一台扫描仪，已验收、还没启用、也没付款，但价格资料这次没带。
先把能明确的业务办好。
""",
    )
    base.text(
        root / "private/rounds/06.md",
        """
扫描仪价格3000元，没有其他取得费用，还是没付款。
""",
    )
    base.write_json(
        root / "private/expected.json",
        {
            "scenario": SCENARIO,
            "main": main,
            "missing": missing,
            "main_expected": {
                "investment_cost_fen": "20000000",
                "quantity": "53868.87",
                "expense_fen": "120000",
                "asset_cost_fen": "500000",
                "wage_fen": "800000",
                "net_wage_fen": "730000",
                "employer_social_fen": "171000",
            },
            "necessary_gaps": [
                "不同实际收款人的代收关系",
                "工资不足的200元如何处理",
                "已发生维修金额",
            ],
            "not_required": [
                "投资税务资格",
                "管理人资料包",
                "未来赎回安排",
                "可选发票",
                "未启用资产使用寿命",
                "未发生实际扣税",
                "无不足时的差额安排",
            ],
            "round2": "复用长期安排，追加费用300元，不能因无发票延迟",
            "round5": "自行复用已有政策登记1010元服务收入；仅核实扫描仪的真实取得成本",
            "round6": "采用3000元成本办理扫描仪取得，不追问未启用寿命",
            "round4": (
                "代收和维修完成；员工欠款路线超出当前工资能力时，不编造入账、不转为老板缺资料"
            ),
            "round4b": "老板改为单位承担后，复用已知工资及社保结果完成核算，不虚构实际付款",
        },
    )
    prefix = (
        f'& "{sys.executable}" -I -X utf8 -m ai_accounting_modular.host.runtime.cli '
        f'--catalog "{catalog}"'
    )
    base.text(
        root / "operator/START.md",
        f"""
这是隔离合成会计办理。你作为会计AI处理老板资料，不修改源码、不读测试或private目录。
仅使用本目录提供的真实CLI连接，禁止连接默认MCP或其他公司目录。不读取SQLite或实现猜字段。
工作规则来自当前安装包公开指南，可用仓库虚拟环境通过ai_accounting_modular.agent.public的read_guide和guide_index读取；入口先读work及当前阶段规则，按实际办理读取专项规则。输入合同用真实accounting_schema按command/kinds取得。
命令前缀（PowerShell）：{prefix}
例如 前缀 call accounting_context --input <绝对JSON请求路径>。
请求和你的答复写在operator目录内。用公开接口发现公司与已有内容，再处理messages/01.md；之后只在收到下一轮消息后继续。
运行时已完成隔离登录，无需请求真实负责人登录。确需老板答复时如实提出剩余问题并结束该轮；不能预读后续材料。
无需交付整月报表或关账；以老板实际交办范围和当前程序规则处理。最后如实报告已办与未办内容。
""",
    )
    return {"root": str(root), "operator_entry": str(root / "operator/START.md")}


def main():
    if Path(sys.prefix).resolve() != (base.REPO / ".tmp-kernel-venv").resolve():
        raise ValueError("Use the repository virtual environment")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    print(prepare(args.output))


if __name__ == "__main__":
    main()
