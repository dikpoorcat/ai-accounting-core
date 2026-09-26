"""Verify a relocated software bundle using only its isolated interpreter.

All business data below is synthetic. Most is written beside the relocated
package; the default-launcher check writes only after the software-only ZIP has
already been generated.
"""

from __future__ import annotations

import atexit
import base64
import ctypes
import hashlib
import http.client
import importlib
import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

_SERVICES = {}
_RUNNERS = {}
_PRIVATE_NATIVE = {}


def verify_reserve_business(call, call_rejected, approve_close, wait_for_backup, validation):
    """Exercise the reserve contract through packaged public commands, using synthetic facts."""
    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES
    from ai_accounting.payroll import CumulativeIncomeTaxPolicy

    company = call(
        "create_company", {"taxpayer_id": "91310000123456789C", "name": "运行包备用金合成企业"}
    )
    cid, month = company["id"], "2026-01"
    sequence = 0

    def execute(command, payload, **options):
        return call(command, {"company_id": cid, **payload}, **options)

    def request():
        nonlocal sequence
        sequence += 1
        return f"reserve-package-{sequence}"

    accounts = {
        channel: execute(
            "register_entity",
            {
                "kind": "fund_account",
                "account_type": channel,
                "data": {"display_name": f"合成{channel}账户"},
                "source": "独立包合成资料",
                "request_id": request(),
            },
        )["entity_id"]
        for channel in ("bank", "cash", "platform")
    }
    employee = execute(
        "register_entity",
        {
            "kind": "person",
            "data": {"display_name": "合成员工"},
            "source": "独立包合成资料",
            "request_id": request(),
        },
    )["entity_id"]
    tax = CumulativeIncomeTaxPolicy.china_resident_wage_withholding()
    facts = {
        "reserve-profile": (
            "payroll_profile",
            {
                "employee_id": employee,
                "effective_from": month,
                "effective_to": month,
                "withholding_start_date": "2026-01-01",
                "social_insurance_base_fen": 1000000,
                "housing_fund_base_fen": None,
                "social_insurance_participating": True,
                "housing_fund_participating": False,
                "contribution_shortfall": "reject",
            },
        ),
        "reserve-contributions": (
            "payroll_contribution_policy",
            {
                "version": "synthetic-reserve-contributions",
                "jurisdiction": "synthetic",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "primary_source_url": "https://www.mof.gov.cn/",
                "rules": [
                    {
                        "code": "pension",
                        "base_kind": "social_insurance",
                        "employee_rate": "0.08",
                        "employer_rate": "0.16",
                        "minimum_base_fen": 0,
                        "maximum_base_fen": 10000000,
                        "rounding": "half_up",
                        "enabled": True,
                    }
                ],
            },
        ),
        "reserve-income-tax": (
            "payroll_income_tax_policy",
            {
                "version": tax.version,
                "effective_from": tax.effective_from.isoformat(),
                "effective_to": None,
                "primary_source_url": tax.primary_source_url,
                "legal_basis_source_url": tax.legal_basis_source_url,
                "monthly_standard_deduction_fen": tax.monthly_standard_deduction_fen,
                "brackets": [
                    {
                        "upper_bound_fen": x.upper_bound_fen,
                        "rate": str(x.rate),
                        "quick_deduction_fen": x.quick_deduction_fen,
                    }
                    for x in tax.brackets
                ],
            },
        ),
        "reserve-payroll-opening": (
            "payroll_opening_state",
            {
                "employee_id": employee,
                "through_period": None,
                **dict.fromkeys(
                    (
                        "cumulative_income_fen",
                        "cumulative_tax_exempt_income_fen",
                        "cumulative_standard_deduction_fen",
                        "cumulative_employee_contributions_fen",
                        "cumulative_special_additional_deduction_fen",
                        "cumulative_other_legal_deduction_fen",
                        "cumulative_tax_relief_fen",
                        "cumulative_withheld_tax_fen",
                    ),
                    0,
                ),
            },
        ),
        "reserve-wage": (
            "payroll",
            {
                "employee_id": employee,
                "profile_id": "reserve-profile",
                "contribution_policy_id": "reserve-contributions",
                "income_tax_policy_id": "reserve-income-tax",
                "accounting_gross_salary_fen": 500000,
                "tax_reported_salary_fen": 500000,
                "tax_exempt_income_fen": 0,
                "special_additional_deduction_fen": 0,
                "other_legal_deduction_fen": 0,
                "tax_relief_fen": 0,
                "expense_class": "management",
                "contribution_basis": "policy_until_actual",
            },
        ),
        "reserve-bank-opening": (
            "bank_opening",
            {
                "bank_account_id": accounts["bank"],
                "opening_fen": 0,
                "basis": "new_account",
            },
        ),
    }
    for index, channel in enumerate(accounts):
        for action, amount, day in (
            ("expense", 1000 + index * 200, "03"),
            ("refund", 1600 + index * 200, "04"),
        ):
            facts[f"reserve-{channel}-{action}"] = (
                f"managed_reserve_{action}",
                {
                    "actual_date": f"2026-01-{day}",
                    f"{channel}_account_id": accounts[channel],
                    "amount_fen": amount,
                    **(
                        {"movement_ids": [f"reserve-platform-{action}-row"]}
                        if channel == "platform"
                        else {}
                    ),
                },
            )
    facts["reserve-batch"] = (
        "payroll_reserve_payment",
        {
            "actual_date": "2026-01-10",
            "bank_account_id": accounts["bank"],
            "amount_fen": 500000,
            "reserve_expense_fen": 80000,
            "return_period": month,
            "actual_return_date": None,
            "return_confirmed": True,
            "complete_group_confirmed": True,
            "allocations": [
                {
                    "source_kind": "payroll",
                    "source_id": "reserve-wage",
                    "recipient_id": employee,
                    "amount_fen": 420000,
                }
            ],
        },
    )
    text = json.dumps({"synthetic_confirmed_business": facts}, ensure_ascii=False)
    proof = execute(
        "evidence",
        {
            "content_base64": base64.b64encode(text.encode()).decode(),
            "media_type": "text/plain",
            "name": "独立包备用金明确合成依据",
            "request_id": request(),
        },
    )["digest"]

    def save(subject, kind, data, *, revision=0, evidence=proof, amend=False):
        command = {
            "material_source_v2": "receive_material",
            "material_resolution_v2": "resolve_material",
        }.get(kind)
        return execute(
            command or ("amend_fact" if amend else "save_fact"),
            {
                "subject_id": subject,
                **({} if command else {"kind": kind}),
                "data": {"period": month, **data},
                "evidence": [evidence],
                "expected_revision": revision,
                "request_id": request(),
                **({"recording_error_confirmed": True} if amend else {}),
            },
        )

    def publish(subjects, posting_period=None):
        args = {
            "subjects": subjects,
            **({"posting_period": posting_period} if posting_period else {}),
        }
        preview = execute("preview", args)
        result = execute(
            "confirm",
            {
                **args,
                "preview_digest": preview["digest"],
                "epochs": preview["epochs"],
                "request_id": request(),
            },
        )
        assert result["status"] == "published"
        return preview

    material = save(
        "reserve-source",
        "material_source_v2",
        {
            "evidence_digest": proof,
            "category": "transactions",
            "purpose": "supporting",
            "supporting_purpose": "合成明确业务事实确认，不冒充额外流水",
            "specification": {
                "format": "text",
                "all_pages_reviewed": True,
                "passages": [{"location": "facts", "page": 1, "excerpt": text}],
            },
        },
    )
    save(
        "reserve-source-resolution",
        "material_resolution_v2",
        {
            "source_id": "reserve-source",
            "source_fact_id": material["fact_id"],
            "location": "facts",
            "treatment": "supporting",
            "reason": "保留合成事实的确认依据",
        },
    )
    for action, direction, amount, day in (
        ("expense", "outflow", 1400, "03"),
        ("refund", "inflow", 2000, "04"),
    ):
        save(
            f"reserve-platform-{action}-row",
            "platform_movement",
            {
                "platform_account_id": accounts["platform"],
                "actual_date": f"2026-01-{day}",
                "direction": direction,
                "amount_fen": amount,
                "source_evidence_digest": proof,
                "source_location": f"platform-{action}",
            },
        )
    for subject, (kind, data) in facts.items():
        save(subject, kind, data)
    call_rejected(
        "preview", {"company_id": cid, "subjects": ["reserve-wage"]}, code="needs_information"
    )

    def wage_confirmation(data, *, revision=0, evidence=proof):
        return save(
            "reserve-wage-confirmation",
            "payroll_plan_v2",
            {
                "employee_id": employee,
                "payroll": {"period": month, **data},
                "profile_revision": {"subject_id": "reserve-profile", "revision": 1},
                "contribution_policy_revision": {
                    "subject_id": "reserve-contributions",
                    "revision": 1,
                },
                "income_tax_policy_revision": {"subject_id": "reserve-income-tax", "revision": 1},
                "change_notice_revisions": [],
            },
            revision=revision,
            evidence=evidence,
        )

    wage_confirmation(facts["reserve-wage"][1])
    publish(
        [
            "reserve-wage",
            "reserve-bank-opening",
            "reserve-platform-expense-row",
            "reserve-platform-refund-row",
        ]
    )
    publish(
        [
            subject
            for subject in facts
            if subject.startswith(
                ("reserve-bank-e", "reserve-bank-r", "reserve-cash-", "reserve-platform-")
            )
        ]
        + ["reserve-batch"]
    )
    save(
        "reserve-statement",
        "bank_statement",
        {
            "bank_account_id": accounts["bank"],
            "opening_fen": 0,
            "closing_fen": -499400,
            "entries": [
                {"reference": action, "actual_date": f"2026-01-{day}", "signed_fen": amount}
                for action, day, amount in (
                    ("expense", "03", -1000),
                    ("refund", "04", 1600),
                    ("batch", "10", -500000),
                )
            ],
        },
    )
    save(
        "reserve-reconciliation",
        "bank_reconciliation",
        {
            "bank_account_id": accounts["bank"],
            "statement_id": "reserve-statement",
            "matches": [
                {"reference": action, "source_kind": kind, "source_id": subject}
                for action, kind, subject in (
                    ("expense", "managed_reserve_expense", "reserve-bank-expense"),
                    ("refund", "managed_reserve_refund", "reserve-bank-refund"),
                    ("batch", "payroll_reserve_payment", "reserve-batch"),
                )
            ],
        },
    )
    publish(["reserve-statement", "reserve-reconciliation"])
    overview = execute("overview", {"period": month})
    nets = {row["account"]: row["debit"] - row["credit"] for row in overview["accounts"]}
    assert {key: nets[key] for key in ("1002", "1001", "1012")} == {
        "1002": -499400,
        "1001": 600,
        "1012": 600,
    }
    # Payroll keeps its own exact management-expense account. The direct reserve
    # expense is 80000, less net actual refunds 1800; neither overwrites payroll.
    assert nets["560201"] == 660000
    assert nets["5602"] == 78200
    funds = execute("dashboard_funds", {"period": month})
    assert funds["schema_version"] == 7
    mapping_check = funds["data"]["period_preparation"]["current_followups"]["tax_import_mapping"]
    assert mapping_check["status"] == "needs_information"
    assert mapping_check["blocking_scope"] == "tax_import_file"
    for category in MATERIAL_CATEGORIES:
        busy = category in {"bank", "payroll", "transactions"}
        execute(
            "inventory",
            {
                "period": month,
                "category": category,
                "evidence": [proof] if busy else [],
                "expected": int(busy),
                "no_business": not busy,
                "confirmation_evidence": proof,
                "request_id": request(),
            },
        )
    preview = execute("preview_close", {"period": month, "owner_confirmation": proof})
    approval = approve_close(company, month, preview)
    execute(
        "close",
        {
            "period": month,
            "owner_confirmation": proof,
            "approval_id": approval,
            "preview_digest": preview["digest"],
            "epochs": preview["epochs"],
            "request_id": request(),
        },
    )
    frozen = execute("closed_report", {"period": month})
    correction_proof = execute(
        "evidence",
        {
            "content_base64": base64.b64encode(
                "合成纠错：现金退款实际为1900分，原录1800分。".encode()
            ).decode(),
            "media_type": "text/plain",
            "name": "合成现金退款录入更正",
            "request_id": request(),
        },
    )["digest"]
    kind, data = facts["reserve-cash-refund"]
    save(
        "reserve-cash-refund",
        kind,
        {**data, "amount_fen": 1900},
        revision=1,
        evidence=correction_proof,
        amend=True,
    )
    correction = publish(["reserve-cash-refund"], "2026-02")
    assert correction["results"][0]["mode"] == "closed_correction"
    assert execute("closed_report", {"period": month}) == frozen
    corrected_month = execute("overview", {"period": "2026-02"})
    assert {
        row["account"]: row["debit"] - row["credit"] for row in corrected_month["accounts"]
    } == {"1001": 100, "5602": -100}

    # An explicit change to the approved wage input is corrected in an open month.
    # It changes the expense classification, not the real payment or its net split.
    wage_correction_text = (
        "合成负责人确认：一月工资属于销售费用，原管理费用归类有误。工资金额及实际付款不变。"
    )
    wage_proof = execute(
        "evidence",
        {
            "content_base64": base64.b64encode(wage_correction_text.encode()).decode(),
            "media_type": "text/plain",
            "name": "合成工资分类更正确认",
            "request_id": request(),
        },
    )["digest"]
    revised_wage = {**facts["reserve-wage"][1], "expense_class": "sales"}
    save("reserve-wage", "payroll", revised_wage, revision=1, evidence=wage_proof, amend=True)
    call_rejected(
        "preview",
        {"company_id": cid, "subjects": ["reserve-wage"], "posting_period": "2026-02"},
        code="needs_information",
    )
    wage_confirmation(revised_wage, revision=1, evidence=wage_proof)
    wage_correction = publish(["reserve-wage"], "2026-02")
    assert (
        next(row for row in wage_correction["results"] if row["subject_id"] == "reserve-wage")[
            "mode"
        ]
        == "closed_correction"
    )
    assert execute("closed_report", {"period": month}) == frozen

    # A newly received closed-period document remains an open-period follow-up.
    wage_details = execute(
        "dashboard_business_status",
        {"period": month, "subject_id": "reserve-wage", "as_of": "2026-02-28"},
    )
    assert wage_details["schema_version"] == 5
    current_confirmation = wage_details["data"]["current_business_result"]["payroll_confirmation"]
    frozen_confirmation = wage_details["data"]["frozen_adoption"]["payroll_confirmation"]
    assert current_confirmation["confirmation_revision"] == 2
    assert current_confirmation["evidence"] == [wage_proof]
    assert frozen_confirmation["confirmation_revision"] == 1
    assert frozen_confirmation["evidence"] == [proof]

    late_source = save(
        "reserve-late-source",
        "material_source_v2",
        {
            "evidence_digest": wage_proof,
            "category": "payroll",
            "purpose": "supporting",
            "supporting_purpose": "合成后补工资分类更正确认",
            "specification": {
                "format": "text",
                "all_pages_reviewed": True,
                "passages": [
                    {
                        "location": "late-confirmation",
                        "page": 1,
                        "excerpt": wage_correction_text,
                    }
                ],
            },
        },
        evidence=wage_proof,
    )
    followups = execute("period_readiness", {"period": "2026-02"})["current_followups"]
    assert any(
        item.get("source_id") == "reserve-late-source"
        and item.get("responsibility") == "closed_followup"
        and item.get("origin_periods") == [month]
        for item in followups["materials"]["issues"]
    )
    save(
        "reserve-late-resolution",
        "material_resolution_v2",
        {
            "source_id": "reserve-late-source",
            "source_fact_id": late_source["fact_id"],
            "location": "late-confirmation",
            "treatment": "supporting",
            "reason": "已核对并保留负责人更正确认，工资分类已在开放月更正",
        },
        evidence=wage_proof,
    )
    assert not any(
        item.get("source_id") == "reserve-late-source"
        for item in execute("period_readiness", {"period": "2026-02"})["current_followups"][
            "materials"
        ]["issues"]
    )
    assert execute("closed_report", {"period": month}) == frozen
    assert execute("verify_integrity", {})["status"] == "verified"
    queued = execute(
        "backup", {"directory": str(validation / "reserve-backups"), "request_id": request()}
    )
    archive = wait_for_backup(cid, queued)
    restored_root = validation / "reserve-restored"
    restored = call(
        "restore_company",
        {
            "archive": archive["path"],
            "taxpayer_id": company["taxpayer_id"],
            "name": "独立包备用金恢复企业",
        },
        root=restored_root,
    )
    assert restored["database_id"] == company["database_id"]
    assert execute("closed_report", {"period": month}, root=restored_root) == frozen
    for period in (month, "2026-02"):
        assert execute("overview", {"period": period}, root=restored_root) == execute(
            "overview", {"period": period}
        )
    assert execute("verify_integrity", {}, root=restored_root)["status"] == "verified"
    return cid, {
        "channels": ["bank", "cash", "platform"],
        "direct_expenses_and_refunds": 6,
        "bank_reconciliation_verified": True,
        "payroll_bank_outflow_fen": facts["reserve-batch"][1]["amount_fen"],
        "payroll_reserve_expense_fen": facts["reserve-batch"][1]["reserve_expense_fen"],
        "management_expense_net_fen": nets["5602"] + nets["560201"],
        "closed_correction_fen": 100,
        "frozen_snapshot_unchanged": True,
        "backup_restored_and_integrity_verified": True,
        "payroll_confirmation_required_and_closed_correction_verified": True,
        "current_and_frozen_payroll_confirmation_sources_verified": True,
        "late_closed_material_followup_resolved_without_rewriting_close": True,
        "tax_mapping_issue_does_not_block_close": True,
    }


def assert_current_formats(app, company_id):
    """Check installed contracts and real synthetic database formats together."""
    from ai_accounting.kernel.runtime import connect
    from ai_accounting.kernel.schema_bundle import production_bundle
    from ai_accounting.kernel.versions import database_format

    bundle = production_bundle()
    expected = {
        kind: {
            "family": contract["family"],
            "kind": contract["kind"],
            "status": contract["status"],
            "version": contract["version"],
            "fingerprint": contract["sha256"],
        }
        for kind in ("catalog", "company")
        for contract in (bundle.current(kind),)
    }
    with app.engine(company_id).store.connection(read_only=True) as connection:
        assert database_format(connection, bundle=bundle, kind="company") == expected["company"]
    connection = connect(app.catalog.path, read_only=True)
    try:
        assert database_format(connection, bundle=bundle, kind="catalog") == expected["catalog"]
    finally:
        connection.close()
    return expected


def business_contract(value):
    """Exclude file-task activity when comparing a backup and its restored source."""
    assert value["read_semantics"]["knowledge"] == "current_knowledge"
    assert value["read_semantics"]["accounting"] == "as_posted"
    assert value["read_semantics"]["business_basis"] == "current_known"
    assert value["review"]["status"] == "current"
    assert value["closure"]["state"] == "open"
    assert value["current_business_result"] is not None
    assert value["frozen_adoption"] is None
    obligations = value["settlements"]["obligations"]
    assert len(obligations) == 1
    assert type(obligations[0]["source_amount_fen"]) is int
    assert obligations[0]["source_amount_fen"] == obligations[0]["remaining_fen"] == 123456
    assert obligations[0]["settlement_status"] == "open"
    return {
        key: value[key]
        for key in (
            "identity",
            "period",
            "as_of",
            "closure",
            "as_posted",
            "current_business_result",
            "frozen_adoption",
            "settlements",
        )
    }


def readiness_contract(value):
    assert value["schema_version"] == 1
    assert value["as_of_semantics"] == "current_knowledge"
    assert value["closure"]["state"] == "open"
    assert value["current_followups"]["affects_frozen_readiness"] is False
    return {
        key: value[key]
        for key in ("company_id", "database_id", "period", "as_of", "closure", "readiness")
    }


def verify_stage8_workflow(call, other_company_id):
    """Use packaged commands for real completion, separate review and lost-response recovery."""
    company = call(
        "create_company", {"taxpayer_id": "91310000123456789D", "name": "运行包流程合成企业"}
    )
    cid = company["id"]

    def execute(command, payload):
        return call(command, {"company_id": cid, **payload})

    empty = execute("workflow", {"as_of": "2026-02-28"})
    assert empty["schema_version"] == 1 and empty["period"] is None
    assert {item["id"] for item in empty["sections"]["materials_and_accounting"]} == {
        "bank", "payroll", "transactions", "tax", "assets", "financing"
    }
    evidence = execute(
        "evidence",
        {
            "content_base64": base64.b64encode(b"Synthetic external filing and review").decode(),
            "media_type": "text/plain",
            "name": "合成外部办理确认",
            "request_id": "stage8-evidence",
        },
    )["digest"]
    obligation = execute(
        "save_fact",
        {
            "kind": "external_obligation", "subject_id": "stage8-obligation",
            "data": {
                "period": "2026-01", "obligation_kind": "quarterly_tax",
                "start_period": "2026-01", "end_period": "2026-01",
                "due_date": "2026-02-20", "applicability_confirmed": True,
            },
            "evidence": [evidence], "expected_revision": 0,
            "request_id": "stage8-obligation-request",
        },
    )
    before = execute("workflow", {"period": "2026-01", "as_of": "2026-02-28"})
    assert before["schema_version"] == 1
    assert before["sections"]["external"]["obligations"][0]["actual_completion_status"] == "due"
    basis = execute("obligation_basis", {"obligation_id": "stage8-obligation"})
    assert basis["obligation_fact_id"] == obligation["fact_id"]
    completion_request = {
        "kind": "external_completion", "subject_id": "stage8-completion",
        "data": {
            "period": "2026-02", "obligation_id": basis["obligation_id"],
            "obligation_fact_id": basis["obligation_fact_id"],
            "obligation_kind": basis["obligation_kind"],
            "start_period": basis["start_period"], "end_period": basis["end_period"],
            "no_reportable_activity_confirmed": True,
            "completion_status": "confirmed_complete", "date_status": "known",
            "completion_date": "2026-02-10",
        },
        "evidence": [evidence], "expected_revision": 0,
        "request_id": "stage8-completion-request",
    }
    saved = execute("save_fact", completion_request)
    assert execute("save_fact", completion_request) == saved
    receipt = execute(
        "request_result", {"submitted_request_id": completion_request["request_id"]}
    )
    assert receipt == {
        "status": "committed", "company_id": cid,
        "database_id": company["database_id"],
        "submitted_request_id": completion_request["request_id"],
        "action": "confirm_fact", "result": saved,
    }
    assert call(
        "request_result",
        {"company_id": other_company_id,
         "submitted_request_id": completion_request["request_id"]},
    )["status"] == "unknown"
    preview = execute("preview", {"subjects": ["stage8-completion"]})
    execute("confirm", {
        "subjects": ["stage8-completion"], "preview_digest": preview["digest"],
        "epochs": preview["epochs"], "request_id": "stage8-completion-publish",
    })
    completed = execute("workflow", {"period": "2026-01", "as_of": "2026-02-28"})
    item = completed["sections"]["external"]["obligations"][0]
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "not_reviewed"
    review = execute("save_fact", {
        "kind": "external_basis_review", "subject_id": "stage8-review",
        "data": {
            "period": "2026-02", "completion_id": "stage8-completion",
            "completion_fact_id": saved["fact_id"],
            "obligation_id": basis["obligation_id"],
            "obligation_fact_id": basis["obligation_fact_id"],
            "obligation_kind": basis["obligation_kind"],
            "start_period": basis["start_period"], "end_period": basis["end_period"],
            "reviewed_calculations": [], "review_result": "matched",
        },
        "evidence": [evidence], "expected_revision": 0,
        "request_id": "stage8-review-request",
    })
    assert review["fact_id"]
    review_preview = execute("preview", {"subjects": ["stage8-review"]})
    execute("confirm", {
        "subjects": ["stage8-review"], "preview_digest": review_preview["digest"],
        "epochs": review_preview["epochs"], "request_id": "stage8-review-publish",
    })
    after = execute("workflow", {"period": "2026-01", "as_of": "2026-02-28"})
    item = after["sections"]["external"]["obligations"][0]
    assert item["actual_completion_status"] == "completed"
    assert item["basis_review_status"] == "reviewed"
    return cid


def verify_stage8_job_recovery(call, validation, company_id):
    """Exercise failed durable backup, public diagnostics and explicit retry."""
    blocker = validation / "stage8-blocked-backup"
    blocker.write_text("synthetic directory blocker", encoding="utf-8")
    queued = call("backup", {
        "company_id": company_id, "directory": str(blocker),
        "request_id": "stage8-failing-backup",
    })
    deadline = time.monotonic() + 30
    while True:
        job = call("jobs", {"company_id": company_id, "job_id": queued["job_id"]})[0]
        if job["status"] == "failed" and job["attempts"] == 3:
            break
        assert time.monotonic() < deadline, "Synthetic backup did not reach failed state"
        time.sleep(0.1)
    assert job["attempts"] == 3
    assert job["error_code"] and job["error_message"]
    assert "last_error" not in job and str(blocker) not in job["error_message"]
    workflow = call("workflow", {
        "company_id": company_id, "period": "2026-01", "as_of": "2026-02-28"
    })
    assert any(
        item["job_id"] == queued["job_id"] and item["status"] == "failed"
        for item in workflow["sections"]["files"]["jobs"]
    )
    resident_root = validation / "companies"
    _RUNNERS[resident_root].stop()
    blocker.unlink()
    retry = call("retry_job", {
        "company_id": company_id, "job_id": queued["job_id"],
        "request_id": "stage8-retry-backup",
    })
    assert retry["status"] == "pending"
    assert retry["previous_error_code"] == job["error_code"]
    assert retry["previous_error_message"] == job["error_message"]
    from ai_accounting.kernel.jobs import JobRunner

    runner = JobRunner(_SERVICES[resident_root][0].catalog, interval=0.1)
    _RUNNERS[resident_root] = runner
    runner.start()
    deadline = time.monotonic() + 30
    while True:
        retried = call("jobs", {"company_id": company_id, "job_id": queued["job_id"]})[0]
        if retried["status"] == "succeeded":
            break
        assert retried["status"] != "failed", retried
        assert time.monotonic() < deadline, "Explicit backup retry did not complete"
        time.sleep(0.1)
    assert Path(retried["result"]["path"]).is_file()


def start_resident(root, *, native_smoke=False):
    """Exercise the exact daemon components using exclusively synthetic owners."""
    from pydantic import SecretStr

    from ai_accounting.kernel.daemon import build_native_security_controller
    from ai_accounting.kernel.http import create_server
    from ai_accounting.kernel.jobs import JobRunner
    from ai_accounting.kernel.security.transport import (
        NativeHttpClient,
        WindowBridge,
        launch_native_window,
    )
    from ai_accounting.kernel.security.window import SecurityForm
    from ai_accounting.kernel.security.windows import read_protected_json, write_protected_json
    from ai_accounting.kernel.service import LocalService

    app = LocalService(root)
    password = SecretStr("Synthetic-package-owner-only-2026")
    app.security.provision("package-test-owner", password)
    server, capability = create_server(app, port=0)
    controller = build_native_security_controller(
        app, server, capability, window_opener=lambda request_id: None
    )
    app.security_controller = controller
    metadata = {
        "protocol": 2,
        "database_format": app.catalog.database_format(),
        "pid": os.getpid(),
        "port": server.server_port,
        "capability": capability,
        "catalog_id": app.security.catalog_instance_id,
        "build_id": server.build_id,
    }
    write_protected_json(root / ".service.json", metadata)
    assert read_protected_json(root / ".service.json") == metadata
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _SERVICES[root] = (app, server, thread, metadata)
    runner = JobRunner(app.catalog, interval=0.1)
    _RUNNERS[root] = runner
    runner.start()
    client = NativeHttpClient(
        port=server.server_port,
        capability=capability,
        catalog_instance_id=app.security.catalog_instance_id,
    )
    _PRIVATE_NATIVE[root] = (client, password)

    if native_smoke:
        spawned = []
        controller.window_opener = lambda request_id: spawned.append(
            launch_native_window(
                request_id,
                port=server.server_port,
                capability=capability,
                catalog_instance_id=app.security.catalog_instance_id,
            )
        )
        window = controller.request(kind="login")
        deadline = time.monotonic() + 15
        while controller.status(window["request_id"])["status"] == "starting":
            assert time.monotonic() < deadline, "Native pythonw window did not become visible"
            time.sleep(0.1)
        assert controller.status(window["request_id"])["status"] == "waiting_for_user"
        kernel = ctypes.WinDLL("Kernel32.dll", use_last_error=True)
        kernel.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        kernel.WaitForSingleObject.restype = ctypes.c_uint32
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x00100000, False, spawned[0])
        assert handle
        try:
            controller.cancel(window["request_id"])
            assert kernel.WaitForSingleObject(handle, 10_000) == 0
        finally:
            kernel.CloseHandle(handle)
        controller.window_opener = lambda request_id: None

        # Use the actual copied Tk form, its worker and private HTTP transport.
        # Only a synthetic password is entered, automatically, into this test form.
        import tkinter as tk

        request_id = controller.request(kind="login")["request_id"]
        bridge = WindowBridge(client, request_id)
        record, facts = bridge.inspect()
        window_root = tk.Tk()
        form = SecurityForm(window_root, bridge, record, facts)

        def submit_synthetic():
            form.entries["password"].insert(0, password.get_secret_value())
            form.submit()

        window_root.after(250, submit_synthetic)
        window_root.after(15_000, form.destroy)
        window_root.mainloop()
        assert controller.status(request_id)["status"] == "succeeded"
    else:
        request_id = controller.request(kind="login")["request_id"]
        client.call("native_execute", request_id, password=password)
        client.call("native_update", request_id, status="succeeded")
    assert controller.session_status()["authenticated"]
    return app, server, metadata


def stop_residents():
    for runner in _RUNNERS.values():
        runner.stop()
    _RUNNERS.clear()
    for app, server, thread, _ in reversed(tuple(_SERVICES.values())):
        try:
            token = app.security_controller.store.load_session_token()
            if token is not None:
                app.security.logout(token)
        finally:
            app.security_controller.store.delete_session_token()
            server.shutdown()
            thread.join(timeout=5)
            server.server_close()
    _SERVICES.clear()
    _PRIVATE_NATIVE.clear()


atexit.register(stop_residents)


def main():
    package = Path(__file__).resolve().parents[1]
    manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    assert sys.flags.isolated == 1 and sys.flags.ignore_environment == 1
    assert Path(sys.base_prefix).resolve() == package / "runtime"
    assert all(Path(entry).resolve().is_relative_to(package) for entry in sys.path)
    for relative, expected in manifest["files"].items():
        source = package / relative
        assert source.resolve().is_relative_to(package)
        assert source.stat().st_size == expected["bytes"], relative
        with source.open("rb") as stream:
            assert hashlib.file_digest(stream, "sha256").hexdigest() == expected["sha256"], relative

    import argon2  # noqa: F401
    import pypdf  # noqa: F401
    import xlrd  # noqa: F401
    import xlwt  # noqa: F401

    from ai_accounting.financial_statement_template import _template_bytes
    from ai_accounting.kernel.build import calculator_build_id
    from ai_accounting.kernel.mcp import serve  # noqa: F401 - validate optional entry dependencies

    assert calculator_build_id() == manifest["runtime"]["build_id"]
    required_modules = (
        "business_queries",
        "query_reads",
        "dashboard_reads",
        "read_indexes",
        "versions",
    )
    for name in required_modules:
        module = importlib.import_module("ai_accounting.kernel." + name)
        assert Path(module.__file__).resolve().is_relative_to(package)
        assert "kernel/" + name + ".py" in manifest["application_modules"]
    contracts = package / "app/ai_accounting/kernel/schema_contracts"
    assert sorted(path.relative_to(contracts).as_posix() for path in contracts.rglob("*.json")) == [
        "catalog/draft.json",
        "company/draft.json",
    ]
    assert not (package / "app/ai_accounting/kernel/migrations").exists()
    assert not (package / "app/ai_accounting/kernel/security/batches.py").exists()
    for contract_file in contracts.rglob("*.json"):
        objects = json.loads(contract_file.read_text("utf-8"))["objects"]
        assert "security_close_batch" not in json.dumps(objects)
    assert sqlite3.sqlite_version == manifest["runtime"]["sqlite"] == "3.53.1"
    assert sys.version.split()[0] == manifest["runtime"]["python"] == "3.12.13"
    template_bytes = len(_template_bytes())
    validation = package.with_name(package.name + "-validation")
    validation.mkdir()  # Never overwrite an earlier verification or company.
    inputs = validation / "inputs"
    inputs.mkdir()
    data_root = validation / "companies"
    start_resident(data_root, native_smoke=True)
    calls = 0

    def call(command, payload, *, root=data_root):
        nonlocal calls
        if root not in _SERVICES:
            start_resident(root)
        calls += 1
        request = inputs / f"{calls:02d}-{command}.json"
        request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                "-m",
                "ai_accounting.kernel.cli",
                "--root",
                str(root),
                "call",
                command,
                "--input",
                str(request),
            ],
            cwd=package,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        try:
            response = json.loads(result.stdout)
        except json.JSONDecodeError:
            response = None
        if result.returncode:
            diagnostic = {"command": command, "returncode": result.returncode}
            if isinstance(response, dict):
                diagnostic["response"] = {
                    key: response[key] for key in ("status", "code", "message") if key in response
                }
            else:
                stderr_lines = [line.strip() for line in result.stderr.splitlines() if line.strip()]
                if stderr_lines:
                    diagnostic["stderr"] = stderr_lines[-1][:500]
            raise RuntimeError(
                "Packaged CLI call failed: " + json.dumps(diagnostic, ensure_ascii=False)
            )
        if response is None:
            raise RuntimeError(
                "Packaged CLI returned invalid JSON: "
                + json.dumps({"command": command, "returncode": result.returncode})
            )
        assert not isinstance(response, dict) or response.get("status") not in {
            "rejected",
            "needs_information",
        }, response
        return response

    def call_rejected(command, payload, *, code, root=data_root):
        nonlocal calls
        if root not in _SERVICES:
            start_resident(root)
        calls += 1
        request = inputs / f"{calls:02d}-{command}-rejected.json"
        request.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
        result = subprocess.run(
            [
                sys.executable,
                "-I",
                "-X",
                "utf8",
                "-m",
                "ai_accounting.kernel.cli",
                "--root",
                str(root),
                "call",
                command,
                "--input",
                str(request),
            ],
            cwd=package,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        response = json.loads(result.stdout)
        if code == "needs_information":
            assert result.returncode == 0, response
            assert response["status"] == "needs_information" and response["fact_issues"], response
        else:
            assert result.returncode == 1, response
            assert response["status"] == "rejected" and response["code"] == code, response
        return response

    def wait_for_backup(company_id, queued, *, root=data_root):
        assert queued["status"] == "pending"
        deadline = time.monotonic() + 30
        while True:
            jobs = call("jobs", {"company_id": company_id}, root=root)
            backup_job = next(row for row in jobs if row["id"] == queued["job_id"])
            if backup_job["status"] == "succeeded":
                return backup_job["result"]
            assert backup_job["status"] != "failed", "Automatic backup failed"
            assert time.monotonic() < deadline, "Automatic backup did not complete"
            time.sleep(0.1)

    def approve_close(company, period, preview, *, root=data_root):
        app = _SERVICES[root][0]
        client, password = _PRIVATE_NATIVE[root]
        review = call(
            "dashboard_close_review",
            {"company_id": company["id"], "period": period, "preview_digest": preview["digest"]},
            root=root,
        )
        assert review["state"] == "prepared"
        assert review["preview_digest"] == preview["digest"]
        assert review["owner_review"] == preview["manifest"]["owner_review"]
        request = app.security_controller.request(
            kind="approve_period_close",
            company_id=company["id"],
            database_id=company["database_id"],
            period=period,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
        )
        approved = client.call("native_execute", request["request_id"], password=password)
        client.call("native_update", request["request_id"], status="succeeded")
        assert app.security_controller.status(request["request_id"])["status"] == "succeeded"
        return approved["approval_id"]

    schema = call("schema", {})
    assert not {"preview_close_range", "close_range"} & set(schema["commands"])
    assert "approve_close_batches" not in json.dumps(schema["security_request_schema"])
    assert "calculation_hash" not in schema["security_request_schema"]["properties"]
    assert "preview_digest" in schema["security_request_schema"]["properties"]
    assert schema["period_close_contract"]["format_version"] == 4
    assert "owner_review" in schema["period_close_contract"]["required_fields"]
    assert {
        "expense",
        "cash_payment",
        "cash_funding",
        "labor",
        "managed_reserve_expense",
        "managed_reserve_refund",
        "payroll_reserve_payment",
    } <= schema["facts"].keys()
    assert (
        not {
            "managed_reserve_scope",
            "managed_reserve_bank_expense",
            "managed_reserve_obligation_settlement",
            "platform_boundary_disposition",
        }
        & schema["facts"].keys()
    )
    assert (
        not {"preview_managed_reserve_settlement", "confirm_managed_reserve_settlement"}
        & schema["command_schemas"].keys()
    )
    assert {"business_status", "period_readiness", "workflow", "request_result"} <= (
        schema["command_schemas"].keys()
    )
    for command in (
        "preview",
        "confirm",
        "prepare_asset_activation_batch",
        "confirm_asset_activation_batch",
        "prepare_asset_consumption_month",
        "confirm_asset_consumption_month",
    ):
        fields = schema["command_schemas"][command]["properties"]
        assert "posting_period" in fields and "correction_period" not in fields
    assert schema["publication_contract"]["preview_item_fields"] == [
        "source_period",
        "posting_period",
        "mode",
    ]
    assert schema["period_close_contract"]["format"] == "ai-accounting-kernel/2/period-close"
    company = call(
        "create_company", {"taxpayer_id": "91310000123456789A", "name": "运行包合成验证企业"}
    )
    company_id = company["id"]
    supplier = call(
        "register_entity",
        {
            "company_id": company_id,
            "kind": "organization",
            "data": {"display_name": "合成供应商原身份"},
            "source": "运行包合成资料",
            "request_id": "package-supplier",
        },
    )["entity_id"]
    corrected_supplier = call(
        "register_entity",
        {
            "company_id": company_id,
            "kind": "organization",
            "data": {"display_name": "合成供应商正确身份"},
            "source": "运行包合成纠错依据",
            "request_id": "package-correct-supplier",
        },
    )["entity_id"]
    proof = call(
        "evidence",
        {
            "company_id": company_id,
            "content_base64": base64.b64encode(b"Synthetic package verification expense").decode(
                "ascii"
            ),
            "media_type": "text/plain",
            "name": "合成验证资料",
            "request_id": "package-evidence",
        },
    )
    call(
        "save_fact",
        {
            "company_id": company_id,
            "kind": "expense",
            "subject_id": "synthetic-expense",
            "data": {
                "period": "2026-09",
                "amount_fen": 123456,
                "counterparty_id": supplier,
                "expense_class": "administration",
                "creditor_kind": "supplier",
            },
            "evidence": [proof["digest"]],
            "expected_revision": 0,
            "request_id": "package-fact",
        },
    )
    preview = call("preview", {"company_id": company_id, "subjects": ["synthetic-expense"]})
    confirmation = {
        "company_id": company_id,
        "subjects": ["synthetic-expense"],
        "preview_digest": preview["digest"],
        "epochs": preview["epochs"],
        "request_id": "package-publish",
    }
    published = call("confirm", confirmation)
    assert call("confirm", confirmation) == published
    correction = {
        "company_id": company_id,
        "changes": [
            {
                "subject_id": "synthetic-expense",
                "expected_revision": 1,
                "action": "reassign",
                "data": {
                    "period": "2026-09",
                    "amount_fen": 123456,
                    "counterparty_id": corrected_supplier,
                    "expense_class": "administration",
                    "creditor_kind": "supplier",
                },
            }
        ],
        "evidence": [proof["digest"]],
        "reason": "合成资料确认同一业务引用了错误对象",
        "entity_resolution": {"source_entity_id": supplier, "target_entity_id": corrected_supplier},
    }
    correction_preview = call("preview_identity_correction", correction)
    correction_result = call(
        "confirm_identity_correction",
        {
            **correction,
            "preview_digest": correction_preview["digest"],
            "epochs": correction_preview["epochs"],
            "request_id": "package-identity-correction",
        },
    )
    assert correction_result["status"] == "corrected"
    discovered = call(
        "find_facts",
        {"company_id": company_id, "entity_id": corrected_supplier, "kind": "expense", "limit": 1},
    )
    assert discovered["schema_version"] == 2
    assert discovered["items"][0]["revision"] == 2
    overview_request = {"company_id": company_id, "period": "2026-09"}
    overview = call("overview", overview_request)
    assert sum(row["debit"] for row in overview["accounts"]) == 123456
    assert sum(row["credit"] for row in overview["accounts"]) == 123456
    business_request = {
        **overview_request,
        "subject_id": "synthetic-expense",
        "as_of": "2026-09-30",
    }
    readiness_request = {**overview_request, "as_of": "2026-09-30"}
    business = business_contract(call("business_status", business_request))
    readiness = readiness_contract(call("period_readiness", readiness_request))
    database_formats = assert_current_formats(_SERVICES[data_root][0], company_id)
    assert manifest["runtime"]["database_formats"] == database_formats
    queued = call(
        "backup",
        {
            "company_id": company_id,
            "directory": str(validation / "backups"),
            "request_id": "package-backup",
        },
    )
    backup = wait_for_backup(company_id, queued)
    restored_root = validation / "restored"
    restored = call(
        "restore_company",
        {
            "archive": backup["path"],
            "taxpayer_id": company["taxpayer_id"],
            "name": "运行包合成恢复验证企业",
        },
        root=restored_root,
    )
    assert restored["id"] == company_id and restored["database_id"] == company["database_id"]
    assert call("overview", overview_request, root=restored_root) == overview
    assert assert_current_formats(_SERVICES[restored_root][0], company_id) == database_formats
    restored_business = call("business_status", business_request, root=restored_root)
    restored_readiness = call("period_readiness", readiness_request, root=restored_root)
    restored_workflow_request = {
        "company_id": company_id, "period": "2026-09", "as_of": "2026-09-30"
    }
    restored_workflow = call("workflow", restored_workflow_request, root=restored_root)
    restored_receipt_request = {
        "company_id": company_id, "submitted_request_id": "package-publish"
    }
    restored_receipt = call("request_result", restored_receipt_request, root=restored_root)
    assert restored_workflow["schema_version"] == 1
    assert restored_receipt["status"] == "committed"
    assert restored_receipt["result"] == published
    assert business_contract(restored_business) == business
    assert readiness_contract(restored_readiness) == readiness

    stage8_company_id = verify_stage8_workflow(call, company_id)
    verify_stage8_job_recovery(call, validation, stage8_company_id)

    from ai_accounting.kernel.periods import MATERIAL_CATEGORIES

    close_company = call(
        "create_company", {"taxpayer_id": "91310000123456789B", "name": "运行包合成关账企业"}
    )
    close_company_id = close_company["id"]
    close_period = "2026-08"
    close_proof = call(
        "evidence",
        {
            "company_id": close_company_id,
            "content_base64": base64.b64encode(
                b"Explicit synthetic close and correction confirmation"
            ).decode("ascii"),
            "media_type": "text/plain",
            "name": "合成关账与更正确认",
            "request_id": "package-close-evidence",
        },
    )["digest"]
    close_subject = "closed-expense"
    close_supplier = call(
        "register_entity",
        {
            "company_id": close_company_id,
            "kind": "organization",
            "data": {"display_name": "合成关账供应商"},
            "source": "合成关账原件",
            "request_id": "package-close-supplier",
        },
    )["entity_id"]
    close_fact = {
        "company_id": close_company_id,
        "kind": "expense",
        "subject_id": close_subject,
        "data": {
            "period": close_period,
            "amount_fen": 1000,
            "counterparty_id": close_supplier,
            "expense_class": "administration",
            "creditor_kind": "supplier",
        },
        "evidence": [close_proof],
        "expected_revision": 0,
        "request_id": "package-close-fact",
    }
    call("save_fact", close_fact)
    close_publication_preview = call(
        "preview", {"company_id": close_company_id, "subjects": [close_subject]}
    )
    close_route = close_publication_preview["results"][0]
    assert (close_route["source_period"], close_route["posting_period"], close_route["mode"]) == (
        close_period,
        close_period,
        "initial",
    )
    call(
        "confirm",
        {
            "company_id": close_company_id,
            "subjects": [close_subject],
            "preview_digest": close_publication_preview["digest"],
            "epochs": close_publication_preview["epochs"],
            "request_id": "package-close-publish",
        },
    )
    material_location = "confirmed-facts"
    material_excerpt = "Explicit synthetic close and correction confirmation"
    material_source_id = "closed-expense-source"
    material_source = call(
        "receive_material",
        {
            "company_id": close_company_id,
            "subject_id": material_source_id,
            "data": {
                "period": close_period,
                "evidence_digest": close_proof,
                "category": "transactions",
                "purpose": "supporting",
                "supporting_purpose": "合成运行包类型化事实确认记录，不是额外业务行",
                "specification": {
                    "format": "text",
                    "passages": [
                        {
                            "location": material_location,
                            "page": 1,
                            "excerpt": material_excerpt,
                        }
                    ],
                    "all_pages_reviewed": True,
                },
            },
            "evidence": [close_proof],
            "expected_revision": 0,
            "request_id": "package-close-material-source",
        },
    )
    call(
        "resolve_material",
        {
            "company_id": close_company_id,
            "subject_id": "closed-expense-resolution",
            "data": {
                "period": close_period,
                "source_id": material_source["subject_id"],
                "source_fact_id": material_source["fact_id"],
                "location": material_location,
                "treatment": "supporting",
                "reason": "确认记录仅证明已保存的类型化事实，不代表额外业务行",
            },
            "evidence": [close_proof],
            "expected_revision": 0,
            "request_id": "package-close-material-resolution",
        },
    )
    for category in MATERIAL_CATEGORIES:
        has_expense_material = category == "transactions"
        call(
            "inventory",
            {
                "company_id": close_company_id,
                "period": close_period,
                "category": category,
                "evidence": [close_proof] if has_expense_material else [],
                "expected": 1 if has_expense_material else 0,
                "no_business": not has_expense_material,
                "confirmation_evidence": close_proof,
                "request_id": f"package-close-inventory-{category}",
            },
        )
    close_preview = call(
        "preview_close",
        {
            "company_id": close_company_id,
            "period": close_period,
            "owner_confirmation": close_proof,
        },
    )
    approval_id = approve_close(close_company, close_period, close_preview)
    closed = call(
        "close",
        {
            "company_id": close_company_id,
            "period": close_period,
            "owner_confirmation": close_proof,
            "preview_digest": close_preview["digest"],
            "epochs": close_preview["epochs"],
            "approval_id": approval_id,
            "request_id": "package-close",
            "backup_directory": str(validation / "closed-backups"),
        },
    )
    assert closed["status"] == "closed" and closed["backup_job"]
    frozen_close = call("closed_report", {"company_id": close_company_id, "period": close_period})
    frozen_review = call(
        "dashboard_close_review", {"company_id": close_company_id, "period": close_period}
    )
    assert frozen_review["state"] == "closed"
    assert frozen_review["owner_review"] == close_preview["manifest"]["owner_review"]
    close_backup = wait_for_backup(
        close_company_id, {"status": "pending", "job_id": closed["backup_job"]}
    )
    close_restored_root = validation / "closed-restored"
    close_restored = call(
        "restore_company",
        {
            "archive": close_backup["path"],
            "taxpayer_id": close_company["taxpayer_id"],
            "name": "运行包合成关账恢复企业",
        },
        root=close_restored_root,
    )
    assert close_restored["id"] == close_company_id
    assert close_restored["database_id"] == close_company["database_id"]
    restored_review = call(
        "dashboard_close_review",
        {"company_id": close_company_id, "period": close_period},
        root=close_restored_root,
    )
    assert restored_review == frozen_review
    assert (
        call(
            "closed_report",
            {"company_id": close_company_id, "period": close_period},
            root=close_restored_root,
        )
        == frozen_close
    )
    call(
        "amend_fact",
        {
            **close_fact,
            "data": {**close_fact["data"], "amount_fen": 1500},
            "expected_revision": 1,
            "recording_error_confirmed": True,
            "request_id": "package-closed-correction-fact",
        },
    )
    call_rejected(
        "preview",
        {"company_id": close_company_id, "subjects": [close_subject]},
        code="posting_period_required",
    )
    correction_posting_period = "2026-09"
    correction_preview = call(
        "preview",
        {
            "company_id": close_company_id,
            "subjects": [close_subject],
            "posting_period": correction_posting_period,
        },
    )
    correction_route = correction_preview["results"][0]
    assert (
        correction_route["source_period"],
        correction_route["posting_period"],
        correction_route["mode"],
    ) == (close_period, correction_posting_period, "closed_correction")
    call(
        "confirm",
        {
            "company_id": close_company_id,
            "subjects": [close_subject],
            "posting_period": correction_posting_period,
            "preview_digest": correction_preview["digest"],
            "epochs": correction_preview["epochs"],
            "request_id": "package-closed-correction-publish",
        },
    )
    assert (
        call("closed_report", {"company_id": close_company_id, "period": close_period})
        == frozen_close
    )
    frozen_overview = call("overview", {"company_id": close_company_id, "period": close_period})
    correction_overview = call(
        "overview", {"company_id": close_company_id, "period": correction_posting_period}
    )
    frozen_net = {
        row["account"]: row["debit"] - row["credit"] for row in frozen_overview["accounts"]
    }
    correction_net = {
        row["account"]: row["debit"] - row["credit"] for row in correction_overview["accounts"]
    }
    assert frozen_net == {"2202": -1000, "5602": 1000}
    assert correction_net == {"2202": -500, "5602": 500}
    corrected_queued = call(
        "backup",
        {
            "company_id": close_company_id,
            "directory": str(validation / "corrected-backups"),
            "request_id": "package-corrected-backup",
        },
    )
    corrected_backup = wait_for_backup(close_company_id, corrected_queued)
    corrected_restored_root = validation / "corrected-restored"
    corrected_restored = call(
        "restore_company",
        {
            "archive": corrected_backup["path"],
            "taxpayer_id": close_company["taxpayer_id"],
            "name": "运行包合成更正恢复企业",
        },
        root=corrected_restored_root,
    )
    assert corrected_restored["id"] == close_company_id
    assert corrected_restored["database_id"] == close_company["database_id"]
    assert (
        call(
            "closed_report",
            {"company_id": close_company_id, "period": close_period},
            root=corrected_restored_root,
        )
        == frozen_close
    )
    restored_frozen_overview = call(
        "overview",
        {"company_id": close_company_id, "period": close_period},
        root=corrected_restored_root,
    )
    restored_correction_overview = call(
        "overview",
        {"company_id": close_company_id, "period": correction_posting_period},
        root=corrected_restored_root,
    )
    assert restored_frozen_overview == frozen_overview
    assert restored_correction_overview == correction_overview
    assert {
        row["account"]: row["debit"] - row["credit"] for row in restored_frozen_overview["accounts"]
    } == frozen_net
    assert {
        row["account"]: row["debit"] - row["credit"]
        for row in restored_correction_overview["accounts"]
    } == correction_net
    assert (
        call(
            "verify_integrity",
            {"company_id": close_company_id},
            root=corrected_restored_root,
        )["status"]
        == "verified"
    )

    reserve_company_id, reserve_validation = verify_reserve_business(
        call, call_rejected, approve_close, wait_for_backup, validation
    )
    for launcher in (
        [str(package / "finance-local.cmd")],
        [
            "powershell.exe",
            "-NoProfile",
            "-NonInteractive",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(package / "finance-local.ps1"),
        ],
    ):
        result = subprocess.run(
            [*launcher, "--root", str(data_root), "call", "companies"],
            cwd=package,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert {item["id"] for item in json.loads(result.stdout)} == {
            company_id,
            close_company_id,
            reserve_company_id,
            stage8_company_id,
        }

    default_environment = {
        key: value for key, value in os.environ.items() if key != "FINANCE_DATA_ROOT"
    }
    default_root = package / "data/kernel-draft"
    try:
        result = subprocess.run(
            [str(package / "finance-local.cmd"), "call", "schema"],
            cwd=package,
            env=default_environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        assert json.loads(result.stdout)["database_formats"] == database_formats
        result = subprocess.run(
            [
                "powershell.exe",
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-File",
                str(package / "finance-local.ps1"),
                "service-info",
            ],
            cwd=package,
            env=default_environment,
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
        default_metadata = json.loads(result.stdout)
        assert default_metadata["protocol"] == 2
        assert default_metadata["database_format"] == database_formats["catalog"]
    finally:
        subprocess.run(
            [str(package / "finance-local.cmd"), "stop"],
            cwd=package,
            env=default_environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
        )
    assert (default_root / "catalog.sqlite").is_file()

    import anyio
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    async def check_stdio_mcp():
        parameters = StdioServerParameters(
            command=sys.executable,
            args=[
                "-I",
                "-X",
                "utf8",
                "-m",
                "ai_accounting.kernel.cli",
                "--root",
                str(restored_root),
                "mcp",
            ],
        )
        with anyio.fail_after(30):
            async with stdio_client(parameters) as streams:
                async with ClientSession(*streams) as session:
                    await session.initialize()
                    tool_list = await session.list_tools()
                    assert {tool.name for tool in tool_list.tools} >= {
                        "finance_local_schema",
                        "finance_local_command",
                        "finance_local_security",
                    }
                    result = await session.call_tool(
                        "finance_local_command",
                        {
                            "command": "overview",
                            "payload": overview_request,
                        },
                    )
                    assert not result.isError
                    value = result.structuredContent
                    if value is None:
                        value = json.loads("".join(item.text for item in result.content))
                    assert value["accounts"] == overview["accounts"]
                    for command, payload, extract, expected in (
                        ("business_status", business_request, business_contract, business),
                        ("period_readiness", readiness_request, readiness_contract, readiness),
                        (
                            "workflow", restored_workflow_request,
                            lambda value: value, restored_workflow,
                        ),
                        (
                            "request_result", restored_receipt_request,
                            lambda value: value, restored_receipt,
                        ),
                    ):
                        result = await session.call_tool(
                            "finance_local_command", {"command": command, "payload": payload}
                        )
                        assert not result.isError
                        value = result.structuredContent
                        if value is None:
                            value = json.loads("".join(item.text for item in result.content))
                        assert extract(value) == expected

    anyio.run(check_stdio_mcp)

    app, server, thread, metadata = _SERVICES[restored_root]
    token = app.security_controller.store.load_session_token().get_secret_value()
    try:
        for route in (
            "/",
            "/index.html",
            "/local.html",
            "/funds",
            "/employees",
            "/assets",
            "/reports",
        ):
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request("GET", route)
            response = connection.getresponse()
            assert response.status == 200 and b"<html" in response.read(), route
            connection.close()
        connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
        connection.request(
            "GET",
            f"/api/local/overview?company_id={company_id}&period=2026-09",
            headers={"Authorization": "Bearer " + token},
        )
        response = connection.getresponse()
        assert response.status == 200
        wire_overview = json.loads(response.read())
        assert all(isinstance(row["debit"], str) for row in wire_overview["accounts"])
        assert sum(int(row["debit"]) for row in wire_overview["accounts"]) == 123456
        connection.close()
        dashboard_read_context = None
        for action in (
            "context",
            "brief",
            "funds",
            "employees",
            "assets",
            "quarterly-report",
            "business-status",
            "period-preparation",
        ):
            query = f"company_id={company_id}"
            query += (
                "&year=2026&quarter=3"
                if action == "quarterly-report"
                else ("" if action == "context" else "&period=2026-09")
            )
            if action == "business-status":
                query += "&subject_id=synthetic-expense&as_of=2026-09-30&limit=1"
            if action == "period-preparation":
                assert dashboard_read_context is not None
                query += "&" + urlencode(
                    {
                        "expected_read_version": dashboard_read_context["read_version"],
                        "as_of": dashboard_read_context["as_of"],
                    }
                )
            connection = http.client.HTTPConnection("127.0.0.1", server.server_port)
            connection.request(
                "GET",
                f"/api/dashboard/{action}?{query}",
                headers={"Authorization": "Bearer " + token},
            )
            response = connection.getresponse()
            wire_dashboard = json.loads(response.read())
            assert response.status == 200, (action, wire_dashboard)
            expected_version = {
                "context": 2,
                "business-status": 5,
                "quarterly-report": 4,
                "period-preparation": 4,
            }.get(action, 7)
            assert wire_dashboard["schema_version"] == expected_version
            if action == "context":
                assert wire_dashboard["current_company"]["company_id"] == company_id
            elif action == "brief":
                assert isinstance(wire_dashboard["data"]["total_debit_fen"], str)
                assert wire_dashboard["data"]["total_debit_fen"] == "123456"
                dashboard_read_context = wire_dashboard["read_context"]
            if action in {"brief", "funds", "employees", "assets"}:
                assert wire_dashboard["data"]["period_preparation"]["projection"] == (
                    "dashboard_period_preparation"
                )
            if action == "business-status":
                assert wire_dashboard["data"]["identity"] == business["identity"]
                obligation = wire_dashboard["data"]["settlements"]["obligations"][0]
                assert obligation["source_amount_fen"] == obligation["remaining_fen"] == "123456"
            for collection in wire_dashboard.get("data", {}).get("collections", {}).values():
                page = collection["page"]
                assert len(collection["items"]) == page["returned_count"]
                assert page["returned_count"] <= page["filtered_count"] <= page["total_count"]
            connection.close()
        review_app, review_server, _, _ = _SERVICES[corrected_restored_root]
        review_token = review_app.security_controller.store.load_session_token().get_secret_value()
        connection = http.client.HTTPConnection("127.0.0.1", review_server.server_port)
        connection.request(
            "GET",
            f"/api/dashboard/close-review?company_id={close_company_id}&period={close_period}",
            headers={"Authorization": "Bearer " + review_token},
        )
        response = connection.getresponse()
        wire_review = json.loads(response.read())
        assert response.status == 200, wire_review
        assert wire_review["schema_version"] == 1 and wire_review["state"] == "closed"
        assert wire_review["company_id"] == close_company_id
        assert wire_review["owner_review"]["accounting_summary"]["total_debit_fen"] == "1000"
        connection.close()
    finally:
        connection.close()

    outside = {
        name: module.__file__
        for name, module in tuple(sys.modules.items())
        if getattr(module, "__file__", None)
        and not Path(module.__file__).resolve().is_relative_to(package)
    }
    assert not outside, outside
    stop_residents()
    print(
        json.dumps(
            {
                "status": "passed",
                "package": str(package),
                "validation_data": str(validation),
                "runtime": manifest["runtime"],
                "verified_files": len(manifest["files"]),
                "cli_calls": calls,
                "fact_kinds": len(schema["facts"]),
                "entity_registration_and_atomic_identity_correction": True,
                "corrected_entity_fact_discovery_v2": True,
                "managed_reserve": reserve_validation,
                "published_vouchers": len(published["results"]),
                "debit_fen": 123456,
                "credit_fen": 123456,
                "backup_verified_and_restored": True,
                "background_backup_without_manual_run": True,
                "native_approved_close_backup_restored_and_frozen": True,
                "closed_period_correction_uses_posting_period": True,
                "closed_snapshot_unchanged_after_correction": True,
                "corrected_backup_verified_and_restored": True,
                "relative_imports_only": True,
                "http_page_and_authenticated_api": True,
                "dashboard_five_routes_and_legacy_entries": True,
                "dashboard_seven_authenticated_queries": True,
                "database_formats": database_formats,
                "cli_mcp_business_status_and_period_readiness": True,
                "stage8_external_completion_review_and_request_result": True,
                "stage8_failed_job_and_explicit_retry": True,
                "dashboard_current_schemas_and_collections": True,
                "dashboard_integer_cent_strings": True,
                "relative_cmd_and_powershell_launchers": True,
                "launchers_default_to_packaged_draft_root": True,
                "stdio_mcp_handshake_and_query": True,
                "native_pythonw_window_and_private_transport": True,
                "tk_form_synthetic_login": True,
                "windows_private_metadata_acl": True,
                "synthetic_credentials_revoked_and_removed": True,
                "template_bytes": template_bytes,
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
