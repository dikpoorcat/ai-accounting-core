"""New management dependencies preserve existing, unrelated projection selectors."""

import pytest

from ai_accounting_modular.contracts.public import ReadInputs


def test_pre_description_projection_keeps_all_existing_dependencies():
    original = ReadInputs(business_ids=("business-one",), account_codes=("1002",))
    stored = original.wire()
    del stored["note_scopes"]
    assert ReadInputs.from_wire(stored) == original
    assert "note_scopes" not in stored


@pytest.mark.parametrize("damage", ["missing_business_ids", "unknown_key", "bad_note_type"])
def test_optional_note_selector_does_not_relax_existing_dependency_integrity(damage):
    stored = ReadInputs().wire()
    if damage == "missing_business_ids":
        del stored["business_ids"]
    elif damage == "unknown_key":
        stored["unrecognized"] = []
    else:
        stored["note_scopes"] = "company"
    with pytest.raises((ValueError, TypeError)):
        ReadInputs.from_wire(stored)
