"""Fact references remain navigable even when an actual filing supplies the amount."""

import pytest

from ai_accounting_modular.contracts.public import validate_contract
from ai_accounting_modular.domains.taxes.public import create_taxes

from ._fixtures import assessment, income_inputs


class NoCalculation:
    def __getattr__(self, name):
        raise AssertionError(f"A direct fact declaration must not read {name}")


@pytest.mark.parametrize("income", [False, True])
def test_explicit_policy_and_prepayment_references_do_not_require_available_results(income):
    current = income_inputs()[0] if income else assessment()
    validate_contract("business-facts.schema.json", "Fact", current)
    actual = create_taxes().declare_relations(
        [{"op": "create", "expected_seq": 0, "fact": current}], NoCalculation()
    )
    expected = (
        {
            current["data"]["policy_id"]: "policy",
            current["data"]["prepayment_basis_id"]: "calculation",
        }
        if income
        else {
            current["data"]["vat_policy_id"]: "policy",
            current["data"]["surtax_policy_id"]: "policy",
        }
    )
    assert {row["producer_id"]: row["relation_kind"] for row in actual} == expected
    for row in actual:
        validate_contract("business-facts.schema.json", "DirectRelation", row)
        assert row["consumer_business_id"] == current["business_id"]
        assert row["producer_kind"] == "business"


def test_an_assessment_without_policy_fields_has_no_fabricated_policy_reference():
    current = assessment(policies=False)
    assert (
        create_taxes().declare_relations(
            [{"op": "create", "expected_seq": 0, "fact": current}], NoCalculation()
        )
        == ()
    )
