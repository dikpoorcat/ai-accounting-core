"""The AI's narrow payable read consumes the same cutoff composition as monthly reads."""

from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, validate_contract
from ai_accounting_modular.domains.people import _read_sources
from ai_accounting_modular.domains.people.public import create_people
from tests.modular.application._working_case import case as case

from .test_people import declaration, no_contributions
from .test_read_remuneration import activity, add, bank_object, payment, wage_with_actuals
from .test_read_remuneration_tax import tax_target, unpaid_wage


def positions(case, ids, month="2026-02", *, view="current"):
    with case.kernel.storage.read_unit(case.company) as read:
        result = create_people(ledger=case.kernel.ledger).remuneration_positions(
            ids, month, read, view=view
        )
    for item in result:
        validate_contract("core.schema.json", "RemunerationPositionItem", item)
    return result


def test_narrow_payable_read_uses_exact_wage_debt_without_a_company_opening(case, monkeypatch):
    wage = unpaid_wage()
    add(case, no_contributions(), wage)
    assert activity(case).summary.outstanding_employee_net_fen is None

    def forbidden(*_args, **_kwargs):
        pytest.fail("exact wage payables must not build the all-company payroll or roster")

    monkeypatch.setattr(_read_sources.Contents, "roster", forbidden)
    monkeypatch.setattr(type(create_people()), "remuneration_activity", forbidden)
    value = positions(case, [wage["business_id"]], "2026-01")[0]
    assert value["business_id"] == wage["business_id"]
    assert value["person_id"] == wage["data"]["person_id"]
    assert value["remuneration_type"] == "wage" and value["business_period"] == "2026-01"
    assert value["remaining_fen"] == value["payable_fen"] == "100000"
    assert value["tax_targets"] == []
    assert value["payment_composition_state"] == "unconfirmed"


@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_selected_cutoff_excludes_future_tax_and_already_paid_money(case, view):
    case.seed(expense=False)
    wage, bank = unpaid_wage(), bank_object()
    target = tax_target(wage, "10000", basis_type="actual_withholding")
    paid = payment(bank["object_id"], "2026-02", (wage["business_id"], "30000"))
    add(case, no_contributions(), wage, target, paid, objects=[bank])
    january = positions(case, [wage["business_id"]], "2026-01", view=view)[0]
    assert january["amounts"]["tax_fen"] == "0"
    assert january["remaining_fen"] == january["payable_fen"] == "100000"
    assert january["tax_targets"] == []
    assert january["payment_composition_state"] == "unconfirmed"
    february = positions(case, [wage["business_id"]], view=view)[0]
    assert february["amounts"]["tax_fen"] == "10000"
    assert february["amounts"]["net_fen"] == "90000"
    assert february["remaining_fen"] == february["payable_fen"] == "60000"
    assert february["tax_state"] == "actual_withholding"
    assert february["payment_composition_state"] == "confirmed"
    assert february["tax_targets"][0]["business_id"] == target["business_id"]


def test_absorbed_hold_is_not_subtracted_again_from_unpaid_capacity(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    declared, reference = declaration(wage, "40000")
    wage["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    bank = bank_object()
    target = tax_target(wage, "40000", tax_period="2026-01")
    paid = payment(bank["object_id"], "2026-02", (wage["business_id"], "30000"))
    add(case, wage, employment, actual, declared, target, paid, objects=[bank])
    value = positions(case, [wage["business_id"]])[0]
    assert value["amounts"]["net_fen"] == "490000"
    assert value["amounts"]["held_fen"] == "0"
    assert value["remaining_fen"] == value["payable_fen"] == "460000"
    assert value["payment_composition_state"] == "confirmed"


def test_partial_hold_and_fully_paid_wage_do_not_become_an_extra_payment(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    declared, reference = declaration(wage, "40000")
    wage["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    bank = bank_object()
    target = tax_target(wage, "35000", tax_period="2026-01")
    paid = payment(bank["object_id"], "2026-02", (wage["business_id"], "490000"))
    add(case, wage, employment, actual, declared, target, paid, objects=[bank])
    value = positions(case, [wage["business_id"]])[0]
    assert value["amounts"]["held_fen"] == value["remaining_fen"] == "5000"
    assert value["payable_fen"] == "0"


def test_narrow_read_keeps_evidenced_zero_confirmation(case):
    case.seed(expense=False)
    wage = unpaid_wage()
    target = tax_target(wage, "0")
    add(case, no_contributions(), wage, target)
    value = positions(case, [wage["business_id"]])[0]
    assert value["tax_state"] == "final_statement"
    assert value["tax_targets"][0]["business_id"] == target["business_id"]
    assert value["payable_fen"] == "100000"
    assert value["payment_composition_state"] == "confirmed"


def test_withdrawn_closed_wage_is_explicit_and_historical_wage_remains_available(case):
    case.seed(expense=False)
    wage = unpaid_wage()
    add(case, no_contributions(), wage)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    withdrawn = case.app.accounting_apply(
        case.request(
            mutations=[{"op": "withdraw", "business_id": wage["business_id"], "expected_seq": 1}],
            correction_period="2026-02",
            explanation="合成误记工资撤去。",
        )
    )
    assert withdrawn["completion"] == "complete", withdrawn
    assert positions(case, [wage["business_id"]]) == (
        {"business_id": wage["business_id"], "state": "withdrawn"},
    )
    original = positions(case, [wage["business_id"]], "2026-01", view="as_posted")[0]
    assert original["state"] == "active" and original["payable_fen"] == "100000"


def test_missing_future_or_non_payroll_identity_fails_instead_of_silently_omitting(case):
    case.seed(expense=False)
    wage = unpaid_wage()
    add(case, no_contributions(), wage)
    for ids, month, code in (
        ([wage["business_id"], str(uuid4())], "2026-02", "not_found"),
        ([no_contributions()["business_id"]], "2026-02", "invalid_input"),
        ([wage["business_id"]], "2025-12", "not_found"),
    ):
        with pytest.raises(ContractError) as failure:
            positions(case, ids, month)
        assert failure.value.code == code
