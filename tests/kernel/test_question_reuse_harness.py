"""Truth checks for the real-dialogue seed, independent of AI question selection."""

from __future__ import annotations

import json

import pytest
import question_reuse_agent_harness as harness

from ai_accounting.kernel.contracts import KernelError, NeedsInformation
from ai_accounting.kernel.service import LocalService


@pytest.fixture
def seeded(tmp_path):
    app = LocalService(tmp_path / "data")
    app.security.provision(harness.reading.OWNER, harness.reading.PASSWORD)
    token = app.security.login(harness.reading.OWNER, harness.reading.PASSWORD).session_token
    info = harness.seed(app, token, tmp_path)
    yield app, token, info, tmp_path
    app.close()


def test_prior_confirmations_and_lost_receipt_continue_to_real_publication(seeded):
    app, token, info, _ = seeded

    def command(name, **payload):
        return app.dispatch(name, payload, session_token=token)

    scope = {"company_id": info["company_id"], "period": info["period"], "work_area": "payroll"}
    context = command("work_context", **scope, include_work_draft=True, limit=1)
    assert context["has_more"]
    assert len(context["items"]) == 1
    facts = list(context["items"])
    while context["has_more"]:
        context = command("work_context", **scope, cursor=context["next_cursor"], limit=1)
        facts.extend(context["items"])
    assert {"stage-january-plan", "stage-wage-tax"} <= {fact["subject_id"] for fact in facts}
    receipt = command(
        "request_result",
        company_id=info["company_id"],
        submitted_request_id="reuse-save-actual-contributions",
    )
    assert receipt["status"] == "committed"
    preview = command(
        "preview", company_id=info["company_id"], subjects=[info["payroll_subject_id"]]
    )
    assert preview["results"][0]["values"]["net_fen"] == 887400
    published = command(
        "confirm",
        company_id=info["company_id"],
        subjects=[info["payroll_subject_id"]],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="test-reuse-wage-publication",
    )
    assert published["status"] == "published"
    engine = app.engine(info["company_id"])
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM voucher").fetchone()[0] > 0
        assert connection.execute("SELECT count(*) FROM fact_payment").fetchone()[0] == 0


def test_early_answer_is_not_already_registered_and_other_scope_has_no_draft(seeded):
    app, token, info, directory = seeded

    def context(**extra):
        return app.dispatch(
            "work_context",
            {
                "company_id": info["company_id"],
                "period": info["period"],
                "work_area": "transactions",
                "include_work_draft": True,
                **extra,
            },
            session_token=token,
        )

    result = context()
    question = result["work_draft"]["draft"]["questions"][0]
    assert question["status"] == "answered" and "供应商" in question["answer"]
    assert "creditor_kind" not in result["work_draft"]["draft"]["candidates"][0]["payload"]["data"]
    assert all(fact["subject_id"] != info["expense_subject_id"] for fact in result["items"])
    assert context(period="2026-02")["work_draft"]["status"] == "absent"
    assert context(company_id=info["companies"][1]["id"])["work_draft"]["status"] == "absent"
    public = json.loads((directory / "seed.json").read_text(encoding="utf-8"))
    assert "payroll_net_fen" not in public and "repeat_questions_expected" not in public


def test_changed_gross_only_blocks_the_old_tax_basis_despite_resolved_question(seeded):
    app, _, info, _ = seeded
    engine = app.engine(info["company_id"])
    proof = engine.register_evidence(
        "合成负责人更正：原录入工资毛额有误，本期实际毛额为11000元；工资方案已确认。"
        "其他工资输入不变，新毛额的最终个税尚未提供。".encode(),
        "text/plain",
        "合成毛额更正",
        request_id="reuse-gross-correction-proof",
    )["digest"]
    with engine.store.connection(read_only=True) as connection:
        wage = engine.store.current_fact(connection, info["payroll_subject_id"])
        plan = engine.store.current_fact(connection, "stage-january-plan")
    engine.amend_fact(
        wage.fact.kind,
        wage.subject_id,
        wage.fact.model_dump(mode="json") | {"accounting_gross_salary_fen": 1100000},
        evidence=(proof,),
        expected_revision=wage.revision,
        recording_error_confirmed=True,
        request_id="reuse-correct-gross",
    )
    engine.amend_fact(
        plan.fact.kind,
        plan.subject_id,
        plan.fact.model_dump(mode="json")
        | {
            "payroll_revision": {"subject_id": wage.subject_id, "revision": wage.revision + 1},
        },
        evidence=(proof,),
        expected_revision=plan.revision,
        recording_error_confirmed=True,
        request_id="reuse-correct-plan-reference",
    )
    with pytest.raises(NeedsInformation) as error:
        engine.preview([wage.subject_id])
    assert [issue["field"] for issue in error.value.issues] == [
        "remuneration_tax_basis.source_fact_id",
    ]
    assert wage.subject_id in error.value.issues[0]["reusable_sources"]


def test_question_scope_and_disposition_are_recoverable_in_all_six_areas(seeded):
    app, token, info, _ = seeded
    for area in ("bank", "payroll", "transactions", "tax", "assets", "financing"):
        scope = {"company_id": info["company_id"], "period": info["period"], "work_area": area}
        previous = app.dispatch("read_work_draft", scope, session_token=token)
        draft = previous.get("draft") or {}
        draft.setdefault("questions", []).append(
            {
                "id": "scope-reuse",
                "question": "这项资料的实际所属期？",
                "answer": "2025年12月。",
                "status": "resolved",
                "target": {"field": "period", "period": "2025-12"},
                "disposition_reason": "已核对原答复范围，工作月份不代替业务月份。",
            }
        )
        app.dispatch(
            "save_work_draft",
            {
                **scope,
                "expected_revision": previous.get("revision"),
                "draft": draft,
            },
            session_token=token,
        )
        recovered = app.dispatch(
            "work_context",
            {
                **scope,
                "include_work_draft": True,
            },
            session_token=token,
        )["work_draft"]["draft"]
        assert recovered["questions"][-1] == draft["questions"][-1]


def test_existing_business_display_request_can_resume_without_losing_unknown_result(seeded):
    app, token, info, _ = seeded

    def command(name, **payload):
        return app.dispatch(name, payload, session_token=token)

    scope = {
        "company_id": info["company_id"],
        "period": info["period"],
        "work_area": "transactions",
    }
    current = command("read_work_draft", **scope)
    payload = {
        "company_id": info["company_id"],
        "request_id": "reuse-business-display",
        "expected_revision": 0,
        "profile": {
            "kind": "business",
            "entity_id": info["payroll_subject_id"],
            "purpose": "合成管理用途",
            "source": "合成已登记业务的负责人管理说明",
        },
    }
    draft = current["draft"]
    draft["pending_requests"] = [
        {
            "command": "save_display_profile",
            "payload": payload,
            "request_id": payload["request_id"],
        }
    ]
    saved = command("save_work_draft", **scope, expected_revision=current["revision"], draft=draft)
    finished = {key: value for key, value in draft.items() if key != "pending_requests"}
    finished["result_refs"] = [
        {
            "command": "save_display_profile",
            "request_id": payload["request_id"],
        }
    ]
    with pytest.raises(KernelError) as failure:
        command("save_work_draft", **scope, expected_revision=saved["revision"], draft=finished)
    assert failure.value.code == "work_draft_pending_request_required"
    result = command("save_display_profile", **payload)
    receipt = command(
        "request_result", company_id=info["company_id"], submitted_request_id=payload["request_id"]
    )
    assert receipt["status"] == "committed" and receipt["result"] == result
    assert command("save_display_profile", **payload) == result
    command("save_work_draft", **scope, expected_revision=saved["revision"], draft=finished)
    assert command("read_work_draft", **scope)["draft"] == finished
