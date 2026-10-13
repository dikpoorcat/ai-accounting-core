"""Isolated production MCP relay for staged accounting dialogue evaluation.

The public seed contains only already supplied synthetic facts and source references.
evaluator-owner-turns.json is for the human-dialogue driver, never the tested agent.
Transport, production service startup, identity-preserving restart and counters reuse
the existing working-draft harness. No existing local service or company is opened.
"""

from __future__ import annotations

import base64
import sys
from pathlib import Path

import reading_agent_harness as reading
import work_draft_agent_harness as transport

write_json = reading.write_json


def seed(app, token, directory):
    from test_payroll import contribution_policy, payroll, profile

    from ai_accounting.kernel.payroll_preparation import PayrollPlan
    from ai_accounting.kernel.remuneration_tax_basis import RemunerationTaxBasis

    calls = []

    def command(command_name, **payload):
        result = app.dispatch(command_name, payload, session_token=token)
        calls.append({"command": command_name, "payload": payload, "result": result})
        return result

    def evidence(name, text, media_type="text/plain"):
        raw = text.encode("utf-8-sig" if media_type == "text/csv" else "utf-8")
        result = command(
            "evidence", company_id=company,
            content_base64=base64.b64encode(raw).decode(), media_type=media_type,
            name=name, request_id="seed-proof-" + str(len(calls)),
        )
        return result["digest"]

    def material(subject, category, name, text, columns):
        proof = evidence(name, text, "text/csv")
        specification = {"format": "csv", "columns": columns}
        result = command(
            "receive_material", company_id=company, subject_id=subject,
            data={"period": "2026-01", "category": category, "purpose": "business",
                  "evidence_digest": proof, "specification": specification},
            evidence=[proof], expected_revision=0, request_id="seed-" + subject,
        )
        return {"source_id": subject, "source_fact_id": result["fact_id"],
                "evidence_digest": proof, "name": name, "specification": specification}

    def fact(subject, value, proof):
        return command(
            "save_fact", company_id=company, kind=value.kind, subject_id=subject,
            data=value.model_dump(mode="json"), evidence=[proof], expected_revision=0,
            request_id="seed-fact-" + subject,
        )

    companies = [
        command("create_company", taxpayer_id=taxpayer, name=name)
        for taxpayer, name in (
            ("91310000123456789A", "合成分阶段甲公司"),
            ("91310000123456789B", "合成分阶段乙公司"),
        )
    ]
    company = companies[0]["id"]
    note = (
        "本公司仅用于隔离合成会计对话。办理日期2026年2月25日，处理2026年1月。"
        "资料陆续提供；当前已提供一份银行流水和工资、普通业务资料。"
        "流水四笔用途原件未写明。未提供资料不表示无业务，不推定外部事项已完成。"
    )
    note_proof = evidence("已提供公司说明.txt", note)
    command("update_company_note", company_id=company, text=note,
            expected_revision=0, evidence_digest=note_proof, request_id="seed-company-note")
    command("update_company_note", company_id=companies[1]["id"],
            text="本公司仅用于合成公司切换测试，业务资料尚未提供。",
            expected_revision=0, request_id="seed-other-note")
    account_proof = evidence(
        "合成银行账户与流水说明.txt",
        "合成分阶段甲公司账户：合成银行，账号6222000000000001。2026年1月"
        "流水期初10000元、期末9000元，四笔分别支出100、200、300、400元。",
    )
    account = command(
        "register_entity", company_id=company, kind="fund_account", account_type="bank",
        data={"display_name": "合成公司银行", "bank_name": "合成银行",
              "bank_account_number": "6222000000000001",
              "account_holder_name": "合成分阶段甲公司"},
        source="合成银行账户与流水说明", evidence_digest=account_proof,
        request_id="seed-bank-account",
    )["entity_id"]
    bank = material(
        "stage-bank-source", "bank", "合成四笔流水.csv",
        "日期,收支金额,所属期,参考号,对方,摘要\n"
        "2026-01-05,-100.00,2026-01,BANK-1,合成对手甲,\n"
        "2026-01-06,-200.00,2026-01,BANK-2,合成对手乙,\n"
        "2026-01-07,-300.00,2026-01,BANK-3,合成对手丙,\n"
        "2026-01-08,-400.00,2026-01,BANK-4,合成对手丁,\n",
        [
            {"column": "A", "role": "context", "label": "日期"},
            {"column": "B", "role": "amount", "label": "收支金额"},
            {"column": "C", "role": "recognition_period", "label": "所属期"},
            {"column": "D", "role": "context", "label": "参考号"},
            {"column": "E", "role": "context", "label": "对方"},
            {"column": "F", "role": "context", "label": "摘要"},
        ],
    )
    from ai_accounting.kernel.domains.banking import BankEntry, BankStatement

    statement = fact(
        "stage-bank-statement", BankStatement(
            period="2026-01", bank_account_id=account, opening_fen=1000000, closing_fen=900000,
            entries=tuple(BankEntry(reference=f"BANK-{i}", actual_date=f"2026-01-{i + 4:02}",
                                    signed_fen=-i * 10000) for i in range(1, 5)),
        ), bank["evidence_digest"],
    )
    wage_source = material(
        "stage-payroll-source", "payroll", "提前提供工资资料.csv",
        "人员,工资所属期,税前工资,费用类别,扣税依据\n"
        "合成员工甲,2026-01,10000.00,management,本项工资最终报酬表应扣税126.00元\n",
        [
            {"column": "A", "role": "context", "label": "人员"},
            {"column": "B", "role": "recognition_period", "label": "工资所属期"},
            {"column": "C", "role": "amount", "label": "税前工资"},
            {"column": "D", "role": "context", "label": "费用类别"},
            {"column": "E", "role": "context", "label": "扣税依据"},
        ],
    )
    wage_proof = evidence(
        "既有合成工资方案.txt",
        "负责人此前明确确认：合成员工甲2026年1月税前工资10000元，为管理费用；"
        "养老金个人和单位承担额必须采用本月实际明细，不能暂用政策推算。"
        "本项工资的最终报酬表应扣个人所得税126元，税期为2026年1月。"
        "本月实际社保、公积金明细尚未提供；本方案不确认申报、缴款或工资支付完成。",
    )
    employee = command(
        "register_worker", company_id=company,
        data={"display_name": "合成员工甲", "action": "hire", "effective_date": "2026-01"},
        source="既有合成工资方案", evidence_digest=wage_proof, request_id="seed-worker",
    )["entity_id"]
    fact("stage-payroll-profile", profile(employee_id=employee), wage_proof)
    fact("stage-contributions-policy", contribution_policy(), wage_proof)
    wage = payroll(
        employee_id=employee, profile_id="stage-payroll-profile",
        contribution_policy_id="stage-contributions-policy", contribution_basis="actual_required",
    )
    wage_saved = fact("stage-january-wage", wage, wage_proof)
    fact("stage-january-plan", PayrollPlan(
        period="2026-01", payroll_revision={"subject_id": "stage-january-wage", "revision": 1},
        change_notice_revisions=(),
    ), wage_proof)
    fact("stage-wage-tax", RemunerationTaxBasis(
        period="2026-01", source_kind="payroll", source_id="stage-january-wage",
        source_fact_id=wage_saved["fact_id"], tax_period="2026-01", tax_amount_fen=12600,
        basis_type="final_statement", confirmed=True,
    ), wage_proof)
    transactions = material(
        "stage-transactions-source", "transactions", "提前提供办公服务资料.csv",
        "项目,金额,所属期,往来方,费用类别,债权人类别\n"
        "办公服务,555.00,2026-01,合成办公商,administration,\n",
        [
            {"column": "A", "role": "context", "label": "项目"},
            {"column": "B", "role": "amount", "label": "金额"},
            {"column": "C", "role": "recognition_period", "label": "所属期"},
            {"column": "D", "role": "context", "label": "往来方"},
            {"column": "E", "role": "context", "label": "费用类别"},
            {"column": "F", "role": "context", "label": "债权人类别"},
        ],
    )
    known = {
        "synthetic": True, "catalog_id": app.security.catalog_instance_id,
        "companies": companies, "company_id": company, "period": "2026-01",
        "as_of": "2026-02-25", "bank_account_id": account, "employee_id": employee,
        "sources": [bank, wage_source, transactions],
        "existing_facts": {"statement": statement, "payroll": wage_saved},
        "initial_message": (
            "开始给合成分阶段甲公司做2026年1月的账，今天是2026年2月25日。"
            "银行流水、工资和办公服务资料已经提供，可以先看。按阶段办理，资料不够时再问我。"
        ),
    }
    write_json(directory / "seed.json", known)
    write_json(directory / "seed-transcript.json", calls)
    write_json(directory / "evaluator-owner-turns.json", {
        "driver_only": True,
        "bank_answers": {
            "BANK-1": "1月5日100元是本月办公服务付款，对方合成对手甲是供应商。",
            "BANK-2": "1月6日200元是归还合成对手乙此前替公司垫付的办公费。",
            "BANK-3": "1月7日300元是支付给合成对手丙的本月仓储服务费。",
            "BANK-4": "1月8日400元是归还合成对手丁的股东借款本金。",
        },
        "pause": "先停一下，请保存现在已核对的内容和还没有回答的问题，稍后继续。",
        "resume": "继续刚才合成分阶段甲公司2026年1月的工作，已答过的内容请接着使用。",
        "payroll_dependency": (
            "本月工资方案就是已提供的那份，实际养老金明细我还没拿到；"
            "不要按政策估算，先记下待办。"
        ),
        "contribution_answer": (
            "2026年1月合成员工甲的实际养老金已申报："
            "个人承担1000元，单位承担2000元，本月公积金未参与。"
            "这份明细确认的是承担额与申报，银行是否已经缴款还未确认。"
        ),
        "transaction_answer": "办公服务555元的债权人是供应商合成办公商，不是员工。",
    })
    return known


def parameters(directory, *, source_dir=None, resume=False, instrument=False, cache_build_id=None):
    from mcp import StdioServerParameters

    args = ["-X", "utf8", str(Path(__file__).resolve()), "mcp", str(directory)]
    for flag, value in (("--source-dir", source_dir), ("--cache-build-id", cache_build_id)):
        if value is not None:
            args.extend([flag, str(value)])
    if resume:
        args.append("--resume")
    if instrument:
        args.append("--instrument")
    return StdioServerParameters(command=sys.executable, args=args)


reading.seed = seed
reading.parameters = parameters
transport.parameters = parameters
reading.relay = transport.relay


if __name__ == "__main__":
    reading.main()
