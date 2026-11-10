"""A work month belongs to an explicitly selected company request."""

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract

COMPANY = "00000000-0000-4000-8000-000000000001"


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"company_id": COMPANY},
        {"company_id": COMPANY, "work_period": "2026-09"},
        {"company_id": COMPANY, "work_period": "2026-09", "note_scopes": [], "issue_scopes": []},
    ],
)
def test_work_period_is_optional_but_precise_when_selected(payload):
    assert validate_contract("core.schema.json", "ContextRequest", payload) == payload


@pytest.mark.parametrize(
    "payload",
    [
        {"work_period": "2026-09"},
        {"company_id": COMPANY, "work_period": None},
        {"company_id": COMPANY, "work_period": "2026-9"},
        {"company_id": COMPANY, "work_period": "2026-13"},
        {"company_id": COMPANY, "work_period": "0000-01"},
        {"company_id": COMPANY, "work_period": 202609},
    ],
)
def test_work_period_rejects_missing_company_and_invalid_months(payload):
    with pytest.raises(ContractError) as error:
        validate_contract("core.schema.json", "ContextRequest", payload)
    assert error.value.code == "invalid_input"
