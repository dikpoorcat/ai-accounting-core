"""Business inputs and adopted prepayment states have distinct responsibilities."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract

PARTY = "00000000-0000-4000-8000-000000000001"
SCHEMA = "business-facts.schema.json"


def customer():
    return {"side": "customer", "counterparty_id": PARTY, "amount_fen": "10100"}


@pytest.mark.parametrize(
    "point",
    [
        {
            "type": "explicit",
            "tax_period": "2026-01",
            "source": {"source_id": PARTY, "location": "老板明确的计税安排"},
        },
        {"type": "derived", "tax_point_id": PARTY},
        {"type": "owner_recognition_default"},
    ],
)
def test_complete_tax_point_needs_no_second_input_state(point):
    value = customer() | {"tax_point": point}
    before = deepcopy(value)
    assert validate_contract(SCHEMA, "CustomerAdvance", value) == before
    # Accounting keeps the computed state even when it was absent from the input.
    adopted = deepcopy(value)
    adopted["tax_point"].pop("source", None)
    with pytest.raises(ContractError):
        validate_contract(SCHEMA, "AccountingCustomerAdvance", adopted)
    adopted["tax_status"] = "not_due"
    assert validate_contract(SCHEMA, "AccountingCustomerAdvance", adopted) == adopted


def test_without_tax_point_only_explicit_not_due_is_complete():
    value = customer()
    for state in (None, "due"):
        raw = value if state is None else value | {"tax_status": state}
        with pytest.raises(ContractError):
            validate_contract(SCHEMA, "CustomerAdvance", raw)
    value["tax_status"] = "not_due"
    assert validate_contract(SCHEMA, "CustomerAdvance", value) == value
    assert validate_contract(SCHEMA, "AccountingCustomerAdvance", value) == value


def test_partial_input_can_be_retained_without_claiming_complete_accounting_facts():
    value = customer()
    assert validate_contract(SCHEMA, "PartialCustomerAdvance", value) == value
    with pytest.raises(ContractError):
        validate_contract(SCHEMA, "CustomerAdvance", value)
