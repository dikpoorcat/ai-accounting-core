"""Business notes share current-note storage without adopting Fact hashes or month bases."""

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract

BUSINESS = "abcdefab-cdef-4abc-8def-abcdefabcdef"
SOURCE = "30000000-0000-4000-8000-000000000001"
SCOPE = "business:" + BUSINESS


def change():
    return {"scope": SCOPE, "text": "仓库经营费用", "evidence_ids": [SOURCE], "expected_seq": 0}


def test_business_note_accepts_existing_text_boundary_and_explicit_empty_clear():
    note = change()
    validate_contract("core.schema.json", "NoteChange", note | {"text": "实" * 20000})
    validate_contract("core.schema.json", "NoteChange", note | {"text": "", "evidence_ids": []})
    for scope in ("company", "period:2026-01"):
        old = note | {"scope": scope, "text": "", "evidence_ids": []}
        if scope.startswith("period:"):
            old["expected_basis_head"] = "month-basis"
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "NoteChange", old)


@pytest.mark.parametrize(
    "mutation",
    [
        {"scope": "business:" + BUSINESS.upper()},
        {"scope": "business:" + BUSINESS + "\n"},
        {"scope": "business:not-a-uuid"},
        {"evidence_ids": []},
        {"text": "", "evidence_ids": [SOURCE]},
        {"text": " \t\r\n　"},
        {"text": "实" * 20001},
        {"expected_basis_head": "fact-hash"},
        {"text_format": "markdown"},
        {"evidence_ids": [SOURCE] * 501},
    ],
)
def test_business_note_rejects_invalid_scope_body_source_and_basis(mutation):
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "NoteChange", change() | mutation)


def test_current_business_note_has_null_basis_and_checked_empty_state():
    current = {
        "scope": SCOPE,
        "seq": 1,
        "state": "current",
        "text": "仓库经营费用",
        "basis_head": None,
        "evidence_ids": [SOURCE],
    }
    validate_contract("core.schema.json", "CurrentNote", current)
    cleared = current | {"state": "needs_refresh", "text": None, "evidence_ids": []}
    validate_contract("core.schema.json", "CurrentNote", cleared)
    for bad in (
        current | {"basis_head": "old-fact-digest"},
        current | {"evidence_ids": []},
        current | {"text": "　\n "},
        cleared | {"basis_head": "old-fact-digest"},
        cleared | {"evidence_ids": [SOURCE]},
        cleared | {"text": "留下的旧正文"},
    ):
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "CurrentNote", bad)


def test_standard_business_note_requires_body_and_source_without_a_small_global_row_cap():
    document = {key: value for key, value in change().items() if key != "expected_seq"}
    validate_contract("core.schema.json", "DocumentNote", document)
    for mutation in ({"evidence_ids": []}, {"text": ""}, {"text": "　\n "}):
        with pytest.raises(ContractError):
            validate_contract("core.schema.json", "DocumentNote", document | mutation)
    # Company/month notes never needed a practical per-business row capacity.
    # A common notes shard now also carries individual business descriptions.
    notes = []
    for index in range(12001):
        row = document | {"scope": f"business:10000000-0000-4000-8000-{index:012x}"}
        notes.append(row)
    validate_contract(
        "core.schema.json",
        "NotesShard",
        {"format": "ai-accounting-standard/1", "key": "notes", "notes": notes},
    )
