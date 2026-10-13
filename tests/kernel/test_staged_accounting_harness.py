"""Synthetic staged dialogue seed, preparation and dependency recovery checks.

These tests verify real kernel behavior; conversational question selection is
evaluated by the independent agent through staged_accounting_agent_harness.
"""

from __future__ import annotations

import json

import pytest
import staged_accounting_agent_harness as harness
from test_payroll import actual
from test_work_draft_service import business_state

from ai_accounting.kernel.contracts import NeedsInformation
from ai_accounting.kernel.service import LocalService


@pytest.fixture
def seeded(tmp_path):
    root = tmp_path / "data"
    app = LocalService(root)
    app.security.provision(harness.reading.OWNER, harness.reading.PASSWORD)
    token = app.security.login(harness.reading.OWNER, harness.reading.PASSWORD).session_token
    info = harness.seed(app, token, tmp_path)
    yield app, token, info, tmp_path
    app.close()


def dispatch(app, token, command, **payload):
    return app.dispatch(command, payload, session_token=token)


def test_seed_keeps_all_kernel_issues_and_four_bank_rows_without_disclosing_answers(seeded):
    app, token, info, directory = seeded
    result = dispatch(app, token, "workflow", company_id=info["company_id"],
                      period=info["period"], as_of=info["as_of"])
    unresolved = [issue for issue in result["issues"]
                  if issue.get("source_id") == "stage-bank-source"]
    assert {issue["location"] for issue in unresolved} == {
        "CSV!B2", "CSV!B3", "CSV!B4", "CSV!B5",
    }
    assert {issue["field"] for issue in result["issues"]} >= {
        "materials.bank", "materials.payroll", "materials.transactions",
        "materials.tax", "materials.assets", "materials.financing", "unpublished_payroll",
    }
    public = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
    driver = json.loads((directory / "evaluator-owner-turns.json").read_text(encoding="utf-8"))
    assert public == info
    assert "bank_answers" not in public and "owner_answer_file" not in public
    assert len(driver["bank_answers"]) == 4
    assert not any(answer in json.dumps(public, ensure_ascii=False)
                   for answer in driver["bank_answers"].values())


def test_future_source_preparation_and_draft_do_not_publish_or_clear_complete_checks(seeded):
    app, token, info, _ = seeded
    company = info["company_id"]
    before = business_state(app, company)
    for area in ("payroll", "transactions"):
        context = dispatch(app, token, "work_context", company_id=company,
                           period=info["period"], work_area=area, include_work_draft=True)
        assert context["materials"] and context["work_draft"]["status"] == "absent"
        source = next(item for item in info["sources"] if area in item["source_id"])
        dispatch(app, token, "inspect_material", company_id=company,
                 evidence_digest=source["evidence_digest"], specification=source["specification"])
        dispatch(app, token, "save_work_draft", company_id=company, period=info["period"],
                 work_area=area, expected_revision=None,
                 draft={"source_refs": [{"source_id": source["source_id"],
                                         "evidence_digest": source["evidence_digest"]}],
                        "resume_note": "资料已提前整理，尚未到所属办理步骤。",
                        "next_step": "到本步骤时核对当前来源与正式状态，再询问必要缺项。"})
    assert business_state(app, company) == before
    engine = app.engine(company)
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM calculation_publication").fetchone()[0] == 0
        assert connection.execute("SELECT count(*) FROM voucher").fetchone()[0] == 0
    workflow = dispatch(app, token, "workflow", company_id=company,
                        period=info["period"], as_of=info["as_of"])
    assert any(issue.get("source_id") == "stage-transactions-source"
               for issue in workflow["issues"])


def test_confirmed_stage2_scheme_waits_for_actual_contributions_before_stage4_net_preview(seeded):
    app, token, info, _ = seeded
    engine = app.engine(info["company_id"])
    from test_payroll import context_for

    from ai_accounting.kernel.payroll_confirmation import resolve_payroll_confirmation

    with engine.store.connection(read_only=True) as connection:
        wage = engine.store.current_fact(connection, "stage-january-wage")
        plan = engine.store.current_fact(connection, "stage-january-plan")
        profile = engine.store.current_fact(connection, "stage-payroll-profile")
        policy = engine.store.current_fact(connection, "stage-contributions-policy")
    confirmation = resolve_payroll_confirmation(
        wage, context_for(wage, facts=(plan, profile, policy)),
        profile_version=profile, contribution_policy_version=policy,
    )
    assert confirmation
    assert plan.fact.payroll_revision.subject_id == wage.subject_id
    assert plan.fact.payroll_revision.revision == wage.revision and plan.evidence
    with pytest.raises(NeedsInformation) as error:
        engine.preview(["stage-january-wage"])
    issues = error.value.response()["fact_issues"]
    assert any(issue["field"] == "contribution_actual.items" for issue in issues)
    dispatch(app, token, "save_work_draft", company_id=info["company_id"],
             period=info["period"], work_area="payroll", expected_revision=None,
             draft={"resume_note": "第2阶段工资方案及个税依据已有确认；第3阶段实际社保尚缺。",
                    "next_step": "第3阶段取得实际社保后，第4阶段核算工资净额并准备薪资代发。"})
    with pytest.raises(NeedsInformation):
        engine.preview(["stage-january-wage"])
    evidence = engine.register_evidence(
        "合成来源：2026年1月该人员养老金个人1000元、单位2000元，已申报。".encode(),
        "text/plain", "合成社保明细", request_id="test-contribution-source",
    )["digest"]
    contribution = actual().model_copy(update={"employee_id": info["employee_id"]})
    dispatch(app, token, "save_fact", company_id=info["company_id"],
             kind=contribution.kind, subject_id="stage-actual-contributions",
             data=contribution.model_dump(mode="json"), evidence=[evidence],
             expected_revision=0, request_id="test-contribution-fact")
    preview = engine.preview(["stage-january-wage"])
    assert preview["status"] == "preview"
    calculated = preview["results"][0]
    assert calculated["values"]["net_fen"] == 887400
    assert calculated["values"]["payroll_confirmation"] == confirmation
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM calculation_publication").fetchone()[0] == 0


def test_eight_stage_checkpoint_restarts_with_company_and_period_isolation(seeded):
    app, token, info, directory = seeded
    scope = {"company_id": info["company_id"], "period": info["period"], "work_area": "bank"}
    source = info["sources"][0]
    driver = json.loads((directory / "evaluator-owner-turns.json").read_text(encoding="utf-8"))
    draft = {
        "questions": [
            {"id": "BANK-1", "question": "1月5日100元是什么用途？",
             "answer": driver["bank_answers"]["BANK-1"],
             "source_refs": [{"source_id": source["source_id"], "location": "CSV!B2"}]},
            {"id": "BANK-4", "question": "1月8日400元是什么用途？"},
        ],
        "resume_note": "八阶段第1阶段流水识别；用途问题分轮核对，本轮已收到第1笔答复。",
        "next_step": "接续第1阶段尚未回答的用途问题；工资方案到第2阶段复用，代发到第4阶段。",
    }
    saved = dispatch(app, token, "save_work_draft", **scope, expected_revision=None, draft=draft)
    before = business_state(app, scope["company_id"])
    app.close()
    restarted = LocalService(directory / "data")
    try:
        new_token = restarted.security.login(
            harness.reading.OWNER, harness.reading.PASSWORD,
        ).session_token
        assert restarted.security.catalog_instance_id == info["catalog_id"]
        assert {(c["id"], c["database_id"]) for c in restarted.catalog.companies()} == {
            (c["id"], c["database_id"]) for c in info["companies"]
        }
        recovered = dispatch(restarted, new_token, "work_context", **scope,
                             include_work_draft=True)["work_draft"]
        assert recovered["revision"] == saved["revision"] and recovered["draft"] == draft
        assert "answer" not in recovered["draft"]["questions"][1]
        for change in ({"company_id": info["companies"][1]["id"]}, {"period": "2026-02"}):
            other = dispatch(restarted, new_token, "work_context", **{**scope, **change},
                             include_work_draft=True)
            assert other["work_draft"]["status"] == "absent"
        assert business_state(restarted, scope["company_id"]) == before
    finally:
        restarted.close()
