"""Isolated MCP dialogue cases for resolved questions and business continuation.

Reuse the staged harness's production HTTP/MCP relay. The tested agent receives
only seed metadata and requested tool responses, never the evaluator's answers.
"""

from __future__ import annotations

import base64

import reading_agent_harness as reading
import staged_accounting_agent_harness as staged
import work_draft_agent_harness as transport
from opening_fixture import adopt_empty_opening
from test_payroll import actual


def seed(app, token, directory):
    info = staged.seed(app, token, directory)
    company = info["company_id"]

    def command(command_name, **payload):
        return app.dispatch(command_name, payload, session_token=token)

    def evidence(name, text, media_type="text/plain"):
        return command(
            "evidence",
            company_id=company,
            name=name,
            media_type=media_type,
            content_base64=base64.b64encode(text.encode("utf-8")).decode(),
            request_id="reuse-proof-" + name,
        )["digest"]

    adopt_empty_opening(app.engine(company), period=info["period"])
    contribution_proof = evidence(
        "已提供实际社保.txt",
        "合成员工甲2026年1月养老金已经申报：个人1000元、单位2000元。"
        "本月不参与公积金。这里只确认承担额与申报，不确认实际银行缴款。",
    )
    contribution = actual().model_copy(update={"employee_id": info["employee_id"]})
    contribution_payload = {
        "company_id": company,
        "kind": contribution.kind,
        "subject_id": "reuse-actual-contributions",
        "data": contribution.model_dump(mode="json"),
        "evidence": [contribution_proof],
        "expected_revision": 0,
        "request_id": "reuse-save-actual-contributions",
    }
    command("save_fact", **contribution_payload)
    wage_ref = {
        "subject_id": "stage-january-wage",
        "fact_id": info["existing_facts"]["payroll"]["fact_id"],
    }
    command(
        "save_work_draft",
        company_id=company,
        period=info["period"],
        work_area="payroll",
        expected_revision=None,
        draft={
            "questions": [
                {
                    "id": "wage-scheme",
                    "question": "本项工资方案是否确认？",
                    "status": "resolved",
                    "answer": "确认此前提供的本期方案。",
                    "target": {
                        "field": "payroll_confirmation",
                        "period": info["period"],
                        "subject_id": "stage-january-wage",
                        "entity_id": info["employee_id"],
                    },
                    "source_refs": [wage_ref],
                    "disposition_reason": "本月方案已登记，复用原依据。",
                },
                {
                    "id": "wage-tax",
                    "question": "本期应扣个税是否为126元？",
                    "status": "resolved",
                    "answer": "本项最终报酬表应扣个税126元。",
                    "target": {
                        "field": "tax_amount_fen",
                        "period": info["period"],
                        "subject_id": "stage-january-wage",
                    },
                    "source_refs": [{"subject_id": "stage-wage-tax"}],
                    "disposition_reason": "第2步已有本项税依据，尚未正式发布。",
                },
                {
                    "id": "repeated-tax",
                    "question": "为了代发需要再确认税额吗？",
                    "status": "withdrawn",
                    "target": {
                        "field": "tax_amount_fen",
                        "period": info["period"],
                        "subject_id": "stage-january-wage",
                    },
                    "source_refs": [{"subject_id": "stage-wage-tax"}],
                    "disposition_reason": "相同事项已有有效确认，撤回重复问题。",
                },
                {
                    "id": "bank-payment",
                    "question": "工资是否已实际付款？",
                    "status": "deferred",
                    "target": {
                        "field": "actual_payment",
                        "period": info["period"],
                        "subject_id": "stage-january-wage",
                    },
                    "disposition_reason": "当前仅核算发布，实际银行付款另行跟踪。",
                },
            ],
            "pending_requests": [
                {
                    "command": "save_fact",
                    "payload": contribution_payload,
                    "request_id": contribution_payload["request_id"],
                }
            ],
            "resume_note": "第2步工资及个税已有确认，第3步实际社保已提供。社保登记回执待恢复核对。",
            "next_step": "第4步核对本项净额并正式发布；个税导入文件本轮未要求，不阻断本项核算。",
        },
    )
    supplier = command(
        "register_entity",
        company_id=company,
        kind="organization",
        data={"display_name": "合成复用办公商"},
        source="合成服务资料",
        request_id="reuse-supplier",
    )["entity_id"]
    expense_proof = evidence(
        "待复用办公费.csv",
        "项目,金额,所属期,债权人类别\n办公服务,555.00,2026-01,\n",
        "text/csv",
    )
    specification = {
        "format": "csv",
        "columns": [
            {"column": "A", "role": "context", "label": "项目"},
            {"column": "B", "role": "amount", "label": "金额"},
            {"column": "C", "role": "recognition_period", "label": "所属期"},
            {"column": "D", "role": "context", "label": "债权人类别"},
        ],
    }
    expense_source = command(
        "receive_material",
        company_id=company,
        subject_id="reuse-expense-source",
        data={
            "period": info["period"],
            "category": "transactions",
            "purpose": "business",
            "evidence_digest": expense_proof,
            "specification": specification,
        },
        evidence=[expense_proof],
        expected_revision=0,
        request_id="reuse-expense-material",
    )
    candidate = {
        "id": "reuse-expense",
        "command": "save_fact",
        "payload": {
            "company_id": company,
            "kind": "expense",
            "subject_id": "reuse-expense",
            "data": {
                "period": info["period"],
                "counterparty_id": supplier,
                "amount_fen": 55500,
                "expense_class": "administration",
            },
            "evidence": [expense_proof],
            "expected_revision": 0,
            "request_id": "reuse-expense-save",
            "source_locations": [
                {
                    "source_id": "reuse-expense-source",
                    "source_fact_id": expense_source["fact_id"],
                    "evidence_digest": expense_proof,
                    "location": "CSV!B2",
                }
            ],
        },
    }
    command(
        "save_work_draft",
        company_id=company,
        period=info["period"],
        work_area="transactions",
        expected_revision=None,
        draft={
            "candidates": [candidate],
            "questions": [
                {
                    "id": "expense-creditor",
                    "question": "办公费555元的债权人是什么类别？",
                    "answer": "是供应商合成复用办公商，不是员工。",
                    "status": "answered",
                    "target": {
                        "field": "creditor_kind",
                        "period": info["period"],
                        "subject_id": "reuse-expense",
                        "entity_id": supplier,
                        "candidate_id": "reuse-expense",
                        "fact_kind": "expense",
                    },
                    "source_refs": [
                        {
                            "source_id": "reuse-expense-source",
                            "evidence_digest": expense_proof,
                            "location": "CSV!B2",
                        }
                    ],
                },
            ],
            "resume_note": "第5步办公服务；负责人提前答复已保存，尚未转成正式事实。",
            "next_step": "保全原答复、补齐有源候选，核验后登记并发布本项费用。",
        },
    )
    info.update(
        {
            "instruction": "隔离合成公司当前恢复到第4步薪资核算，或第5步明确的单项办公费。",
            "payroll_subject_id": "stage-january-wage",
            "expense_subject_id": "reuse-expense",
        }
    )
    reading.write_json(directory / "seed.json", info)
    reading.write_json(
        directory / "reuse-evaluator.json",
        {
            "driver_only": True,
            "payroll_net_fen": 887400,
            "expense_amount_fen": 55500,
            "repeat_questions_expected": 0,
            "bank_payment_confirmed": False,
        },
    )
    return info


def parameters(directory, *, source_dir=None, resume=False, instrument=False, cache_build_id=None):
    return reading.parameters(
        directory, source_dir=source_dir, resume=resume, instrument=instrument,
        cache_build_id=cache_build_id, entry_script=__file__,
    )


def main():
    reading.main(
        seed_factory=seed, workload_factory=transport.Workload,
        parameters_factory=parameters, relay_factory=transport.relay,
    )


if __name__ == "__main__":
    main()
