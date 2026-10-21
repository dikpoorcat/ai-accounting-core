"""Source retirement has its own strict result without changing source reads."""

from copy import deepcopy
from typing import get_type_hints

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    SourceDiscardResult,
    SourcesCommandResult,
    SourcesResult,
    validate_contract,
)

COMPANY = "10000000-0000-4000-8000-000000000001"


def source_ids(count):
    return [f"40000000-0000-4000-8000-{index:012x}" for index in range(count)]


def result():
    return {
        "company_id": COMPANY,
        "discarded_source_ids": source_ids(1),
        "missing_source_ids": [],
        "file_cleanup": {"state": "complete", "issues": []},
    }


@pytest.mark.parametrize("count", [1, 500])
def test_discard_accepts_explicit_bounded_source_identity_batches(count):
    request = {"company_id": COMPANY, "action": "discard", "source_ids": source_ids(count)}
    assert validate_contract("core.schema.json", "SourcesRequest", request) == request


@pytest.mark.parametrize("ids", [[], source_ids(501), source_ids(1) * 2, [True], ["not-a-uuid"]])
def test_discard_rejects_empty_excessive_duplicate_or_invalid_source_ids(ids):
    with pytest.raises(ContractError):
        validate_contract(
            "core.schema.json",
            "SourcesRequest",
            {"company_id": COMPANY, "action": "discard", "source_ids": ids},
        )


def test_discard_request_does_not_accept_file_paths_or_implicit_selection():
    request = {"company_id": COMPANY, "action": "discard", "source_ids": source_ids(1)}
    for extra in ({"relative_path": "originals/wrong.pdf"}, {"period_from": "2026-01"}):
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "SourcesRequest", request | extra)
    del request["source_ids"]
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "SourcesRequest", request)


def test_result_union_keeps_read_results_and_pending_cleanup_distinct():
    old = {"company_id": COMPANY, "sources": [], "missing_source_ids": []}
    assert validate_contract("core.schema.json", "SourcesResult", old) == old
    assert validate_contract("core.schema.json", "SourcesCommandResult", old) == old
    for cleanup in (
        {"state": "complete", "issues": []},
        {"state": "pending", "issues": []},
        {
            "state": "pending",
            "issues": [{"code": "file_busy", "message": "原件正在读取，稍后接续。"}],
        },
    ):
        value = result() | {"file_cleanup": cleanup}
        assert validate_contract("core.schema.json", "SourceDiscardResult", value) == value
        assert validate_contract("core.schema.json", "SourcesCommandResult", value) == value
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "SourcesResult", value)
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "SourceDiscardResult", old)
    assert get_type_hints(SourceDiscardResult).keys() == result().keys()
    assert set(SourcesCommandResult.__value__.__args__) == {SourcesResult, SourceDiscardResult}


@pytest.mark.parametrize(
    "field,value",
    [
        ("discarded_source_ids", source_ids(501)),
        ("discarded_source_ids", source_ids(1) * 2),
        ("missing_source_ids", ["not-a-uuid"]),
        ("file_cleanup", {"state": "succeeded", "issues": []}),
        ("file_cleanup", {"state": "complete"}),
        ("file_cleanup", {"state": "pending", "issues": [{"code": "busy"}]}),
        ("file_cleanup", {"state": "pending", "issues": [{"code": "", "message": "原因"}]}),
    ],
)
def test_discard_result_requires_actual_complete_or_pending_cleanup_shape(field, value):
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "SourceDiscardResult", result() | {field: value})


def test_result_is_closed_at_every_level_and_exposes_no_internal_cleanup_markers():
    original = result()
    original["file_cleanup"]["issues"] = [{"code": "file_busy", "message": "待接续。"}]
    for path, field in (
        ((), "marker_ids"),
        (("file_cleanup",), "pending_source_ids"),
        (("file_cleanup", "issues", 0), "marker_id"),
    ):
        value = deepcopy(original)
        target = value
        for part in path:
            target = target[part]
        target[field] = source_ids(1)
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "SourceDiscardResult", value)
    for field in original:
        value = deepcopy(original)
        del value[field]
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "SourceDiscardResult", value)
