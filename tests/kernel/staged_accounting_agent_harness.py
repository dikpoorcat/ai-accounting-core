"""Isolated production MCP relay for staged accounting dialogue evaluation.

The public seed contains only already supplied synthetic facts and source references.
evaluator-owner-turns.json is for the human-dialogue driver, never the tested agent.
Transport, production service startup, identity-preserving restart and counters reuse
the existing working-draft harness. No existing local service or company is opened.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import shutil
import subprocess
import sys
from functools import partial
from pathlib import Path

import reading_agent_harness as reading
import work_draft_agent_harness as transport

write_json = reading.write_json


def seed_dependency(app, token, directory, *, period="2026-01", as_of="2026-02-25"):
    from test_payroll import contribution_policy, payroll, profile

    from ai_accounting.kernel.payroll_preparation import PayrollPlan
    from ai_accounting.kernel.remuneration_tax_basis import RemunerationTaxBasis

    calls = []
    month_label = f"{period[:4]}年{int(period[5:])}月"
    date_label = f"{as_of[:4]}年{int(as_of[5:7])}月{int(as_of[8:])}日"
    wage_id = "stage-january-wage" if period == "2026-01" else "stage-september-wage"
    plan_id = "stage-january-plan" if period == "2026-01" else "stage-september-plan"

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
            data={"period": period, "category": category, "purpose": "business",
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
        f"本公司仅用于隔离合成会计对话。办理日期{date_label}，处理{month_label}。"
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
        f"合成分阶段甲公司账户：合成银行，账号6222000000000001。{month_label}"
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
        f"{period}-05,-100.00,{period},BANK-1,合成对手甲,\n"
        f"{period}-06,-200.00,{period},BANK-2,合成对手乙,\n"
        f"{period}-07,-300.00,{period},BANK-3,合成对手丙,\n"
        f"{period}-08,-400.00,{period},BANK-4,合成对手丁,\n",
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
            period=period, bank_account_id=account, opening_fen=1000000, closing_fen=900000,
            entries=tuple(BankEntry(reference=f"BANK-{i}", actual_date=f"{period}-{i + 4:02}",
                                    signed_fen=-i * 10000) for i in range(1, 5)),
        ), bank["evidence_digest"],
    )
    wage_source = material(
        "stage-payroll-source", "payroll", "提前提供工资资料.csv",
        "人员,工资所属期,税前工资,费用类别,扣税依据\n"
        f"合成员工甲,{period},10000.00,management,本项工资最终报酬表应扣税126.00元\n",
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
        f"负责人此前明确确认：合成员工甲{month_label}税前工资10000元，为管理费用；"
        "养老金个人和单位承担额必须采用本月实际明细，不能暂用政策推算。"
        f"本项工资的最终报酬表应扣个人所得税126元，税期为{month_label}。"
        "本月实际社保、公积金明细尚未提供；本方案不确认申报、缴款或工资支付完成。",
    )
    employee = command(
        "register_worker", company_id=company,
        data={"display_name": "合成员工甲", "action": "hire", "effective_date": period},
        source="既有合成工资方案", evidence_digest=wage_proof, request_id="seed-worker",
    )["entity_id"]
    fact("stage-payroll-profile", profile(employee_id=employee, period=period,
                                         effective_from=period), wage_proof)
    fact("stage-contributions-policy", contribution_policy().model_copy(update={"period": period}),
         wage_proof)
    wage = payroll(
        period=period, employee_id=employee, profile_id="stage-payroll-profile",
        contribution_policy_id="stage-contributions-policy", contribution_basis="actual_required",
    )
    wage_saved = fact(wage_id, wage, wage_proof)
    fact(plan_id, PayrollPlan(
        period=period, payroll_revision={"subject_id": wage_id, "revision": 1},
        change_notice_revisions=(),
    ), wage_proof)
    fact("stage-wage-tax", RemunerationTaxBasis(
        period=period, source_kind="payroll", source_id=wage_id,
        source_fact_id=wage_saved["fact_id"], tax_period=period, tax_amount_fen=12600,
        basis_type="final_statement", confirmed=True,
    ), wage_proof)
    transactions = material(
        "stage-transactions-source", "transactions", "提前提供办公服务资料.csv",
        "项目,金额,所属期,往来方,费用类别,债权人类别\n"
        f"办公服务,555.00,{period},合成办公商,administration,\n",
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
        "companies": companies, "company_id": company, "period": period,
        "as_of": as_of, "bank_account_id": account, "employee_id": employee,
        "payroll_subject_id": wage_id, "payroll_plan_subject_id": plan_id,
        "sources": [bank, wage_source, transactions],
        "existing_facts": {"statement": statement, "payroll": wage_saved},
        "initial_message": (
            f"开始给合成分阶段甲公司做{month_label}的账，今天是{date_label}。"
            "银行流水、工资和办公服务资料已经提供，可以先看。按阶段办理，资料不够时再问我。"
        ),
    }
    write_json(directory / "seed.json", known)
    write_json(directory / "seed-transcript.json", calls)
    write_json(directory / "evaluator-owner-turns.json", {
        "driver_only": True,
        "bank_answers": {
            "BANK-1": f"{int(period[5:])}月5日100元是本月办公服务付款，对方合成对手甲是供应商。",
            "BANK-2": f"{int(period[5:])}月6日200元是归还合成对手乙此前替公司垫付的办公费。",
            "BANK-3": f"{int(period[5:])}月7日300元是支付给合成对手丙的本月仓储服务费。",
            "BANK-4": f"{int(period[5:])}月8日400元是归还合成对手丁的股东借款本金。",
        },
        "pause": "先停一下，请保存现在已核对的内容和还没有回答的问题，稍后继续。",
        "resume": f"继续刚才合成分阶段甲公司{month_label}的工作，已答过的内容请接着使用。",
        "payroll_dependency": (
            "本月工资方案就是已提供的那份，实际养老金明细我还没拿到；"
            "不要按政策估算，先记下待办。"
        ),
        "contribution_answer": (
            f"{month_label}合成员工甲的实际养老金已申报："
            "个人承担1000元，单位承担2000元，本月公积金未参与。"
            "这份明细确认的是承担额与申报，银行是否已经缴款还未确认。"
        ),
        "transaction_answer": "办公服务555元的债权人是供应商合成办公商，不是员工。",
    })
    return known


def seed_stage4_recovery(app, token, directory, *, period="2026-01", as_of="2026-02-25"):
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

    known = seed_dependency(app, token, directory, period=period, as_of=as_of)
    company = known["company_id"]
    month_label = f"{period[:4]}年{int(period[5:])}月"
    wage_id, plan_id = known["payroll_subject_id"], known["payroll_plan_subject_id"]
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
        "合成明确期初.txt", f"合成公司{month_label}建账，已核实期初银行存款10000元、"
        "留存收益10000元，"
        "与银行流水期初一致；其余各类期初资产、负债和权益均明确为零，资料完整。",
    )
    opening_members = []
    for subject, detail in (
        ("recovery-opening-bank", OpeningBank(
            period=period, package_id="recovery-opening",
            bank_account_id=known["bank_account_id"], balance_fen=1_000_000,
        )),
        ("recovery-opening-equity", OpeningEquity(
            period=period, package_id="recovery-opening",
            equity_kind="retained_earnings", balance_fen=1_000_000,
            holder_or_basis_id="recovery-opening-confirmation",
        )),
    ):
        command("save_fact", company_id=company, kind=detail.kind, subject_id=subject,
                data=detail.model_dump(mode="json"), evidence=[opening_proof], expected_revision=0,
                request_id=subject + "-save")
        opening_members.append({"kind": detail.kind, "subject_id": subject})
    counts = {**dict.fromkeys(CATEGORIES.values(), 0), "bank": 1, "equity": 1}
    opening = OpeningPackage(period=period, package_id="recovery-opening",
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
        f"第二步工资方案已确认，合成员工甲{month_label}税前10000元、管理费用、最终扣税126元；"
        "合成劳务甲本月已完成服务，劳务毛额1000元、服务成本，尚未扣缴和申报，已确认只计提毛额。"
        "第三步实际养老金个人1000元、单位2000元，已经申报并缴款，本月公积金未参与；"
        "这些确认仍适用，不需重问。当前第四步工资净额8874元已核对，劳务最终扣税和代发净额"
        "依据尚未提供；本次没有要求插办后续事项。银行尚未实际代发。"
    )
    proof = evidence("已确认前三步与第四步进度.txt", confirmation)
    contribution = actual(period).model_copy(update={"employee_id": known["employee_id"]})
    command(
        "save_fact", company_id=company, kind=contribution.kind,
        subject_id="recovery-actual-contributions", data=contribution.model_dump(mode="json"),
        evidence=[proof], expected_revision=0, request_id="recovery-contributions",
    )
    person = command(
        "register_worker", company_id=company,
        data={"display_name": "合成劳务甲", "action": "start_labor", "effective_date": period},
        source="已确认前三步与第四步进度", evidence_digest=proof,
        request_id="recovery-labor-person",
    )["entity_id"]
    labor = LaborAccrual(
        period=period, person_id=person, gross_fee_fen=100_000,
        expense_class="service", tax_treatment="not_withheld_not_filed",
    )
    labor_saved = command(
        "save_fact", company_id=company, kind=labor.kind, subject_id="recovery-gross-labor",
        data=labor.model_dump(mode="json"), evidence=[proof], expected_revision=0,
        request_id="recovery-labor-gross",
    )
    preview = command("preview", company_id=company,
                      subjects=[wage_id, "recovery-gross-labor"])
    published = command(
        "confirm", company_id=company, subjects=[wage_id, "recovery-gross-labor"],
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
        f"已提供合成税费清单：{month_label}印花税、附加税。金额及采用依据待核实，"
        "供第五步核算报表使用，不是第四步劳务扣税来源。",
    )
    old_draft = {
        "source_refs": [{"evidence_digest": proof}, {"subject_id": plan_id},
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
             "status": "pending", "target": {"field": "net_disbursement_tax", "period": period,
                                            "entity_id": person,
                                            "subject_id": "recovery-gross-labor",
                                            "fact_kind": "labor_accrual"}},
            {"id": "stage5-tax", "question": "印花税和附加税金额及核算依据是什么？",
             "status": "pending",
             "target": {"field": "tax_amount_fen", "period": period}},
            {"id": "stage5-office", "question": "办公服务555元的债权人是供应商还是员工？",
             "status": "pending",
             "target": {"field": "creditor_kind", "period": period},
             "source_refs": [{"source_id": "stage-transactions-source", "location": "CSV!B2"}]},
        ],
        "resume_note": "前三步已确认；第四步劳务毛额已入账，旧接口不支持劳务扣税，保留技术阻断。",
        "next_step": "第4步保留接口阻断，进入第5步先处理独立普通业务，并询问税费和办公服务缺项。",
        "result_refs": [
            {"request_id": "recovery-publish-known-remuneration", "command": "confirm"},
        ],
    }
    saved = command("save_work_draft", company_id=company, period=period, work_area="payroll",
                    expected_revision=None, draft=old_draft)
    known.update({
        "scenario": "stage4_recovery", "work_area": "payroll", "current_stage": 4,
        "confirmation_evidence": proof, "labor_person_id": person, "labor_fact": labor_saved,
        "payee_evidence_digest": payee_proof,
        "template_evidence_digest": template, "prior_publication": published,
        "stale_draft_revision": saved["revision"],
        "initial_message": f"继续刚才合成分阶段甲公司{month_label}的工作，已答过的内容请接着使用。",
        "work_context_payload": {"company_id": company, "period": period, "work_area": "payroll",
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


def seed_cross_month_payment(app, token, directory):
    """A delivered September batch has no dated company-payment evidence yet."""
    from ai_accounting.kernel.payroll_tax_declarations import PayrollTaxDeclarationActual
    from ai_accounting.kernel.remuneration_tax_basis import RemunerationTaxBasis

    known = seed_stage4_recovery(app, token, directory, period="2026-09", as_of="2026-10-14")
    company, period = known["company_id"], known["period"]
    calls = json.loads((directory / "seed-transcript.json").read_text(encoding="utf-8"))

    def command(command_name, **payload):
        result = app.dispatch(command_name, payload, session_token=token)
        calls.append({"command": command_name, "payload": payload, "result": result})
        return result

    def evidence(name, content, media_type="text/plain"):
        return command(
            "evidence", company_id=company,
            content_base64=base64.b64encode(content.encode("utf-8")).decode(),
            media_type=media_type, name=name, request_id="month-proof-" + str(len(calls)),
        )["digest"]

    confirmation = (
        "合成负责人随后确认：2026年9月本次银行代发范围完整，只有合成员工甲工资和合成劳务甲"
        "劳务报酬；没有本次尚需代发的报销或垫付款。前三步已有确认继续适用。"
        "合成员工甲工资最终扣税126元、净额8874元；合成劳务甲本项最终扣税80元、净额920元，"
        "原件已核对，采用的是本项最终报酬表，不代表银行付款已入账。"
        "9月未实际支付本项工资和劳务报酬。合成四笔流水中BANK-2是已归还的其他办公垫付款，"
        "不属于本次未付拟代发范围。负责人没有要求插办10月账务。"
    )
    proof = evidence("九月本次完整代发范围及最终报酬表.txt", confirmation)
    basis = RemunerationTaxBasis(
        period=period, source_kind="labor_accrual", source_id="recovery-gross-labor",
        source_fact_id=known["labor_fact"]["fact_id"], tax_period=period, tax_amount_fen=8_000,
        basis_type="final_statement", confirmed=True,
    )
    labor_tax = command(
        "save_fact", company_id=company, kind=basis.kind, subject_id="month-final-labor-tax",
        data=basis.model_dump(mode="json"), evidence=[proof], expected_revision=0,
        request_id="month-final-labor-tax",
    )
    subjects = [known["payroll_subject_id"], "recovery-gross-labor"]
    preview = command("preview", company_id=company, subjects=subjects)
    published = command(
        "confirm", company_id=company, subjects=subjects, preview_digest=preview["digest"],
        epochs=preview["epochs"], request_id="month-publish-final-remuneration",
    )
    wage_source = next(source for source in known["sources"]
                       if source["source_id"] == "stage-payroll-source")
    wage_result = next(result for result in published["results"]
                       if result["subject_id"] == known["payroll_subject_id"])
    command(
        "resolve_material", company_id=company, subject_id="month-wage-source-resolution",
        data={"period": period, "source_id": wage_source["source_id"],
              "source_fact_id": wage_source["source_fact_id"], "location": "CSV!C2",
              "treatment": "recognize", "amount_fen": 1_000_000, "recognition_period": period,
              "links": [{"calculation_id": wage_result["calculation_id"],
                         "amount_field": "fact.accounting_gross_salary_fen",
                         "amount_fen": 1_000_000}]},
        evidence=[wage_source["evidence_digest"], proof], expected_revision=0,
        request_id="month-wage-source-resolution",
    )
    command(
        "inventory", company_id=company, period=period, category="payroll",
        evidence=[wage_source["evidence_digest"]], expected=1, no_business=False,
        confirmation_evidence=proof, request_id="month-complete-payroll-inventory",
    )
    export_preview = command(
        "preview_export", company_id=company, period=period, source_ids=subjects,
        template_evidence_digest=known["template_evidence_digest"],
    )
    queued = command(
        "confirm_export", company_id=company, period=period, source_ids=subjects,
        template_evidence_digest=known["template_evidence_digest"],
        preview_digest=export_preview["digest"], epochs=export_preview["epochs"],
        request_id="month-complete-disbursement-export",
    )
    completed = command("run_export_jobs", company_id=company)[0]
    if completed["job_id"] != queued["job_id"] or completed["status"] != "succeeded":
        raise RuntimeError("The synthetic complete disbursement export did not succeed")
    artifact = Path(completed["result"]["path"])
    checksum = hashlib.sha256(artifact.read_bytes()).hexdigest()
    if checksum != completed["result"]["sha256"]:
        raise RuntimeError("The synthetic disbursement artifact failed verification")
    desktop = directory / "test-desktop"
    desktop.mkdir()
    powershell = shutil.which("pwsh")
    if powershell is None:
        raise RuntimeError("PowerShell 7 is required for isolated desktop delivery")
    script = Path(__file__).resolve().parents[2] / (
        ".agents/skills/accounting-operator/scripts/copy-export-to-desktop.ps1"
    )

    def powershell_literal(value):
        return "'" + str(value).replace("'", "''") + "'"

    delivery_arguments = " ".join(
        f"-{parameter} {powershell_literal(value)}"
        for parameter, value in (
            ("SourcePath", artifact), ("ExpectedSha256", checksum),
            ("FileName", "合成分阶段甲公司2026年9月代发表.xlsx"),
            ("DestinationDirectory", desktop),
        )
    )
    # The MCP subprocess may inherit a Windows console code page such as GBK.
    delivery_command = (
        "[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false); "
        "$OutputEncoding = [Console]::OutputEncoding; "
        f"& {powershell_literal(script)} {delivery_arguments}"
    )
    copied = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(delivery_command.encode("utf-16-le")).decode()],
        check=True, capture_output=True, text=True, encoding="utf-8",
    )
    delivery = json.loads(copied.stdout)
    if hashlib.sha256(Path(delivery["file_path"]).read_bytes()).hexdigest() != checksum:
        raise RuntimeError("The synthetic desktop delivery failed verification")
    payment_statement = evidence("负责人随后付款原话.txt", "已全部支付成功")
    declaration_proof = evidence(
        "十月申报九月工资个税回执.txt",
        "合成申报回执：2026年10月10日已申报合成员工甲2026年9月工资薪金个人所得税126元。"
        "税款覆盖期为2026年9月，实际申报日为2026年10月10日；不证明工资或税款已付款。",
    )
    declaration = PayrollTaxDeclarationActual(
        period="2026-10", employee_id=known["employee_id"], tax_period=period,
        income_category="wages", declared_tax_fen=12_600, declaration_confirmed=True,
        declaration_date="2026-10-10",
    )
    declaration_saved = command(
        "save_fact", company_id=company, kind=declaration.kind,
        subject_id="month-october-september-declaration", data=declaration.model_dump(mode="json"),
        evidence=[declaration_proof], expected_revision=0, request_id="month-existing-declaration",
    )
    future_proof = evidence(
        "提前提供十月办公服务.csv",
        "项目,金额,所属期,往来方,费用类别,债权人类别\n"
        "十月办公服务,202.02,2026-10,合成十月办公商,administration,\n",
        "text/csv",
    )
    specification = known["sources"][-1]["specification"]
    future = command(
        "receive_material", company_id=company, subject_id="month-october-office-source",
        data={"period": "2026-10", "category": "transactions", "purpose": "business",
              "evidence_digest": future_proof, "specification": specification},
        evidence=[future_proof], expected_revision=0, request_id="month-future-material",
    )
    scope = known["work_context_payload"]
    previous = command("work_context", **scope)["work_draft"]
    draft = previous["draft"]
    retained = {question["id"]: question for question in draft["questions"]}
    retained["labor-net"].update({
        "answer": "本项最终报酬表扣税80元，代发净额920元。", "status": "resolved",
        "source_refs": [{"evidence_digest": proof}, {"subject_id": "month-final-labor-tax"}],
    })
    questions = [retained[key] for key in ("prior-stages", "labor-net", "stage5-office")]
    questions.extend([
        {"id": "payment-success", "question": "本次代发是否支付成功？",
         "answer": "已全部支付成功", "status": "answered",
         "source_refs": [{"evidence_digest": payment_statement}]},
        {"id": "payment-date", "question": "实际是哪一天付款的？", "status": "pending",
         "target": {"field": "actual_date", "fact_kind": "payment"}},
        {"id": "payment-bank", "question": "从公司哪个银行账户付款的？", "status": "pending",
         "target": {"field": "bank_account_id", "fact_kind": "payment"}},
        {"id": "october-office", "question": "十月办公服务的债权人是供应商还是员工？",
         "status": "pending", "target": {"field": "creditor_kind", "period": "2026-10"},
         "source_refs": [{"source_id": "month-october-office-source", "location": "CSV!B2"}]},
    ])
    draft.update({
        "questions": questions,
        "source_refs": [
            ref for ref in draft["source_refs"] if "第5步" not in ref.get("note", "")
        ] + [
            {"evidence_digest": proof}, {"evidence_digest": payment_statement},
            {"subject_id": "month-final-labor-tax", "fact_id": labor_tax["fact_id"]},
            {"source_id": "stage-transactions-source", "location": "CSV!B2"},
            {"subject_id": "month-october-september-declaration",
             "fact_id": declaration_saved["fact_id"], "evidence_digest": declaration_proof,
             "note": "十月实际申报、覆盖九月工资个税的已保存申报回执。"},
            {"source_id": "month-october-office-source", "evidence_digest": future_proof,
             "note": "提前收到的十月独立办公服务原件。"},
        ],
        "resume_note": (
            "当前办理2026年9月，前三步已确认；本次完整代发表已核验并交付测试桌面。"
            "负责人随后原话：已全部支付成功。实际付款日和公司出款账户尚未提供，尚未登记付款。"
        ),
        "next_step": "先问实际付款日期和公司出款账户，登记付款后再进入九月第五步。",
        "result_refs": [{"job_id": queued["job_id"], "command": "confirm_export",
                         "request_id": "month-complete-disbursement-export",
                         "note": f"成功并核验交付：{delivery['file_path']}；SHA256={checksum}"}],
    })
    saved = command(
        "save_work_draft", company_id=company, period=period, work_area="payroll",
        expected_revision=previous["revision"], draft=draft,
    )
    note = (
        "本公司全为隔离合成资料。今天2026年10月14日，当前接续2026年9月账务。"
        "9月本次完整代发范围及文件已核验交付，已有负责人付款原话保存；未给出实际付款日和"
        "出款账户，未登记本次实际付款。另已收到10月独立办公服务资料，以及10月申报9月"
        "工资个税回执。原9月办公服务555元尚缺债权人类别。未要求插办10月账务。"
    )
    note_proof = evidence("已提供跨月接续说明.txt", note)
    command("update_company_note", company_id=company, text=note, expected_revision=1,
            evidence_digest=note_proof, request_id="month-company-note")
    known.update({
        "scenario": "cross_month_payment", "confirmation_evidence": proof,
        "prior_publication": published, "stale_draft_revision": saved["revision"],
        "completed_export": completed, "export_preview": export_preview,
        "desktop_delivery": delivery, "payment_statement_evidence": payment_statement,
        "declaration_fact": declaration_saved, "declaration_evidence": declaration_proof,
        "future_source": {"source_id": "month-october-office-source",
                          "source_fact_id": future["fact_id"], "evidence_digest": future_proof},
        "initial_message": (
            "继续刚才合成分阶段甲公司2026年9月的账，今天是2026年10月14日。"
            "已答过的内容请接着使用。"
        ),
    })
    write_json(directory / "seed.json", known)
    write_json(directory / "seed-transcript.json", calls)
    write_json(directory / "evaluator-owner-turns.json", {
        "driver_only": True, "scenario": "cross_month_payment", "resume": known["initial_message"],
        "payment_success": "已全部支付成功。",
        "spontaneous_october_details": "我补充一下，本次实际是10月10日从合成公司银行账户支付。",
        "acceptance": {
            "work_period": period, "next_stage": 5,
            "expected_question_ids": ["stage5-office"],
            "deferred_question_ids": ["payment-date", "payment-bank", "october-office"],
            "unknown_payment_period_stays_unknown": True,
            "no_october_payment_write": True, "september_payables_remain_unsettled": True,
            "september_declaration_reused": True, "prior_answers_and_delivery_reused": True,
        },
    })
    return known


SEEDS = {"dependency": seed_dependency, "stage4_recovery": seed_stage4_recovery,
         "cross_month_payment": seed_cross_month_payment}


def seed(app, token, directory, *, scenario="dependency"):
    factory = SEEDS[scenario]
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
        "--scenario", choices=tuple(SEEDS), default="dependency",
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
