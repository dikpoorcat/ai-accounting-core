"""Work arrangements and closing-node meaning at the public submission boundary."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract

SOURCE = "10000000-0000-4000-8000-000000000001"


def test_work_note_has_no_financial_head_and_clears_without_fake_source():
    note = {
        "scope": "work:2026-09",
        "expected_seq": 0,
        "text": "等老板反馈申报，已明确先准备报表。",
        "evidence_ids": [SOURCE],
    }
    validate_contract("core.schema.json", "NoteChange", note)
    validate_contract("core.schema.json", "NoteChange", {**note, "text": "", "evidence_ids": []})
    with pytest.raises(ContractError):
        validate_contract(
            "core.schema.json", "NoteChange", {**note, "expected_basis_head": "a" * 64}
        )
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "NoteChange", {**note, "text": ""})


def test_closing_node_is_explicit_and_independent_of_external_work_month():
    issue = {
        "issue_id": SOURCE,
        "scope": "quarterly",
        "period": "2026-10",
        "evidence_ids": [SOURCE],
        "missing_fields": [],
        "question": "第三季度申报结果待反馈。",
        "assignee": "owner",
        "action": "external_filing",
        "required_before_close": "2026-09",
    }
    validate_contract("core.schema.json", "CurrentIssue", issue)
    validate_contract(
        "core.schema.json",
        "CurrentIssue",
        {
            **issue,
            "action": "external_payment",
            "required_before_close": None,
            "question": "已记应付货款，按约定付款。",
        },
    )
    absent = deepcopy(issue)
    del absent["required_before_close"]
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "CurrentIssue", absent)
    with pytest.raises(ContractError):
        validate_contract(
            "core.schema.json", "CurrentIssue", {**issue, "action": "professional_check"}
        )


def test_close_requires_adopted_range_materials_and_head_not_only_a_cutoff():
    request = {
        "company_id": SOURCE,
        "action": "close",
        "actual_range": {"period_from": "2026-08", "period_to": "2026-09"},
        "basis": {
            "coverage": [
                {
                    "period_from": "2026-08",
                    "period_to": "2026-09",
                    "sources": [{"source_id": SOURCE}],
                }
            ]
        },
        "expected_material_version": "a" * 64,
        "expected_head": "b" * 64,
    }
    validate_contract("core.schema.json", "PeriodRequest", request)
    for field in ("actual_range", "basis", "expected_material_version", "expected_head"):
        missing = deepcopy(request)
        del missing[field]
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "PeriodRequest", missing)
    with pytest.raises(ContractError):
        validate_contract(
            "core.schema.json",
            "PeriodRequest",
            {
                "company_id": SOURCE,
                "action": "close",
                "period": "2026-09",
                "expected_head": "b" * 64,
            },
        )
