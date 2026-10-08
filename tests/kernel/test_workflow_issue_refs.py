"""Workflow issue references preserve every original location and business state."""

from copy import deepcopy

import pytest
from monthly_close_fixture import ready
from test_workflow import obligation, setup_company
from workflow_test_helpers import expand_workflow

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.response_contracts import http_response, validate_response
from ai_accounting.kernel.workflow_issues import compact_workflow_issues
from ai_accounting.kernel.worklist import Worklist


def raw_query(company, monkeypatch, *, period="2026-01"):
    with monkeypatch.context() as patch:
        patch.setattr("ai_accounting.kernel.worklist.compact_workflow_issues", lambda value: value)
        with company.engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            return Worklist(company.engine).query(connection, as_of="2026-03-01", period=period)


def rich_response(company, monkeypatch):
    raw = raw_query(company, monkeypatch)
    issue = {"field": "materials.bank", "message": "资料待确认", "source_id": "source-a"}
    sections = raw["sections"]
    for area in sections["materials_and_accounting"]:
        area["materials"]["issues"] = [deepcopy(issue), deepcopy(issue)]
        area["accounting"]["issues"] = [deepcopy(issue)]
        area["close_issues"] = [deepcopy(issue)]
    sections["close"]["issues"] = [deepcopy(issue)]
    sections["external"]["obligations"] = [{
        "id": "filing", "obligation_fact_id": "filing-fact", "kind": "quarterly_tax",
        "start_period": "2026-01", "end_period": "2026-03", "due_date": "2026-04-20",
        "status": "pending", "actual_completion_status": "pending",
        "basis_review_status": "not_reviewed", "basis_review_calculation_id": None,
        "basis_issues": [deepcopy(issue), deepcopy(issue)], "recorded_completions": [],
    }]
    sections["external"]["settlements"]["issues"] = [deepcopy(issue)]
    sections["files"]["jobs"] = [{
        "job_id": "file-job", "kind": "tax_import", "status": "failed", "attempts": 1,
        "error_code": None, "error_message": None, "association": "period_scope",
        "period": "2026-01", "references": [], "verified_when_succeeded": False,
        "current_file_availability": "not_checked", "result_issue": deepcopy(issue),
        "contract_issues": [deepcopy(issue), deepcopy(issue)],
    }]
    sections["files"]["tax_import_mapping"]["issues"] = [{
        "field": "employee_id", "message": "映射待确认", "code": "missing_mapping",
        "category": "management_fact", "employee_id": "employee", "amount_fen": 1200,
    }]
    raw["fact_issues"] = [deepcopy(issue), deepcopy(issue)]
    return raw


def test_all_locations_share_only_equal_bodies_and_expand_losslessly(tmp_path, monkeypatch):
    company = setup_company(tmp_path)
    original = rich_response(company, monkeypatch)
    result = compact_workflow_issues(deepcopy(original))
    assert result["schema_version"] == 2
    assert len(result["issues"]) == 2
    assert result["fact_issue_refs"] == [0, 0]
    sections = result["sections"]
    assert all(area["materials"]["issue_refs"] == [0, 0]
               and area["accounting"]["issue_refs"] == [0]
               and area["close_issue_refs"] == [0]
               for area in sections["materials_and_accounting"])
    assert sections["close"]["issue_refs"] == [0]
    assert sections["external"]["obligations"][0]["basis_issue_refs"] == [0, 0]
    assert sections["external"]["settlements"]["issue_refs"] == [0]
    assert sections["files"]["jobs"][0]["result_issue_ref"] == 0
    assert sections["files"]["jobs"][0]["contract_issue_refs"] == [0, 0]
    assert sections["files"]["tax_import_mapping"]["issue_refs"] == [1]
    assert expand_workflow(result) == original
    wire = http_response("workflow", result)
    assert wire["issues"][1]["amount_fen"] == "1200"
    assert wire["fact_issue_refs"] == [0, 0]


@pytest.mark.parametrize("changed", [
    {"source_id": "source-b"}, {"period": "2026-02"}, {"location": "sheet:2"},
    {"row": 2}, {"expected_fen": 11}, {"location": None},
])
def test_same_wording_with_distinct_context_keeps_separate_body(tmp_path, monkeypatch, changed):
    raw = rich_response(setup_company(tmp_path), monkeypatch)
    base = raw["fact_issues"][0]
    raw["fact_issues"] = [base, dict(base, **changed), deepcopy(base)]
    result = compact_workflow_issues(raw)
    assert result["fact_issue_refs"] == [0, 2, 0]
    assert len(result["issues"]) == 3


def test_object_key_order_is_irrelevant_but_nested_list_order_is_preserved(tmp_path, monkeypatch):
    raw = rich_response(setup_company(tmp_path), monkeypatch)
    base = raw["fact_issues"][0]
    raw["fact_issues"] = [
        base, dict(reversed(list(base.items()))),
        dict(base, allowed_precision=["month", "day"]),
        dict(base, allowed_precision=["day", "month"]),
    ]
    result = compact_workflow_issues(raw)
    assert result["fact_issue_refs"] == [0, 0, 2, 3]


def test_nullable_result_and_omitted_settlement_list_remain_distinct(tmp_path, monkeypatch):
    original = rich_response(setup_company(tmp_path), monkeypatch)
    original["sections"]["files"]["jobs"][0]["result_issue"] = None
    original["sections"]["external"]["settlements"].pop("issues")
    result = compact_workflow_issues(deepcopy(original))
    assert result["sections"]["files"]["jobs"][0]["result_issue_ref"] is None
    assert "issue_refs" not in result["sections"]["external"]["settlements"]
    assert expand_workflow(result) == original


@pytest.mark.parametrize("ref", [True, -1, 1.0, "0", 999, None])
def test_invalid_references_are_rejected(tmp_path, monkeypatch, ref):
    result = compact_workflow_issues(rich_response(setup_company(tmp_path), monkeypatch))
    result["fact_issue_refs"] = [ref]
    for validate in (validate_response, http_response):
        with pytest.raises(KernelError) as error:
            validate("workflow", result)
        assert error.value.code == "response_contract_mismatch"


def test_target_issue_type_is_checked_in_both_directions(tmp_path, monkeypatch):
    result = compact_workflow_issues(rich_response(setup_company(tmp_path), monkeypatch))
    result["sections"]["files"]["tax_import_mapping"]["issue_refs"] = [0]
    with pytest.raises(KernelError) as error:
        validate_response("workflow", result)
    assert error.value.code == "response_contract_mismatch"
    result["sections"]["files"]["tax_import_mapping"]["issue_refs"] = [1]
    result["fact_issue_refs"] = [1]
    with pytest.raises(KernelError):
        validate_response("workflow", result)


def test_unrecognized_issue_location_cannot_be_silently_dropped(tmp_path, monkeypatch):
    raw = rich_response(setup_company(tmp_path), monkeypatch)
    raw["sections"]["files"]["future_check"] = {"issues": raw["fact_issues"]}
    with pytest.raises(KernelError) as error:
        compact_workflow_issues(raw)
    assert error.value.code == "response_contract_mismatch"


@pytest.mark.parametrize("location", ["readiness", "tax_mapping"])
def test_direct_reader_rejects_invalid_issue_type_before_service_validation(
    tmp_path, monkeypatch, location
):
    raw = rich_response(setup_company(tmp_path), monkeypatch)
    if location == "readiness":
        raw["sections"]["files"]["jobs"][0]["result_issue"] = {
            "field": "job.result", "message": None,
        }
    else:
        raw["sections"]["files"]["tax_import_mapping"]["issues"] = [{
            "field": "employee_id", "message": "缺少映射",  # Missing required type fields.
        }]
    with pytest.raises(KernelError) as error:
        compact_workflow_issues(raw)
    assert error.value.code == "response_contract_mismatch"


def test_compaction_does_not_repeat_full_response_validation(tmp_path, monkeypatch):
    raw = rich_response(setup_company(tmp_path), monkeypatch)

    def unexpected_full_validation(*args):
        raise AssertionError("Full response validation belongs to the service boundary")

    monkeypatch.setattr(
        "ai_accounting.kernel.response_contracts.validate_response", unexpected_full_validation
    )
    result = compact_workflow_issues(raw)
    assert result["schema_version"] == 2
    assert result["fact_issue_refs"] == [0, 0]


@pytest.mark.parametrize("state", ["empty", "open", "closed", "cross_month"])
def test_existing_queries_expand_to_the_original_result(tmp_path, monkeypatch, state):
    company = setup_company(tmp_path)
    if state == "closed":
        ready(company.engine, company.owner_confirmation, first="2026-01", last="2026-01")
        company.close("2026-01")
    elif state == "cross_month":
        company.save(
            obligation("quarterly_tax").model_copy(
                update={"end_period": "2026-03", "due_date": "2026-04-20"}
            ),
            "cross-month",
        )
    period = None if state == "empty" else "2026-01"
    original = raw_query(company, monkeypatch, period=period)
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        result = Worklist(company.engine).query(connection, as_of="2026-03-01", period=period)
    assert expand_workflow(result) == original
