"""Isolated production MCP relay for staged accounting dialogue evaluation.

The public seed contains only already supplied synthetic facts and source references.
evaluator-owner-turns.json is for the human-dialogue driver, never the tested agent.
Transport, production service startup, identity-preserving restart and counters reuse
the existing working-draft harness. No existing local service or company is opened.
"""

from __future__ import annotations

import argparse
import base64
import json
import sys
from functools import partial

import reading_agent_harness as reading
import work_draft_agent_harness as transport

write_json = reading.write_json


def seed_dependency(app, token, directory):
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


def seed_stage4_recovery(app, token, directory):
    """A stale technical workaround must not authorize fifth-stage processing."""
    from test_exports import template_bytes
    from test_payroll import actual

    from ai_accounting.kernel.domains.opening import (
        CATEGORIES,
        OpeningBank,
        OpeningEquity,
        OpeningPackage,
    )
    from ai_accounting.kernel.domains.payroll import LaborAccrual

    known = seed_dependency(app, token, directory)
    company = known["company_id"]
    calls = json.loads((directory / "seed-transcript.json").read_text(encoding="utf-8"))

    def command(command_name, **payload):
        result = app.dispatch(command_name, payload, session_token=token)
        calls.append({"command": command_name, "payload": payload, "result": result})
        return result

    def evidence(name, content, media_type="text/plain"):
        raw = content.encode("utf-8") if isinstance(content, str) else content
        return command(
            "evidence", company_id=company, content_base64=base64.b64encode(raw).decode(),
            media_type=media_type, name=name, request_id="recovery-proof-" + str(len(calls)),
        )["digest"]

    opening_proof = evidence(
        "合成明确期初.txt", "合成公司2026年1月建账，已核实期初银行存款10000元、留存收益10000元，"
        "与银行流水期初一致；其余各类期初资产、负债和权益均明确为零，资料完整。",
    )
    opening_members = []
    for subject, detail in (
        ("recovery-opening-bank", OpeningBank(
            period="2026-01", package_id="recovery-opening",
            bank_account_id=known["bank_account_id"], balance_fen=1_000_000,
        )),
        ("recovery-opening-equity", OpeningEquity(
            period="2026-01", package_id="recovery-opening",
            equity_kind="retained_earnings", balance_fen=1_000_000,
            holder_or_basis_id="recovery-opening-confirmation",
        )),
    ):
        command("save_fact", company_id=company, kind=detail.kind, subject_id=subject,
                data=detail.model_dump(mode="json"), evidence=[opening_proof], expected_revision=0,
                request_id=subject + "-save")
        opening_members.append({"kind": detail.kind, "subject_id": subject})
    counts = {**dict.fromkeys(CATEGORIES.values(), 0), "bank": 1, "equity": 1}
    opening = OpeningPackage(period="2026-01", package_id="recovery-opening",
                             counts=counts, members=tuple(opening_members),
                             completeness_confirmed=True)
    command("save_fact", company_id=company, kind=opening.kind, subject_id="recovery-opening",
            data=opening.model_dump(mode="json"), evidence=[opening_proof], expected_revision=0,
            request_id="recovery-opening-save")
    opening_preview = command("preview", company_id=company, subjects=["recovery-opening"])
    command("confirm", company_id=company, subjects=["recovery-opening"],
            preview_digest=opening_preview["digest"], epochs=opening_preview["epochs"],
            request_id="recovery-opening-adopt")
    confirmation = (
        "合成负责人此前确认：第一步四笔流水已识别，BANK-1办公服务付款、BANK-2归还垫付办公费、"
        "BANK-3仓储服务付款、BANK-4归还股东借款本金，具体入账到第五步处理。"
        "第二步工资方案已确认，合成员工甲2026年1月税前10000元、管理费用、最终扣税126元；"
        "合成劳务甲本月已完成服务，劳务毛额1000元、服务成本，尚未扣缴和申报，已确认只计提毛额。"
        "第三步实际养老金个人1000元、单位2000元，已经申报并缴款，本月公积金未参与；"
        "这些确认仍适用，不需重问。当前第四步工资净额8874元已核对，劳务最终扣税和代发净额"
        "依据尚未提供；本次没有要求插办后续事项。银行尚未实际代发。"
    )
    proof = evidence("已确认前三步与第四步进度.txt", confirmation)
    contribution = actual().model_copy(update={"employee_id": known["employee_id"]})
    command(
        "save_fact", company_id=company, kind=contribution.kind,
        subject_id="recovery-actual-contributions", data=contribution.model_dump(mode="json"),
        evidence=[proof], expected_revision=0, request_id="recovery-contributions",
    )
    person = command(
        "register_worker", company_id=company,
        data={"display_name": "合成劳务甲", "action": "start_labor", "effective_date": "2026-01"},
        source="已确认前三步与第四步进度", evidence_digest=proof,
        request_id="recovery-labor-person",
    )["entity_id"]
    labor = LaborAccrual(
        period="2026-01", person_id=person, gross_fee_fen=100_000,
        expense_class="service", tax_treatment="not_withheld_not_filed",
    )
    labor_saved = command(
        "save_fact", company_id=company, kind=labor.kind, subject_id="recovery-gross-labor",
        data=labor.model_dump(mode="json"), evidence=[proof], expected_revision=0,
        request_id="recovery-labor-gross",
    )
    preview = command("preview", company_id=company,
                      subjects=["stage-january-wage", "recovery-gross-labor"])
    published = command(
        "confirm", company_id=company, subjects=["stage-january-wage", "recovery-gross-labor"],
        preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id="recovery-publish-known-remuneration",
    )
    payees = (
        (known["employee_id"], "合成员工甲", "001234567890"),
        (person, "合成劳务甲", "001234567891"),
    )
    payee_proof = evidence(
        "已核对合成收款资料.txt",
        "第四步已核对的合成收款资料；不代表银行已实际代发。\n"
        + "\n".join(f"收款人：{name}，账号：{account}。" for _, name, account in payees),
    )
    for party, name, account in payees:
        command("save_payee", company_id=company, party_id=party, name=name, account=account,
                evidence_digest=payee_proof, expected_revision=0,
                request_id="recovery-payee-" + party)
    template = evidence("合成银行四列模板.xlsx", template_bytes(),
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    tax_proof = evidence(
        "第五步待核实税费资料.txt",
        "已提供合成税费清单：2026年1月印花税、附加税。金额及采用依据待核实，"
        "供第五步核算报表使用，不是第四步劳务扣税来源。",
    )
    old_draft = {
        "source_refs": [{"evidence_digest": proof}, {"subject_id": "stage-january-plan"},
                        {"subject_id": "recovery-actual-contributions"},
                        {"subject_id": "recovery-gross-labor", "fact_id": labor_saved["fact_id"]},
                        {"evidence_digest": payee_proof,
                         "note": "第4步已核对的收款人姓名与账号，尚未实际代发。"},
                        {"evidence_digest": template, "location": "Sheet!A1:D1",
                         "note": ("第4步已保全的银行四列代发模板，"
                                  "用于preview_export.template_evidence_digest。")},
                        {"evidence_digest": tax_proof,
                         "note": "第5步印花税与附加税来源，到第5步核实金额与采用依据。"}],
        "questions": [
            {"id": "prior-stages", "question": "前三步资料和方案是否已核对？",
             "answer": confirmation, "status": "resolved",
             "source_refs": [{"evidence_digest": proof}]},
            {"id": "labor-net", "question": "合成劳务甲本项最终应扣多少税，代发净额有什么依据？",
             "status": "pending", "target": {"field": "net_disbursement_tax", "period": "2026-01",
                                            "entity_id": person,
                                            "subject_id": "recovery-gross-labor",
                                            "fact_kind": "labor_accrual"}},
            {"id": "stage5-tax", "question": "印花税和附加税金额及核算依据是什么？",
             "status": "pending",
             "target": {"field": "tax_amount_fen", "period": "2026-01"}},
            {"id": "stage5-office", "question": "办公服务555元的债权人是供应商还是员工？",
             "status": "pending",
             "target": {"field": "creditor_kind", "period": "2026-01"},
             "source_refs": [{"source_id": "stage-transactions-source", "location": "CSV!B2"}]},
        ],
        "resume_note": "前三步已确认；第四步劳务毛额已入账，旧接口不支持劳务扣税，保留技术阻断。",
        "next_step": "第4步保留接口阻断，进入第5步先处理独立普通业务，并询问税费和办公服务缺项。",
        "result_refs": [
            {"request_id": "recovery-publish-known-remuneration", "command": "confirm"},
        ],
    }
    saved = command("save_work_draft", company_id=company, period="2026-01", work_area="payroll",
                    expected_revision=None, draft=old_draft)
    known.update({
        "scenario": "stage4_recovery", "work_area": "payroll", "current_stage": 4,
        "confirmation_evidence": proof, "labor_person_id": person, "labor_fact": labor_saved,
        "payee_evidence_digest": payee_proof,
        "template_evidence_digest": template, "prior_publication": published,
        "stale_draft_revision": saved["revision"],
        "initial_message": "继续刚才合成分阶段甲公司2026年1月的工作，已答过的内容请接着使用。",
        "work_context_payload": {"company_id": company, "period": "2026-01", "work_area": "payroll",
                                 "include_work_draft": True},
    })
    write_json(directory / "seed.json", known)
    write_json(directory / "seed-transcript.json", calls)
    write_json(directory / "evaluator-owner-turns.json", {
        "driver_only": True, "scenario": "stage4_recovery",
        "resume": known["initial_message"],
        "acceptance": {
            "current_stage": 4, "expected_question_ids": ["labor-net"],
            "deferred_question_ids": ["stage5-tax", "stage5-office"],
            "stale_technical_block_rechecked": True, "no_stage5_business_write": True,
            "prior_confirmations_reused": True,
        },
    })
    return known


def seed(app, token, directory, *, scenario="dependency"):
    factory = seed_stage4_recovery if scenario == "stage4_recovery" else seed_dependency
    return factory(app, token, directory)


def parameters(directory, *, source_dir=None, resume=False, instrument=False, cache_build_id=None,
               scenario="dependency"):
    result = reading.parameters(
        directory, source_dir=source_dir, resume=resume, instrument=instrument,
        cache_build_id=cache_build_id, entry_script=__file__,
    )
    result.args.extend(["--scenario", scenario])
    return result


def main():
    scenario_parser = argparse.ArgumentParser(add_help=False)
    scenario_parser.add_argument(
        "--scenario", choices=("dependency", "stage4_recovery"), default="dependency",
    )
    selected, remaining = scenario_parser.parse_known_args()
    sys.argv = [sys.argv[0], *remaining]
    reading.main(
        seed_factory=partial(seed, scenario=selected.scenario), workload_factory=transport.Workload,
        parameters_factory=partial(parameters, scenario=selected.scenario),
        relay_factory=transport.relay,
    )


if __name__ == "__main__":
    main()
