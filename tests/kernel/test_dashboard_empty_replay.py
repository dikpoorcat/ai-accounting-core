"""Synthetic source replay through public commands, never a production company database."""

import base64
import json
from functools import partial
from pathlib import Path

import pytest
from test_close_range import ready

from ai_accounting.kernel.backup import verify_portable
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.service import LocalService, default_registry
from ai_accounting.kernel.storage import Store

TAXPAYER = "91310000123456789A"
COMPANY_NAME = "合成空库重录企业"
PERIOD = "2026-01"
SOURCE_NAME = "合成原件-一月出资和办公支出.txt"
SOURCE_TEXT = "合成测试：甲出资人于 2026-01-02 交付现金 1000 元，1 月确认办公支出 125 元。"
COMPANY_NOTE = "合成企业仅用于验证空库重录；本次原件明确提供出资和办公支出。"
COMMENTARY = "本月收到出资 1000 元，确认办公费用 125 元；支出已支付，期末现金 875 元。"


def authenticated(root: Path):
    service = LocalService(root)
    if not service.security.status()["provisioned"]:
        service.security.provision("owner", "synthetic-replay-password")
    token = service.security.login("owner", "synthetic-replay-password").session_token
    return service, partial(service.dispatch, session_token=token)


def company_call(dispatch, company_id):
    def call(command, **payload):
        return dispatch(command, {"company_id": company_id, **payload})

    return call


def replay_sources(call, prefix):
    """The private stable references are resolved before sending typed requests."""
    references = {
        key: f"{prefix}-{key}"
        for key in ("owner", "supplier", "cash", "funding", "expense", "payment")
    }
    evidence = call(
        "evidence",
        content_base64=base64.b64encode(SOURCE_TEXT.encode("utf-8")).decode("ascii"),
        name=SOURCE_NAME,
        media_type="text/plain",
        request_id="source:original:v1",
    )["digest"]
    for key, kind, name in (
        ("owner", "counterparty", "甲出资人"),
        ("supplier", "counterparty", "合成办公用品商店"),
        ("cash", "fund_account", "办公室现金"),
        ("expense", "business", "一月办公支出"),
    ):
        profile = {
            "kind": kind,
            "entity_id": references[key],
            "display_name": name,
            "source": "合成原件中的明确名称",
            "evidence_digest": evidence,
        }
        if key == "expense":
            profile.update(purpose="日常办公", note="原件明确为一月办公支出")
        call(
            "save_display_profile",
            profile=profile,
            expected_revision=0,
            request_id=f"profile:{key}:v1",
        )
    call(
        "update_company_note",
        text=COMPANY_NOTE,
        evidence_digest=evidence,
        expected_revision=0,
        request_id="company-note:v1",
    )
    facts = [
        {
            "kind": "cash_funding",
            "subject_id": references["funding"],
            "data": {
                "period": PERIOD,
                "actual_date": "2026-01-02",
                "owner_id": references["owner"],
                "cash_account_id": references["cash"],
                "amount_fen": 100000,
                "funding_kind": "capital",
            },
            "evidence": [evidence],
            "expected_revision": 0,
        },
        {
            "kind": "expense",
            "subject_id": references["expense"],
            "data": {
                "period": PERIOD,
                "counterparty_id": references["supplier"],
                "amount_fen": 12500,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            "evidence": [evidence],
            "expected_revision": 0,
        },
    ]
    saved = call("save_facts", facts=facts, request_id="facts:initial:v1")
    return references, evidence, saved


def reviewed_confirmation(call, subjects, request_id):
    preview = call("preview", subjects=subjects)
    assert preview["status"] == "preview"
    return {
        "subjects": subjects,
        "preview_digest": preview["digest"],
        "epochs": preview["epochs"],
        "request_id": request_id,
    }


def finish_payment(call, references, evidence):
    payment_evidence = call(
        "evidence",
        content_base64=base64.b64encode(
            "合成原始付款凭据：2026-01-08 现金支付 125 元。".encode()
        ).decode(),
        name="合成原件-一月办公支出付款凭据.txt",
        media_type="text/plain",
        request_id="source:payment:v1",
    )["digest"]
    call(
        "save_fact",
        kind="cash_payment",
        subject_id=references["payment"],
        data={
            "period": PERIOD,
            "actual_date": "2026-01-08",
            "direction": "outflow",
            "counterparty_id": references["supplier"],
            "cash_account_id": references["cash"],
            "amount_fen": 12500,
            "allocations": [
                {
                    "source_kind": "expense",
                    "source_id": references["expense"],
                    "obligation": "primary",
                    "amount_fen": 12500,
                }
            ],
        },
        evidence=[payment_evidence],
        expected_revision=0,
        request_id="fact:payment:v1",
    )
    confirmation = reviewed_confirmation(call, [references["payment"]], "publish:payment:v1")
    published = call("confirm", **confirmation)
    assert call("confirm", **confirmation) == published
    context = call("preview_period_commentary", period=PERIOD)
    summary = context["basis"]["accounting_summary"]
    assert summary["month_expense_fen"] == "12500"
    assert summary["month_result_fen"] == "-12500"
    assert summary["funds"]["cash_fen"] == "87500"
    payload = {
        "period": PERIOD,
        "text": COMMENTARY,
        "context_digest": context["context_digest"],
        "expected_revision": context["revision"],
        "source": "依据本次公开预览的经营摘要及合成原件编写",
        "evidence_digest": evidence,
        "request_id": "commentary:2026-01:v1",
    }
    saved = call("update_period_commentary", **payload)
    assert call("update_period_commentary", **payload) == saved
    return context


def assert_dashboard(call, references):
    brief = call("dashboard_brief", period=PERIOD)["data"]
    brief.pop("generated_at")
    assert brief["voucher_count"] == 3
    assert brief["funds_overview"]["cash_fen"] == 87500
    assert brief["management_commentary"] == COMMENTARY
    assert brief["management_commentary_details"]["status"] == "current"
    by_kind = {voucher["kind"]: voucher for voucher in brief["vouchers"]}
    funding, expense = by_kind["cash_funding"], by_kind["expense"]
    assert funding["components"][0]["parties"] == ["甲出资人"]
    assert funding["funds"][0]["name"] == "办公室现金"
    assert expense["components"][0]["parties"] == ["合成办公用品商店"]
    assert expense["components"][0]["description"] == "原件明确为一月办公支出"
    assert expense["recognition"]["precision"] == "month"
    assert expense["date"] is None
    assert expense["recognition"]["label"] == PERIOD
    assert expense["evidence_details"][0]["name"] == SOURCE_NAME
    trace = call("trace", voucher_version_id=expense["voucher_version_id"])
    assert trace["evidence_details"][0]["name"] == SOURCE_NAME
    assert call("company_context")["company_note"]["text"] == COMPANY_NOTE
    assert (
        call("display_profiles")["profiles"]["business"][references["expense"]]["purpose"]
        == "日常办公"
    )
    return brief


def test_empty_replay_resumes_lost_confirmation_and_preserves_dashboard_in_portable_restore(
    tmp_path,
):
    root = tmp_path / "replay"
    _, dispatch = authenticated(root)
    company = dispatch("create_company", {"taxpayer_id": TAXPAYER, "name": COMPANY_NAME})
    call = company_call(dispatch, company["id"])
    references, evidence, saved = replay_sources(call, "target-a")
    assert replay_sources(call, "target-a") == (references, evidence, saved)
    confirmation = reviewed_confirmation(
        call, [references["funding"], references["expense"]], "publish:initial:v1"
    )
    # Persist the exact reviewed request before commit. Simulate losing its response,
    # then reconnect and resend it instead of creating a fresh request or new facts.
    checkpoint = tmp_path / "synthetic-checkpoint.json"
    checkpoint.write_text(
        json.dumps(
            {"company_id": company["id"], "references": references, "confirmation": confirmation}
        ),
        encoding="utf-8",
    )
    published = call("confirm", **confirmation)
    _, resumed_dispatch = authenticated(root)
    state = json.loads(checkpoint.read_text(encoding="utf-8"))
    resumed = company_call(resumed_dispatch, state["company_id"])
    assert resumed("confirm", **state["confirmation"]) == published
    assert replay_sources(resumed, "target-a") == (references, evidence, saved)
    finish_payment(resumed, references, evidence)
    before = assert_dashboard(resumed, references)
    assert resumed("rebuild", request_id="rebuild:verified:v1") == {"status": "rebuilt"}
    assert resumed("rebuild", request_id="rebuild:verified:v1") == {"status": "rebuilt"}
    assert assert_dashboard(resumed, references) == before

    settings = resumed("company_settings")
    backup_directory = str(tmp_path / "company-backups")
    resumed(
        "configure_backup",
        backup_directory=backup_directory,
        expected_revision=settings["revision"],
    )
    job = resumed("backup", directory=backup_directory, request_id="backup:initial:v1")
    assert resumed("backup", directory=backup_directory, request_id="backup:initial:v1") == job
    assert resumed("jobs", job_id=job["job_id"])[0]["status"] == "pending"
    resumed("run_jobs", limit=1)
    completed = resumed("jobs", job_id=job["job_id"])[0]
    assert completed["status"] == "succeeded"
    archive = Path(completed["result"]["path"])
    assert archive.name == f"{TAXPAYER}.finance-company.zip"
    verified = verify_portable(archive)
    assert verified["identity"]["company_id"] == company["id"]

    _, restored_dispatch = authenticated(tmp_path / "restored")
    restoration = {"archive": str(archive), "taxpayer_id": TAXPAYER, "name": COMPANY_NAME}
    restored_company = restored_dispatch("restore_company", restoration)
    assert restored_dispatch("restore_company", restoration) == restored_company
    assert restored_company["id"] == company["id"]
    restored = company_call(restored_dispatch, restored_company["id"])
    assert assert_dashboard(restored, references) == before


def test_new_empty_target_remaps_ids_and_rejects_old_commentary_context(tmp_path):
    contexts = []
    companies = []
    fact_ids = []
    for prefix in ("target-a", "target-b"):
        _, dispatch = authenticated(tmp_path / prefix)
        company = dispatch("create_company", {"taxpayer_id": TAXPAYER, "name": COMPANY_NAME})
        call = company_call(dispatch, company["id"])
        references, evidence, saved = replay_sources(call, prefix)
        confirmation = reviewed_confirmation(
            call, [references["funding"], references["expense"]], "publish:initial:v1"
        )
        call("confirm", **confirmation)
        contexts.append(finish_payment(call, references, evidence))
        companies.append(company["id"])
        fact_ids.append(saved)
        assert_dashboard(call, references)
    assert companies[0] != companies[1]
    assert fact_ids[0] != fact_ids[1]
    assert contexts[0]["basis"]["accounting_summary"] == contexts[1]["basis"]["accounting_summary"]
    assert contexts[0]["context_digest"] != contexts[1]["context_digest"]
    with pytest.raises(KernelError) as stale:
        call(
            "update_period_commentary",
            period=PERIOD,
            text="不能把另一空库的经营结论摘要当成本库依据",
            context_digest=contexts[0]["context_digest"],
            source="合成旧目标上下文",
            expected_revision=1,
            request_id="commentary:wrong-target",
        )
    assert stale.value.code == "preview_expired"
    assert call("preview_period_commentary", period=PERIOD)["revision"] == 1


def test_replay_preserves_preclose_commentary_and_later_supplement_order(tmp_path):
    """Reuse the isolated period fixture; no owner window or real company is involved."""
    original_contexts = []
    close_digests = []
    for target in ("source", "new-empty-target"):
        engine = Engine(
            Store.create(
                tmp_path / f"{target}.sqlite",
                default_registry(),
                f"company-{target}",
                TAXPAYER,
                f"database-{target}",
            )
        )
        proof = engine.register_evidence(
            b"Synthetic owner explicitly confirmed no business in January 2026.",
            "text/plain",
            "合成原件-一月无业务确认.txt",
            request_id="source:no-business:v1",
        )["digest"]
        ready(engine, proof, first=PERIOD, last=PERIOD)
        display, periods = Display(engine), Periods(engine)
        known_profile = {
            "kind": "counterparty",
            "entity_id": "stable-party",
            "display_name": "关账前已有名称",
            "source": "原始时点明确提供的资料",
            "evidence_digest": proof,
        }
        first_profile = display.save_display_profile(
            known_profile, expected_revision=0, request_id="profile:before-close:v1"
        )
        current = display.preview_period_commentary(PERIOD)
        if target == "new-empty-target":
            # Neither the old pre-close digest nor its later supplement is valid
            # in a newly initialized target, even when the prose can be reused.
            for index, old_context in enumerate(original_contexts):
                with pytest.raises(KernelError) as stale:
                    display.update_period_commentary(
                        PERIOD,
                        "旧正文只作为待核对参考",
                        context_digest=old_context,
                        source="旧库经营说明",
                        expected_revision=0,
                        request_id=f"commentary:old-context:{index}",
                    )
                assert stale.value.code == "preview_expired"
        preclose = display.update_period_commentary(
            PERIOD,
            "依据当时负责人确认，本月无业务；本月无收支。",
            context_digest=current["context_digest"],
            source="原始无业务确认及本目标预览",
            evidence_digest=proof,
            expected_revision=0,
            request_id="commentary:before-close:v1",
        )
        preview = periods.preview_close(PERIOD, owner_confirmation=proof)
        closed = periods.close(
            PERIOD,
            owner_confirmation=proof,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="close:2026-01:v1",
        )
        frozen = periods.closed_report(PERIOD)
        later_profile = display.save_display_profile(
            known_profile | {"display_name": "后来明确补齐的名称", "source": "关账后补充资料"},
            expected_revision=1,
            request_id="profile:after-close:v2",
        )
        after_close = display.preview_period_commentary(PERIOD)
        supplement = display.update_period_commentary(
            PERIOD,
            "后补说明：后来补齐了名称，未发生新的核算事项。",
            context_digest=after_close["context_digest"],
            source="关账后补充资料及本目标预览",
            expected_revision=1,
            request_id="commentary:after-close:v2",
        )
        assert preclose["supplementary"] is False
        assert supplement["supplementary"] is True
        assert periods.closed_report(PERIOD) == frozen
        assert frozen["management_snapshot"]["profiles"][0]["id"] == first_profile["id"]
        assert frozen["management_snapshot"]["commentary"]["id"] == preclose["id"]
        assert later_profile["id"] not in json.dumps(frozen)
        assert supplement["id"] not in json.dumps(frozen)
        result = display.preview_period_commentary(PERIOD)
        assert result["current"]["id"] == result["frozen"]["id"] == preclose["id"]
        assert [item["id"] for item in result["supplements"]] == [supplement["id"]]
        if target == "source":
            original_contexts = [current["context_digest"], after_close["context_digest"]]
        else:
            assert current["context_digest"] not in original_contexts
            assert after_close["context_digest"] not in original_contexts
        close_digests.append(closed["digest"])
    assert close_digests[0] != close_digests[1]
