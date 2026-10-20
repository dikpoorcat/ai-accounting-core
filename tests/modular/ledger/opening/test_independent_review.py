"""Independent opening counterexamples through public module and application ports."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, ResourceCutoff
from ai_accounting_modular.ledger.opening.public import create_opening

from ._support import (
    BANK,
    FUND,
    PARTY,
    PERSON,
    assemble,
    bank_and_equity,
    fact,
    get,
    loan_opening,
    member,
    opening,
    replace,
    request,
    result,
)


def test_repaid_principal_with_unpaid_prior_interest_remains_a_real_opening_liability(tmp_path):
    app, company = assemble(tmp_path)
    contract, item, loan = loan_opening()
    loan = item["data"]["members"][0]
    loan["data"].update(principal_fen="0", accrued_interest_fen="100")
    item["data"]["members"][1]["data"]["balance_fen"] = "100"
    saved = app.accounting_apply(request(company, item, contract))
    assert saved.get("completion") == "complete", saved
    opening_result = result(app, company, item)
    assert opening_result["vouchers"] == []
    ref = {"business_id": item["business_id"], "slot": "member:" + loan["member_id"] + ":interest"}
    with app.storage.read_unit(company) as read:
        (liability,) = app.ledger.resource_definitions([ref], "obligation", read)
        assert liability.value["account_code"] == "2231"
        assert liability.value["amount_fen"] == "100"


@pytest.mark.parametrize("corruption", ["last_member", "all_members", "balanced_amounts"])
def test_export_itself_detects_corruption_of_the_normalized_member_set(tmp_path, corruption):
    app, company = assemble(tmp_path)
    item = bank_and_equity()
    saved = app.accounting_apply(request(company, item))
    assert saved["completion"] == "complete", saved
    with app.storage.write_unit(company) as unit:
        session = unit.repository("ledger.opening")._session
        if corruption == "last_member":
            session.execute(
                "DELETE FROM opening_member WHERE member_id=?",
                (item["data"]["members"][-1]["member_id"],),
            )
        elif corruption == "all_members":
            session.execute("DELETE FROM opening_member", ())
        else:
            session.execute(
                "UPDATE opening_member "
                "SET data_json=json_set(data_json,'$.member.data.balance_fen','300000')",
                (),
            )
    with app.storage.read_unit(company) as read, pytest.raises(ContractError) as error:
        create_opening().export_current([item["business_id"]], read)
    assert error.value.code == "storage_corrupt"


def test_prior_tax_source_is_available_from_book_start_but_never_before_it(tmp_path):
    app, company = assemble(tmp_path)
    tax = member(
        {
            "type": "tax",
            "authority_id": PARTY,
            "tax_kind": "vat",
            "tax_period": "2026-06",
            "balance_kind": "payable",
            "amount_fen": "100",
        }
    )
    item = opening(tax, member({"type": "bank", "funds_account_id": BANK, "balance_fen": "100"}))
    saved = app.accounting_apply(request(company, item))
    assert saved["completion"] == "complete", saved
    ref = {"business_id": item["business_id"], "slot": "member:" + tax["member_id"]}
    with app.storage.read_unit(company) as read:
        (definition,) = app.ledger.resource_definitions([ref], "obligation", read)
        assert definition.value["business_period"] == "2026-06"
        candidate = app.ledger.begin_candidate([], read)
        candidate.complete()
        with pytest.raises(ContractError) as error:
            candidate.resource_states([ref], "obligation", ResourceCutoff("2026-06"))
        assert error.value.code == "dependency_conflict"
        (after,) = candidate.resource_states([ref], "obligation", ResourceCutoff("2026-07"))
        assert after.definition.value["amount_fen"] == "100" and after.remaining_fen == 100


@pytest.mark.parametrize("direction,code", [("receivable", "1221"), ("payable", "2241")])
def test_opening_deposit_keeps_its_real_nature_in_the_shared_controlled_accounts(
    tmp_path, direction, code
):
    app, company = assemble(tmp_path)
    deposit = member(
        {
            "type": "obligation",
            "counterparty_id": PARTY,
            "nature": "deposit_" + direction,
            "amount_fen": "100",
            "original_business_reference": "未清真实押金",
        }
    )
    opposite = (
        {"type": "bank", "funds_account_id": BANK, "balance_fen": "100"}
        if direction == "payable"
        else {
            "type": "equity",
            "equity_kind": "retained_earnings",
            "holder_or_basis_id": PARTY,
            "balance_fen": "100",
        }
    )
    item = opening(deposit, member(opposite))
    saved = app.accounting_apply(request(company, item))
    assert saved["completion"] == "complete", saved
    ref = {"business_id": item["business_id"], "slot": "member:" + deposit["member_id"]}
    with app.storage.read_unit(company) as read:
        (definition,) = app.ledger.resource_definitions([ref], "obligation", read)
        assert definition.value["account_code"] == code
        assert definition.value["category"] == "deposit"


def test_consumed_opening_cost_cannot_be_removed_or_reduced_below_real_redemption(tmp_path):
    app, company = assemble(tmp_path)
    lot = member(
        {
            "type": "money_fund",
            "fund_id": FUND,
            "counterparty_id": PARTY,
            "original_lot_reference": "opening-lot",
            "cost_fen": "1000",
            "classification": "readily_redeemable_held_not_over_one_year",
            "cost_basis": "documented_cost",
        }
    )
    equity = member(
        {
            "type": "equity",
            "equity_kind": "retained_earnings",
            "holder_or_basis_id": PARTY,
            "balance_fen": "1000",
        }
    )
    item = opening(lot, equity)
    redeemed = fact(
        "investment.money_fund_redemption",
        {
            "fund_id": FUND,
            "counterparty_id": PARTY,
            "classification": "readily_redeemable_held_not_over_one_year",
            "cost_basis": "documented_cost",
            "costs": [
                {
                    "source": {
                        "business_id": item["business_id"],
                        "slot": "member:" + lot["member_id"] + ":cost",
                    },
                    "amount_fen": "600",
                }
            ],
            "net_proceeds_fen": "630",
        },
    )
    saved = app.accounting_apply(request(company, redeemed, item))
    assert saved["completion"] == "complete", saved
    before = deepcopy(get(app, company, item["business_id"], redeemed["business_id"]))
    revised = deepcopy(item)
    revised["data"]["members"][0]["data"]["cost_fen"] = "500"
    revised["data"]["members"][1]["data"]["balance_fen"] = "500"
    refused = app.accounting_apply(replace(company, revised))
    assert refused["completion"] == "not_saved", refused
    assert get(app, company, item["business_id"], redeemed["business_id"]) == before
    withdrawn = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "withdraw", "expected_seq": 1, "business_id": item["business_id"]}],
    }
    assert app.accounting_apply(withdrawn)["completion"] == "not_saved"
    assert get(app, company, item["business_id"], redeemed["business_id"]) == before


def test_prior_payroll_tax_can_be_paid_in_start_month_without_a_fake_old_payment(tmp_path):
    app, company = assemble(tmp_path)
    withheld = member(
        {
            "type": "payroll_payable",
            "person_id": PERSON,
            "recipient_id": PARTY,
            "payroll_period": "2026-04",
            "component": "tax",
            "tax_period": "2026-06",
            "amount_fen": "100",
        }
    )
    item = opening(
        withheld, member({"type": "bank", "funds_account_id": BANK, "balance_fen": "100"})
    )
    paid = fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": "outflow",
            "amount_fen": "100",
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {
                        "business_id": item["business_id"],
                        "slot": "member:" + withheld["member_id"],
                    },
                    "amount_fen": "100",
                }
            ],
        },
        actual="2026-07-02",
    )
    saved = app.accounting_apply(request(company, paid, item))
    assert saved["completion"] == "complete", saved
    with app.storage.read_unit(company) as read:
        assert app.ledger.fund_movements(BANK, "2026-01", "2026-06", read) == ()
        (movement,) = app.ledger.fund_movements(BANK, "2026-07", "2026-07", read)
        assert movement.value["cashflow_parts"] == [
            {"category": "tax_payments", "amount_fen": "100"}
        ]
    assert result(app, company, item)["vouchers"] == []
