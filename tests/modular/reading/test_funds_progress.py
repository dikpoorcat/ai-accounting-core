"""Actual funds rows advertise only the business content their progress panel can show."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator, ValidationError

from ai_accounting_modular.contracts.public import schema_document
from ai_accounting_modular.reading import _business_status
from ai_accounting_modular.reading.public import MonthScope
from tests.modular.application._working_case import case as case
from tests.modular.domains.financing._support import BANK, CASH, COUNTERPARTY, fact, payment

from .test_business_status import progress
from .test_funds import apply, capture, funding, initialize, page


def _capital():
    value = funding()
    value["data"]["funding_kind"] = "capital"
    return value


def _flags(case):
    return {
        row["subject_id"]: row["has_business_progress"]
        for row in page(case)["data"]["collections"]["movements"]["items"]
    }


def test_capital_income_reserve_are_passive_but_real_payments_loans_and_transfer_expand(
    case, monkeypatch,
):
    initialize(case)
    capital, loan = _capital(), funding()
    income = fact("operations.other_income", {
        "income_kind": "company_reward", "amount_fen": "10", "funds_account_id": BANK,
        "counterparty_id": COUNTERPARTY,
    }, actual="2026-01-04")
    reserve = fact("money.movement", {
        "type": "managed_reserve", "funds_account_id": BANK,
        "direction": "inflow", "amount_fen": "20", "counterparty_id": COUNTERPARTY,
    }, actual="2026-01-05")
    transfer = fact("money.transfer", {
        "type": "physical", "from_account_id": BANK, "to_account_id": CASH,
        "amount_fen": "30",
    }, actual="2026-01-06")
    expense = case.expense("100")
    expense["data"]["creditor_id"] = COUNTERPARTY
    paid = payment(expense, 100, slot="primary", party=COUNTERPARTY)
    apply(case, capital, loan, income, reserve, transfer, expense, paid)

    def no_detail_cards(*args, **kwargs):
        pytest.fail("funds availability must not pre-read business detail cards")

    with monkeypatch.context() as patch:
        patch.setattr(_business_status, "detail", no_detail_cards)
        capture(case)
    assert _flags(case) == {
        capital["business_id"]: False, loan["business_id"]: True,
        income["business_id"]: False, reserve["business_id"]: False,
        transfer["business_id"]: True, paid["business_id"]: True,
    }
    for business in (capital, income, reserve):
        data = progress(case, business["business_id"], settlement_view="historical")["data"]
        assert data["settlements"]["obligations"] == []
        assert data["current_followups"]["settlements"]["obligations"] == []
        assert data["collections"]["settlement_events"]["items"] == []


def test_later_new_obligation_invalidates_old_funds_and_preserves_followup_entry(case):
    initialize(case)
    capital = _capital()
    capital["data"]["funds_account_id"] = CASH
    (capital,) = apply(case, capital)
    capture(case)
    assert _flags(case)[capital["business_id"]] is False
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    # Refresh the January projection after closing before testing a future change.
    capture(case)
    revised = deepcopy(capital)
    revised["data"]["funding_kind"] = "loan"
    apply(case, mutations=[{"op": "replace", "expected_seq": 1, "fact": revised}],
          correction_period="2026-02")
    reader = case.kernel.reading
    month = MonthScope(case.company, "2026-01")
    assert reader.page("funds", month, {}).state == "preparing"
    assert reader.run_pending(case.company)
    delivered = reader.page("funds", month, {})
    assert delivered.state == "ready"
    assert delivered.payload["data"]["collections"]["movements"]["items"][0][
        "has_business_progress"
    ] is True
    data = progress(case, capital["business_id"], settlement_view="historical")["data"]
    assert data["settlements"]["obligations"] == []
    assert data["current_followups"]["settlements"]["obligations"][0][
        "source_amount_fen"
    ] == "2000"


@pytest.mark.parametrize("value", [None, "true", 1])
def test_progress_contract_requires_strict_boolean(case, value):
    initialize(case)
    apply(case, _capital())
    capture(case)
    data = page(case)["data"]
    row = data["collections"]["movements"]["items"][0]
    contract = schema_document("dashboard-response-v2.json", "dashboard_funds")
    validator = Draft202012Validator({
        "$defs": contract["$defs"], "$ref": "#/$defs/FundMovement",
    })
    validator.validate(row)
    for invalid in (value, ...):
        candidate = dict(row)
        if invalid is ...:
            del candidate["has_business_progress"]
        else:
            candidate["has_business_progress"] = invalid
        with pytest.raises(ValidationError):
            validator.validate(candidate)
