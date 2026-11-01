"""Current management dependencies round-trip without weakening complete inputs."""

import pytest

from ai_accounting_modular.contracts.public import ReadInputs


def test_management_dependencies_preserve_all_existing_selectors():
    original = ReadInputs(
        business_ids=("business-one",),
        account_codes=("1002",),
        current_fact_ids=("business-one",),
        note_scopes=("business:business-one",),
    )
    stored = original.wire()
    assert ReadInputs.from_wire(stored) == original


@pytest.mark.parametrize(
    "damage",
    [
        "missing_business_ids",
        "missing_note_scopes",
        "missing_current_fact_ids",
        "unknown_key",
        "bad_note_type",
    ],
)
def test_management_selectors_do_not_relax_dependency_integrity(damage):
    stored = ReadInputs().wire()
    if damage.startswith("missing_"):
        del stored[damage.removeprefix("missing_")]
    elif damage == "unknown_key":
        stored["unrecognized"] = []
    else:
        stored["note_scopes"] = "company"
    with pytest.raises((ValueError, TypeError)):
        ReadInputs.from_wire(stored)
