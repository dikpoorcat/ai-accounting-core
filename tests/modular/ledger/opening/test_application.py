"""A true opening is balance-only and continues into ordinary public business."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, ResourceCutoff, fact_source_paths
from ai_accounting_modular.domains.assets.public import create_assets
from ai_accounting_modular.ledger.opening.public import create_opening

from ._support import (
    BANK,
    CASH,
    FIXED,
    FUND,
    INTANGIBLE,
    PARTY,
    PERSON,
    PLATFORM,
    SOURCE,
    assemble,
    bank_and_equity,
    fact,
    get,
    member,
    opening,
    replace,
    request,
    result,
    source,
)


@pytest.fixture
def assembled(tmp_path):
    return assemble(tmp_path)


def test_real_bank_equity_opening_is_normalized_no_journal_or_cash_and_idempotent(assembled):
    app, company = assembled
    item = bank_and_equity()
    submitted = request(company, item)
    actual = app.accounting_apply(submitted)
    assert actual.get("completion") == "complete", actual
    actual_result = result(app, company, item)
    assert actual_result["vouchers"] == []
    assert actual_result["target"]["values"] == {
        "bookkeeping_start": "2026-07",
        "debit_fen": "200000",
        "credit_fen": "200000",
        "member_count": 2,
    }
    with app.storage.read_unit(company) as read:
        exported = create_opening().export_current([item["business_id"]], read)
        assert exported == (item,)
        assert app.ledger.fund_movements(BANK, "2026-07", "2026-07", read) == ()
    before = get(app, company, item["business_id"])
    assert app.accounting_apply(submitted)["accounting"] == "unchanged"
    assert get(app, company, item["business_id"]) == before
    # Inspect only this module's own persistence to prove members have one body.
    with app.storage.read_unit(company) as read:
        repo = read.repository("ledger.opening")
        row = repo._session.fetchall("SELECT schema_key,data_json FROM opening_fact", ())[0]
        assert row["schema_key"] == "opening.package/storage_projection/1"
        assert '"members"' not in row["data_json"]
        assert len(repo._session.fetchall("SELECT member_id FROM opening_member", ())) == 2


@pytest.mark.parametrize("basis", ["newly_established", "existing_ledger"])
def test_sourced_zero_opening_requires_no_artificial_zero_categories(assembled, basis):
    app, company = assembled
    item = opening(basis=basis)
    actual = app.accounting_apply(request(company, item))
    assert actual.get("completion") == "complete", actual
    assert result(app, company, item)["target"]["values"]["debit_fen"] == "0"
    assert result(app, company, item)["vouchers"] == []


def test_new_company_may_start_with_actual_capital_and_money(assembled):
    app, company = assembled
    item = bank_and_equity()
    item["data"]["basis"] = "newly_established"
    actual = app.accounting_apply(request(company, item))
    assert actual.get("completion") == "complete", actual


def test_opening_balance_error_has_no_partial_objects_sources_facts_or_documents(assembled):
    app, company = assembled
    item = bank_and_equity()
    item["data"]["members"][1]["data"]["balance_fen"] = "199999"
    actual = app.accounting_apply(request(company, item))
    assert actual["completion"] == "not_saved", actual
    assert actual["error"] == "dependency_conflict"
    with app.storage.read_unit(company) as read:
        assert read.head == 0
        assert create_opening().export_current([item["business_id"]], read) == ()


def test_opening_only_at_start_and_unique_for_the_company(assembled):
    app, company = assembled
    wrong = opening(period="2026-08")
    actual = app.accounting_apply(request(company, wrong))
    assert actual["completion"] == "not_saved", actual
    one, two = opening(), opening()
    actual = app.accounting_apply(request(company, one, two))
    assert actual["completion"] == "not_saved", actual
    assert actual["error"] == "conflict"
    assert app.accounting_apply(request(company, one))["completion"] == "complete"
    submitted = request(company, two)
    submitted.pop("objects")
    submitted.pop("sources")
    assert app.accounting_apply(submitted)["completion"] == "not_saved"


def test_open_correction_keeps_result_and_final_members_only(assembled):
    app, company = assembled
    item = bank_and_equity()
    assert app.accounting_apply(request(company, item))["completion"] == "complete"
    before = result(app, company, item)
    for m in item["data"]["members"]:
        m["data"]["balance_fen"] = "300000"
    actual = app.accounting_apply(replace(company, item))
    assert actual.get("completion") == "complete", actual
    after = result(app, company, item)
    assert after["reference"]["result_id"] == before["reference"]["result_id"]
    assert after["vouchers"] == []
    with app.storage.read_unit(company) as read:
        assert create_opening().export_current([item["business_id"]], read) == (item,)
        assert (
            len(
                read.repository("ledger.opening")._session.fetchall(
                    "SELECT member_id FROM opening_member", ()
                )
            )
            == 2
        )


def test_platform_and_cash_are_their_true_account_types(assembled):
    app, company = assembled
    item = opening(
        member({"type": "bank", "funds_account_id": PLATFORM, "balance_fen": "100"}),
        member({"type": "cash", "funds_account_id": CASH, "balance_fen": "200"}),
        member(
            {
                "type": "equity",
                "equity_kind": "paid_in_capital",
                "holder_or_basis_id": PERSON,
                "balance_fen": "300",
            }
        ),
    )
    actual = app.accounting_apply(request(company, item))
    assert actual.get("completion") == "complete", actual
    qs = result(app, company, item)["basis"]["adopted_quantities"]
    assert {
        q["account_code"]: q["debit_fen"]
        for q in qs
        if q["type"] == "opening_balance" and q["debit_fen"] != "0"
    } == {"1012": "100", "1001": "200"}


def test_every_member_source_is_required_and_exactly_indexed(assembled):
    app, company = assembled
    separate = str(uuid4())
    item = bank_and_equity()
    item["data"]["members"][0]["evidence_ids"] = [separate]
    submitted = request(company, item)
    assert app.accounting_apply(submitted)["completion"] == "not_saved"
    submitted["command_id"] = str(uuid4())
    submitted["sources"].append(source(separate))
    actual = app.accounting_apply(submitted)
    assert actual.get("completion") == "complete", actual
    locations = result(app, company, item)["basis"]["source_locations"]
    assert {x["source_id"] for x in locations} == {SOURCE, separate}
    assert fact_source_paths(item)[separate] == ("/data/members/0/evidence_ids/0",)


def test_money_fund_redeems_opening_cost_through_public_resources(assembled):
    app, company = assembled
    lot = member(
        {
            "type": "money_fund",
            "fund_id": FUND,
            "counterparty_id": PARTY,
            "original_lot_reference": "真实前期批次A",
            "cost_fen": "10000",
            "classification": "readily_redeemable_held_not_over_one_year",
            "cost_basis": "documented_cost",
        }
    )
    item = opening(
        lot,
        member(
            {
                "type": "equity",
                "equity_kind": "retained_earnings",
                "holder_or_basis_id": PARTY,
                "balance_fen": "10000",
            }
        ),
    )
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
                    "amount_fen": "6000",
                }
            ],
            "net_proceeds_fen": "6300",
        },
    )
    actual = app.accounting_apply(request(company, redeemed, item))
    assert actual.get("completion") == "complete", actual
    assert result(app, company, redeemed)["target"]["values"]["gain_loss_fen"] == "300"
    with app.storage.read_unit(company) as read:
        candidate = app.ledger.begin_candidate([], read)
        candidate.complete()
        (state,) = candidate.resource_states(
            [{"business_id": item["business_id"], "slot": "member:" + lot["member_id"] + ":cost"}],
            "cost",
            ResourceCutoff("2026-07"),
        )
        assert state.remaining_fen == 4000


def test_prior_contract_opening_principal_then_actual_repayment_and_interest(assembled):
    app, company = assembled
    contract = fact(
        "financing.loan_contract",
        {
            "lender_id": PARTY,
            "lender_type": "licensed_financial_institution",
            "currency": "CNY",
            "rate_type": "fixed",
            "annual_rate": "0.036",
            "day_count": "actual_360",
            "matures_on": "2027-01-01",
            "repayment": "bullet",
            "term": "long_term",
        },
        period="2026-01",
    )
    loan = member(
        {
            "type": "loan",
            "contract_id": contract["business_id"],
            "lender_id": PARTY,
            "principal_fen": "1000000",
            "accrued_interest_fen": "1000",
            "interest_start_on": "2026-07-01",
        }
    )
    item = opening(
        loan,
        member({"type": "bank", "funds_account_id": BANK, "balance_fen": "1000000"}),
        member(
            {
                "type": "equity",
                "equity_kind": "retained_earnings",
                "holder_or_basis_id": PARTY,
                "balance_fen": "-1000",
            }
        ),
    )
    ref = {"business_id": item["business_id"], "slot": "member:" + loan["member_id"] + ":principal"}
    interest = fact(
        "financing.interest_period",
        {"principal_source": ref, "start_on": "2026-07-01", "end_before": "2026-08-01"},
    )
    paid = fact(
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": "outflow",
            "amount_fen": "400000",
            "payment_method": "individual",
            "counterparty_id": PARTY,
            "allocations": [{"source": ref, "amount_fen": "400000"}],
        },
        actual="2026-07-16",
    )
    actual = app.accounting_apply(request(company, paid, interest, item, contract))
    assert actual.get("completion") == "complete", actual
    assert result(app, company, interest)["target"]["values"]["interest_fen"] == "2460"
    assert result(app, company, item)["vouchers"] == []


def test_asset_cards_preserve_fixed_intangible_start_and_prior_accumulation(tmp_path):
    app, company = assemble(tmp_path, asset_accounting=create_assets())
    cards = [
        member(
            {
                "type": "asset",
                "asset_id": FIXED,
                "asset_type": "fixed",
                "cost_fen": "12000",
                "accumulated_fen": "5000",
                "in_use_period": "2026-01",
                "useful_life_months": 12,
                "completed_months": 5,
                "residual_fen": "0",
                "benefit_area": "administration",
                "rounding": "floor_final_remainder",
            }
        ),
        member(
            {
                "type": "asset",
                "asset_id": INTANGIBLE,
                "asset_type": "intangible",
                "cost_fen": "12000",
                "accumulated_fen": "6000",
                "in_use_period": "2026-01",
                "useful_life_months": 12,
                "completed_months": 6,
                "residual_fen": "0",
                "benefit_area": "service",
                "rounding": "floor_final_remainder",
            }
        ),
    ]
    item = opening(
        *cards,
        member(
            {
                "type": "equity",
                "equity_kind": "retained_earnings",
                "holder_or_basis_id": PARTY,
                "balance_fen": "13000",
            }
        ),
    )
    actual = app.accounting_apply(request(company, item))
    assert actual.get("completion") == "complete", actual
    adopted = {
        q["asset_id"]: q
        for q in result(app, company, item)["basis"]["adopted_quantities"]
        if q["type"] == "asset_card"
    }
    assert adopted[FIXED]["consumption_start"] == "2026-02"
    assert adopted[INTANGIBLE]["consumption_start"] == "2026-01"
    assert "in_use_on" not in adopted[FIXED]
    bad = deepcopy(item)
    bad["data"]["members"][0]["data"]["accumulated_fen"] = "4999"
    bad["data"]["members"][2]["data"]["balance_fen"] = "13001"
    rejected = app.accounting_apply(replace(company, bad))
    assert rejected["completion"] == "not_saved", rejected
    assert "累计" in rejected["message"]


def test_withdrawal_removes_only_current_member_bodies_and_formal_balances(assembled):
    app, company = assembled
    item = bank_and_equity()
    assert app.accounting_apply(request(company, item))["completion"] == "complete"
    submitted = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [{"op": "withdraw", "expected_seq": 1, "business_id": item["business_id"]}],
    }
    actual = app.accounting_apply(submitted)
    assert actual.get("completion") == "complete", actual
    with app.storage.read_unit(company) as read:
        assert create_opening().export_current([item["business_id"]], read) == ()
        assert (
            read.repository("ledger.opening")._session.fetchall(
                "SELECT member_id FROM opening_member", ()
            )
            == []
        )
    assert get(app, company, item["business_id"])["businesses"][0]["state"]["state"] == "withdrawn"


@pytest.mark.parametrize("corrupt", ["kind", "position", "duplicated_parent", "missing_member"])
def test_normalized_members_cannot_silently_change_the_exported_current_fact(assembled, corrupt):
    app, company = assembled
    item = bank_and_equity()
    assert app.accounting_apply(request(company, item))["completion"] == "complete"
    with app.storage.write_unit(company) as unit:
        session = unit.repository("ledger.opening")._session
        if corrupt == "kind":
            session.execute("UPDATE opening_member SET kind='cash' WHERE kind='bank'", ())
        elif corrupt == "position":
            session.execute(
                "UPDATE opening_member SET data_json=json_set(data_json,'$.position',5)", ()
            )
        elif corrupt == "duplicated_parent":
            session.execute(
                "UPDATE opening_fact SET data_json=json_set(data_json,'$.data.members',json('[]'))",
                (),
            )
        else:
            session.execute("DELETE FROM opening_member WHERE kind='equity'", ())
    with pytest.raises(ContractError) as error:
        get(app, company, item["business_id"])
    assert error.value.code == "storage_corrupt"
