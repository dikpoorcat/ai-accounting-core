"""Business context locates a failure without redefining its facts or error class."""

from copy import deepcopy
from uuid import uuid4

from ai_accounting_modular.contracts.public import ContractError, validate_contract


def test_unlocated_business_error_keeps_code_text_and_diagnostic():
    business, diagnostic = str(uuid4()), str(uuid4())
    error = ContractError("dependency_conflict", "明确的核验失败。", diagnostic_id=diagnostic)
    located = error.at_business(business)
    assert located is not error
    assert (located.code, located.message, located.diagnostic_id) == (
        error.code,
        error.message,
        diagnostic,
    )
    assert error.fact_issues == ()
    assert located.fact_issues == (
        {
            "code": error.code,
            "business_id": business,
            "field": "/",
            "meaning": "本业务核算或依赖核验",
            "message": error.message,
            "allowed_precision": [],
            "reusable_sources": [],
            "blocking_scope": "business",
        },
    )
    validate_contract("core.schema.json", "FactIssue", located.fact_issues[0])


def test_precise_upstream_issues_remain_unchanged_and_only_missing_identity_is_filled():
    business, upstream = str(uuid4()), str(uuid4())
    bare = {
        "code": "missing_value",
        "field": "/data/source",
        "meaning": "已明确的来源",
        "message": "保留原有问题。",
        "allowed_precision": ["source"],
        "reusable_sources": [],
        "blocking_scope": "business",
    }
    precise = {**bare, "business_id": upstream, "field": "/data/employee_id"}
    original = deepcopy([bare, precise])
    error = ContractError("needs_information", "原缺项正文。", fact_issues=[bare, precise])
    located = error.at_business(business)
    assert located.code == "needs_information"
    assert located.message == error.message
    assert located.fact_issues == ({**bare, "business_id": business}, precise)
    assert [bare, precise] == original


def test_long_original_message_is_not_lost_or_made_into_an_invalid_fact_issue():
    message = "完整核验原因。" * 1000
    error = ContractError("dependency_conflict", message)
    located = error.at_business(str(uuid4()))
    assert located.message == message
    assert located.code == "dependency_conflict"
    assert "错误正文" in located.fact_issues[0]["message"]
    validate_contract("core.schema.json", "FactIssue", located.fact_issues[0])
