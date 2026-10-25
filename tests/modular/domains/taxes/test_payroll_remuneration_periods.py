"""One remuneration can be received and declared in several actual tax periods."""

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.taxes.public import create_taxes
from tests.modular.domains.people.test_people import Context
from tests.modular.domains.people.test_remuneration_tax import declaration, wage


@pytest.mark.parametrize("period", ["2026-02", "2026-03"])
def test_actual_declaration_tax_period_is_independent_of_initial_remuneration_component(period):
    original = wage(initial="10000")
    actual, _ = declaration(original, "2000", tax_period=period)
    context = Context(original, actual)
    result = create_taxes().evaluate_slot(actual["business_id"], "main", context)
    assert result["target"]["values"]["tax_period_end"] == period
    assert result["lines"] == result["obligations"] == result["fund_movements"] == []


def test_later_declaration_still_cannot_relabel_wages_as_bonus():
    original = wage(initial="10000")
    actual, _ = declaration(original, "2000")
    actual["data"]["rows"][0]["income_category"] = "bonus"
    with pytest.raises(ContractError, match="类别"):
        create_taxes().evaluate_slot(actual["business_id"], "main", Context(original, actual))


def test_payroll_declaration_person_reference_is_not_a_missing_owner_fact():
    original = wage(initial="10000")
    actual, _ = declaration(original, "2000")
    context = Context(original, actual)
    context.objects.pop(actual["data"]["rows"][0]["person_id"])
    with pytest.raises(ContractError) as caught:
        create_taxes().evaluate_slot(actual["business_id"], "main", context)
    assert caught.value.code == "invalid_input"
    assert caught.value.fact_issues[0]["code"] == "reference_not_found"
    assert caught.value.fact_issues[0]["field"] == "/data/rows/0/person_id"
