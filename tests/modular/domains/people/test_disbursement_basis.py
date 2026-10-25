"""An evidenced payment arrangement is independent of tax filing and actual payment."""

from copy import deepcopy
from hashlib import sha256
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import validate_contract
from tests.modular.application._working_case import case as case

from .test_application import assembled as assembled
from .test_application import get, request
from .test_people import EMPLOYMENT, PERSON, SOURCE, declaration, evaluate, no_contributions
from .test_people import case as business_case
from .test_read_remuneration import activity, add, bank_object, correct, payment, wage_with_actuals
from .test_read_remuneration_positions import positions
from .test_read_remuneration_tax import tax_target


def current_payable(source_id=SOURCE):
    return {
        "type": "current_payable",
        "source": {"source_id": source_id, "location": "本次按当前可付余额发放，无额外暂扣"},
    }


def pending(name):
    fact = business_case(name)
    fact["data"].pop("tax_basis", None)
    return fact


@pytest.mark.parametrize("name", ["B17Z", "B17B", "B17L"])
def test_current_payable_does_not_create_tax_zero_payment_or_change_accounting(name):
    fact = pending(name)
    before = evaluate(fact, no_contributions())
    fact["data"]["disbursement_basis"] = current_payable()
    validate_contract("business-facts.schema.json", "PartialFact", fact)
    after = evaluate(fact, no_contributions())
    for field in ("target", "lines", "obligations", "settlements", "fund_movements"):
        assert after[field] == before[field]
    assert after["target"]["values"]["tax_state"] == "not_withheld_not_filed"
    assert "tax_period" not in after["target"]["values"]
    assert after["basis"]["input"]["disbursement_basis"] == current_payable()
    assert current_payable()["source"] in after["basis"]["source_locations"]


@pytest.mark.parametrize("name", ["B17Z", "B17B", "B17L"])
def test_declared_tax_hold_has_one_meaning_for_wages_bonuses_and_labor(name):
    fact = business_case(name)
    fact["data"]["tax_basis"] = deepcopy(business_case("B17B")["data"]["tax_basis"])
    if name == "B17L":
        fact["data"]["withholding_state"] = "deduction_established"
    declared, reference = declaration(fact, "20000")
    fact["data"]["disbursement_basis"] = {"type": "declared_tax", **reference}
    plan = evaluate(fact, no_contributions(), declared)
    assert plan["target"]["values"]["net_fen"] == "90000"
    assert plan["target"]["values"]["tax_fen"] == "10000"
    assert plan["target"]["values"]["held_fen"] == "10000"
    assert plan["target"]["values"]["disbursement_target_fen"] == "80000"
    assert plan["obligations"][0]["amount_fen"] == "90000"
    assert declared["data"]["rows"][0]["source"] in plan["basis"]["source_locations"]


@pytest.mark.parametrize("name", ["B17Z", "B17B", "B17L"])
def test_open_correction_confirms_current_payable_and_can_remove_arrangement(case, name):
    fact = pending(name)
    add(case, no_contributions(), fact)
    before = get(case.app, case.company, fact["business_id"])["businesses"][0]["results"][0]
    initial = positions(case, [fact["business_id"]], "2026-01")[0]
    assert initial["payment_composition_state"] == "unconfirmed"
    fact["data"]["disbursement_basis"] = current_payable()
    correct(case, fact)
    confirmed = positions(case, [fact["business_id"]], "2026-01")[0]
    assert confirmed["payment_composition_state"] == "confirmed"
    assert confirmed["tax_state"] == "not_withheld_not_filed"
    assert confirmed["payable_fen"] == initial["payable_fen"] == "100000"
    assert confirmed["amounts"] == initial["amounts"]
    after = get(case.app, case.company, fact["business_id"])["businesses"][0]["results"][0]
    assert after["target"] == before["target"]
    assert after["reference"]["result_id"] == before["reference"]["result_id"]
    assert after["reference"]["voucher_number"] == before["reference"]["voucher_number"]
    assert len(after["vouchers"]) == 1

    fact["data"].pop("disbursement_basis")
    correct(case, fact, seq=2)
    removed = positions(case, [fact["business_id"]], "2026-01")[0]
    assert removed["payment_composition_state"] == "unconfirmed"
    assert removed["amounts"] == initial["amounts"]


def test_confirmation_keeps_contributions_partial_payment_and_later_tax_in_current_capacity(case):
    case.seed(expense=False)
    wage, employment, actual = wage_with_actuals()
    wage["data"].pop("tax_basis")
    wage["data"]["disbursement_basis"] = current_payable()
    bank = bank_object()
    paid = payment(bank["object_id"], "2026-02", (wage["business_id"], "100000"))
    add(case, employment, actual, wage, paid, objects=[bank])
    before_tax = positions(case, [wage["business_id"]])[0]
    assert before_tax["payment_composition_state"] == "confirmed"
    assert before_tax["tax_state"] == "not_withheld_not_filed"
    assert before_tax["amounts"]["employee_social_fen"] == "50000"
    assert before_tax["amounts"]["employee_housing_fen"] == "20000"
    assert before_tax["amounts"]["net_fen"] == "530000"
    assert before_tax["remaining_fen"] == before_tax["payable_fen"] == "430000"

    target = tax_target(wage, "20000", basis_type="actual_withholding")
    add(case, target)
    after_tax = positions(case, [wage["business_id"]])[0]
    assert after_tax["payment_composition_state"] == "confirmed"
    assert after_tax["tax_state"] == "actual_withholding"
    assert after_tax["amounts"]["tax_fen"] == "20000"
    assert after_tax["remaining_fen"] == after_tax["payable_fen"] == "410000"
    assert after_tax["tax_targets"][0]["business_id"] == target["business_id"]


def test_closed_month_payment_arrangement_uses_new_adoption_without_rewriting_prior_state(case):
    case.seed(expense=False)
    wage = pending("B17Z")
    add(case, no_contributions(), wage)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    before = get(case.app, case.company, wage["business_id"])["businesses"][0]["results"][0]
    wage["data"]["disbursement_basis"] = current_payable()
    correct(case, wage, period="2026-02")
    after = get(case.app, case.company, wage["business_id"])["businesses"][0]["results"][0]
    assert after["target"] == before["target"]
    assert after["vouchers"] == []
    assert after["reference"]["result_id"] != before["reference"]["result_id"]
    assert positions(case, [wage["business_id"]])[0]["payment_composition_state"] == "confirmed"
    historical = positions(case, [wage["business_id"]], "2026-01", view="as_posted")[0]
    assert historical["payment_composition_state"] == "unconfirmed"
    february = activity(case, "2026-02")
    assert february.entries == ()
    assert february.summary.amounts.total_expense_fen == 0


def test_missing_payment_arrangement_source_rolls_back_whole_command(assembled):
    app, company_id = assembled
    wage = pending("B17Z")
    wage["data"]["disbursement_basis"] = current_payable(str(uuid4()))
    saved = app.accounting_apply(request(company_id, no_contributions(), wage))
    assert saved["completion"] == "not_saved", saved
    assert saved["error"] == "invalid_input", saved
    with app.storage.read_unit(company_id) as read:
        assert app.identity.get_batch([PERSON], read) == ()
        assert app.ledger.get_results([wage["business_id"], EMPLOYMENT], read) == ()
        assert app.records.resolve_sources([SOURCE], read) == ()


def test_registered_arrangement_source_can_be_reused_without_copying_it_into_evidence_ids(case):
    wage = pending("B17Z")
    source_id = str(uuid4())
    wage["data"]["disbursement_basis"] = current_payable(source_id)
    wire = request(case.company, no_contributions(), wage)
    text = "合成发放安排：本次不扣留未明款项，按内核当前可付余额发放。"
    wire["sources"].append(
        {
            "source_id": source_id,
            "kind": "owner_statement",
            "text": text,
            "sha256": sha256(text.encode()).hexdigest(),
            "archive_month": "2026-01",
        }
    )
    saved = case.app.accounting_apply(wire)
    assert saved["completion"] == "complete", saved
    assert source_id not in wage["evidence_ids"]
    result = get(case.app, case.company, wage["business_id"])["businesses"][0]["results"][0]
    assert current_payable(source_id)["source"] in result["basis"]["source_locations"]
    assert (
        positions(case, [wage["business_id"]], "2026-01")[0]["payment_composition_state"]
        == "confirmed"
    )
