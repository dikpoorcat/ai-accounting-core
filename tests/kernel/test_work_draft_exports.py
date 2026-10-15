"""Export requests remain recoverable without turning draft storage into submission."""

import copy

import pytest
from test_reports import book as book  # noqa: F401
from test_reports import close_quarter, scenario
from test_work_draft_service import business_state, save
from test_work_draft_service import service as service  # noqa: F401

from ai_accounting.kernel.command_schema import command_models, validate_command
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.reports import run_report_jobs
from ai_accounting.kernel.work_drafts import WorkDraftStore

EXPORT_COMMANDS = (
    "confirm_report_export",
    "confirm_browser_report_export",
    "confirm_export",
)
RECOVERABLE_COMMANDS = (
    *EXPORT_COMMANDS,
    "management",
    "update_period_commentary",
    "correct_fund_account_identity",
)


def original_payload(command, company_id, output_directory):
    base = {"company_id": company_id, "request_id": "original-" + command}
    if command == "management":
        return base | {
            "subject_id": "existing-source",
            "payment_period": None,
            "payment_category": "报销",
            "expected_revision": 0,
        }
    if command == "update_period_commentary":
        return base | {
            "period": "2026-02",
            "text": "原始经营结论，尚未提交。",
            "context_digest": "a" * 64,
            "source": "合成资料",
            "expected_revision": 0,
        }
    if command == "correct_fund_account_identity":
        return base | {
            "entity_id": "existing-bank",
            "account_type": "bank",
            "data": {
                "display_name": "合成公司账户",
                "bank_name": "合成银行",
                "bank_account_number": "6222000000000000001",
                "account_holder_name": "合成工作稿公司",
            },
            "expected_identity_digest": "a" * 64,
            "recording_error_confirmed": True,
            "source": "合成录入更正依据",
            "evidence_digest": "b" * 64,
        }
    payload = base | {
        "preview_digest": "a" * 64,
        "epochs": {"accounting": 7, "material": 11, "management": 13},
    }
    if command == "confirm_export":
        return payload | {
            "period": "2026-02",
            "template_evidence_digest": "b" * 64,
            "source_ids": ["second", "first"],
            "output_directory": output_directory,
        }
    payload.update(year=2026, quarter=1, carry_forward_fact_id=None)
    if command == "confirm_report_export":
        payload["output_directory"] = output_directory
    return payload


def pending(command, payload):
    return {
        "pending_requests": [
            {"command": command, "payload": payload, "request_id": payload["request_id"]}
        ]
    }


@pytest.mark.parametrize("command", RECOVERABLE_COMMANDS)
def test_original_export_and_company_requests_survive_restart_without_business_writes(
    service, tmp_path, command
):
    app, _, scope, restart = service
    payload = original_payload(command, scope["company_id"], str(tmp_path / "not-generated"))
    validate_command(app.command_models, command, payload, registry=app.registry)
    draft = pending(command, payload) | {"resume_note": "原载荷及请求编号尚待提交"}
    original = copy.deepcopy(draft)
    before = business_state(app, scope["company_id"])
    saved = save(app, scope, draft)
    assert draft == original
    assert business_state(app, scope["company_id"]) == before
    assert not (tmp_path / "not-generated").exists()
    app.close()
    reopened, _ = restart()
    restored = reopened.dispatch("read_work_draft", scope)
    assert restored["draft"] == original
    assert restored["revision"] == saved["revision"]
    assert restored["warnings"] == []
    assert business_state(reopened, scope["company_id"]) == before


@pytest.mark.parametrize("command", EXPORT_COMMANDS)
def test_export_pending_request_requires_company_and_original_request_id(
    service, tmp_path, command
):
    app, _, scope, _ = service
    payload = original_payload(command, scope["company_id"], str(tmp_path / "output"))
    for edited, code in (
        ({**payload, "company_id": "different-company"}, "work_draft_scope_mismatch"),
        (
            {key: value for key, value in payload.items() if key != "company_id"},
            "work_draft_request_invalid",
        ),
        ({**payload, "request_id": "changed-request"}, "work_draft_request_invalid"),
    ):
        draft = pending(command, payload)
        draft["pending_requests"][0]["payload"] = edited
        with pytest.raises(KernelError) as failure:
            save(app, scope, draft)
        assert failure.value.code == code
        assert app.dispatch("read_work_draft", scope)["status"] == "absent"


@pytest.mark.parametrize("command", EXPORT_COMMANDS)
def test_unsubmitted_export_request_cannot_be_discarded_or_changed(service, tmp_path, command):
    app, _, scope, _ = service
    payload = original_payload(command, scope["company_id"], str(tmp_path / "output"))
    draft = pending(command, payload)
    saved = save(app, scope, draft)
    receipt = app.dispatch(
        "request_result",
        {
            "company_id": scope["company_id"],
            "submitted_request_id": payload["request_id"],
        },
    )
    assert receipt["status"] == "unknown"
    for replacement in (
        {},
        {"result_refs": [{"command": command, "request_id": payload["request_id"]}]},
    ):
        with pytest.raises(KernelError) as failure:
            save(app, scope, replacement, saved["revision"])
        assert failure.value.code == "work_draft_pending_request_required"
    changed = copy.deepcopy(draft)
    changed["pending_requests"][0]["payload"]["preview_digest"] = "c" * 64
    with pytest.raises(KernelError) as failure:
        save(app, scope, changed, saved["revision"])
    assert failure.value.code == "work_draft_request_conflict"
    assert app.dispatch("read_work_draft", scope)["draft"] == draft


@pytest.mark.parametrize("command", ["preview_report_export", "preview_export"])
def test_export_previews_are_candidates_but_never_pending_writes(service, command):
    app, _, scope, _ = service
    draft = {"candidates": [{"id": "unfinished", "command": command, "payload": {}}]}
    saved = save(app, scope, draft)
    assert app.dispatch("read_work_draft", scope)["draft"] == draft
    with pytest.raises(KernelError) as failure:
        save(
            app,
            scope,
            pending(
                command,
                {
                    "company_id": scope["company_id"],
                    "request_id": "not-a-write",
                },
            ),
            saved["revision"],
        )
    assert failure.value.code == "work_draft_request_invalid"
    assert app.dispatch("read_work_draft", scope)["draft"] == draft


@pytest.mark.parametrize(
    "command",
    [
        "close",
        "evidence",
        "inventory",
        "backup",
        "retry_job",
        "verify_integrity",
        "repair_read_indexes",
        "not-a-command",
    ],
)
def test_export_support_does_not_admit_approval_evidence_or_job_operations(service, command):
    app, _, scope, _ = service
    with pytest.raises(KernelError) as failure:
        save(
            app,
            scope,
            pending(
                command,
                {
                    "company_id": scope["company_id"],
                    "request_id": "forbidden-operation",
                },
            ),
        )
    assert failure.value.code == "work_draft_target_invalid"
    assert app.dispatch("read_work_draft", scope)["status"] == "absent"


def database_dump(engine):
    with engine.store.connection(read_only=True) as connection:
        return tuple(connection.iterdump())


@pytest.mark.parametrize("command", EXPORT_COMMANDS[:2])
def test_report_export_saved_request_submits_once_and_retires_with_real_receipt(
    book, tmp_path, command
):
    engine = book[0]
    reports = scenario(book)
    close_quarter(book)
    plan = reports.preview_export(2026, 1)
    models = command_models(engine.store.registry)
    drafts = WorkDraftStore(
        tmp_path,
        "synthetic-catalog",
        engine.store.company_id,
        engine.store.database_id,
        registry=engine.store.registry,
        commands=models,
        request_result_snapshot=engine._request_result_snapshot,
    )
    payload = {
        "company_id": engine.store.company_id,
        "year": 2026,
        "quarter": 1,
        "preview_digest": plan["digest"],
        "epochs": plan["epochs"],
        "request_id": "export-" + command,
    }
    if command == "confirm_report_export":
        payload["output_directory"] = str(tmp_path / "output")
    validate_command(models, command, payload, registry=engine.store.registry)
    draft = pending(command, payload)
    before = database_dump(engine)
    saved = drafts.save("2026-03", "tax", None, draft)
    assert database_dump(engine) == before
    assert drafts.read("2026-03", "tax")["draft"] == draft
    assert engine.request_result(payload["request_id"])["status"] == "unknown"
    confirm = (
        reports.confirm_export
        if command == "confirm_report_export"
        else reports.confirm_browser_export
    )
    arguments = {key: value for key, value in payload.items() if key != "company_id"}
    queued = confirm(**arguments)
    assert queued["status"] == "queued"
    assert drafts.read("2026-03", "tax")["draft"] == draft
    receipt = engine.request_result(payload["request_id"])
    assert receipt["status"] == "committed" and receipt["result"] == queued
    submitted = database_dump(engine)
    assert confirm(**arguments) == queued
    assert database_dump(engine) == submitted
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute("SELECT count(*) FROM jobs WHERE kind='report_export'").fetchone()[0]
            == 1
        )
    result = run_report_jobs(engine)[0]
    assert result["status"] == "succeeded", result
    assert result["job_id"] == queued["job_id"]
    finished = {
        "result_refs": [
            {
                "command": command,
                "request_id": payload["request_id"],
                "job_id": queued["job_id"],
            }
        ]
    }
    generated = database_dump(engine)
    drafts.save("2026-03", "tax", saved["revision"], finished)
    assert drafts.read("2026-03", "tax")["draft"] == finished
    assert database_dump(engine) == generated
