"""Six real domain facts use application, identity, ledger, records and money."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import PageQuery
from ai_accounting_modular.domains.financing.public import create_financing, create_investment
from ai_accounting_modular.money.public import MovementQuery

from ._support import (
    BANK,
    CASH,
    COUNTERPARTY,
    OWNER,
    assemble,
    change,
    get,
    payment,
    request,
    result,
    six,
)


@pytest.fixture
def assembled(tmp_path):
    return assemble(tmp_path)


def test_real_six_fact_chain_and_fund_settlements_are_balanced_idempotent(assembled):
    app, company = assembled
    owner, contract, loan, interest, purchase, redeemed = six()
    paid = payment(purchase, 100000, slot="primary", party=COUNTERPARTY)
    received = payment(
        redeemed, 103000, slot="primary", party=COUNTERPARTY, direction="inflow", day="2026-01-21"
    )
    submitted = request(
        company, received, interest, redeemed, paid, purchase, loan, contract, owner
    )
    actual = app.accounting_apply(submitted)
    assert actual.get("completion") == "complete", actual
    assert result(app, company, interest)["target"]["values"]["interest_fen"] == "3100"
    assert result(app, company, redeemed)["target"]["values"]["gain_loss_fen"] == "3000"
    assert result(app, company, contract)["vouchers"] == []
    with app.storage.read_unit(company) as read:
        money = app.money.query_movements(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": read.view,
            },
            MovementQuery(BANK),
            PageQuery(),
            read,
        )
        assert (
            sum(
                int(x["movement"]["amount_fen"])
                * (1 if x["movement"]["direction"] == "inflow" else -1)
                for x in money.items
            )
            == 1_103_000
        )
        assert len(money.items) == 4
    before = get(
        app,
        company,
        *[x["business_id"] for x in (owner, contract, loan, interest, purchase, redeemed)],
    )
    assert app.accounting_apply(submitted)["accounting"] == "unchanged"
    assert (
        get(
            app,
            company,
            *[x["business_id"] for x in (owner, contract, loan, interest, purchase, redeemed)],
        )
        == before
    )


def test_combined_principal_interest_payment_has_no_result_cycle(assembled):
    app, company = assembled
    _, contract, loan, interest, _, _ = six()
    paid = payment(loan, 402460, day="2026-01-16")
    paid["data"]["allocations"] = [
        {
            "source": {"business_id": loan["business_id"], "slot": "principal"},
            "amount_fen": "400000",
        },
        {
            "source": {"business_id": interest["business_id"], "slot": "interest"},
            "amount_fen": "2460",
        },
    ]
    actual = app.accounting_apply(request(company, paid, interest, loan, contract))
    assert actual.get("completion") == "complete", actual
    calculated = result(app, company, interest)
    assert calculated["target"]["values"]["interest_fen"] == "2460"
    quantities = [
        x for x in calculated["basis"]["adopted_quantities"] if x["type"] == "loan_principal"
    ]
    assert [(x["start_on"], x["end_before"], x["outstanding_fen"]) for x in quantities] == [
        ("2026-01-01", "2026-01-16", "1000000"),
        ("2026-01-16", "2026-02-01", "600000"),
    ]


def test_new_repayment_recalculates_interest_then_replacement_reaches_same_money_identity(
    assembled,
):
    app, company = assembled
    _, contract, loan, interest, _, _ = six()
    assert (
        app.accounting_apply(request(company, interest, loan, contract))["completion"] == "complete"
    )
    before = result(app, company, interest)
    paid = payment(loan, 400000)
    posted = request(company, paid)
    assert app.accounting_apply(posted)["completion"] == "complete"
    assert result(app, company, interest)["target"]["values"]["interest_fen"] == "2460"
    money_before = result(app, company, paid)
    paid["data"]["amount_fen"] = "500000"
    paid["data"]["allocations"][0]["amount_fen"] = "500000"
    changed = app.accounting_apply(change(company, paid))
    assert changed["completion"] == "complete", changed
    after = result(app, company, interest)
    assert after["target"]["values"]["interest_fen"] == "2300"
    assert after["reference"]["voucher_number"] == before["reference"]["voucher_number"]
    assert (
        result(app, company, paid)["reference"]["result_id"]
        == money_before["reference"]["result_id"]
    )


def test_purchase_correction_recalculates_gain_keeps_real_receipt_and_numbers(assembled):
    app, company = assembled
    *_, purchase, redeemed = six()
    received = payment(redeemed, 103000, slot="primary", party=COUNTERPARTY, direction="inflow")
    assert (
        app.accounting_apply(request(company, purchase, redeemed, received))["completion"]
        == "complete"
    )
    before = result(app, company, redeemed)
    money_before = result(app, company, received)
    purchase["data"]["purchase_price_fen"] = "99000"
    redeemed["data"]["costs"][0]["amount_fen"] = "99000"
    changed = app.accounting_apply(change(company, purchase, redeemed))
    assert changed["completion"] == "complete", changed
    after = result(app, company, redeemed)
    assert after["target"]["values"]["gain_loss_fen"] == "4000"
    assert after["reference"]["voucher_number"] == before["reference"]["voucher_number"]
    money_after = result(app, company, received)
    assert money_after["target"] == money_before["target"]
    assert money_after["reference"]["result_id"] == money_before["reference"]["result_id"]
    assert [(x["voucher_id"], x["number"], x["lines"]) for x in money_after["vouchers"]] == [
        (x["voucher_id"], x["number"], x["lines"]) for x in money_before["vouchers"]
    ]


def test_redemption_zero_proceeds_keeps_resource_but_creates_no_cash(assembled):
    app, company = assembled
    *_, purchase, redeemed = six()
    redeemed["data"]["net_proceeds_fen"] = "0"
    actual = app.accounting_apply(request(company, purchase, redeemed))
    assert actual.get("completion") == "complete", actual
    assert result(app, company, redeemed)["target"]["values"]["gain_loss_fen"] == "-100000"
    with app.storage.read_unit(company) as read:
        assert (
            app.money.query_movements(
                {
                    "company_id": read.company_id,
                    "period_from": "2026-01",
                    "period_to": "2026-01",
                    "view": read.view,
                },
                MovementQuery(BANK),
                PageQuery(),
                read,
            ).items
            == ()
        )
        (zero,) = app.ledger.resource_definitions(
            [{"business_id": redeemed["business_id"], "slot": "primary"}], "obligation", read
        )
        assert zero.value["amount_fen"] == "0"
        assert zero.value["cashflow_weights"] == [
            {"category": "investment_recovery", "weight": "1"}
        ]


def test_same_unit_double_redemption_fails_without_partial_facts(assembled):
    app, company = assembled
    *_, purchase, first = six()
    second = deepcopy(first)
    second["business_id"] = str(uuid4())
    actual = app.accounting_apply(request(company, purchase, first, second))
    assert actual.get("completion") == "not_saved", actual
    assert (
        get(app, company, purchase["business_id"], first["business_id"], second["business_id"])[
            "businesses"
        ]
        == []
    )


@pytest.mark.parametrize(
    "fault",
    [
        "overlap",
        "before_start",
        "after_maturity",
        "foreign_fund",
        "duplicate_lot",
        "overflow",
        "wrong_lender",
        "cash_loan",
    ],
)
def test_invalid_facts_and_cross_resource_conflicts_roll_back(assembled, fault):
    app, company = assembled
    _, contract, loan, interest, purchase, redeemed = six()
    items = [contract, loan, interest, purchase, redeemed]
    if fault == "overlap":
        other = deepcopy(interest)
        other["business_id"] = str(uuid4())
        other["data"]["start_on"] = "2026-01-15"
        items.append(other)
    elif fault == "before_start":
        loan["effective_on"] = "2026-01-02"
    elif fault == "after_maturity":
        contract["data"]["matures_on"] = "2026-01-20"
    elif fault == "foreign_fund":
        redeemed["data"]["fund_id"] = OWNER
    elif fault == "duplicate_lot":
        redeemed["data"]["costs"].append(deepcopy(redeemed["data"]["costs"][0]))
    elif fault == "overflow":
        purchase["data"]["purchase_price_fen"] = str(2**63 - 1)
        purchase["data"]["acquisition_fees_fen"] = "1"
    elif fault == "wrong_lender":
        contract["data"]["lender_id"] = OWNER
    else:
        loan["data"]["funds_account_id"] = CASH
    actual = app.accounting_apply(request(company, *items))
    assert actual.get("completion") == "not_saved", actual
    assert get(app, company, *[x["business_id"] for x in items])["businesses"] == []


def test_zero_interest_still_has_classified_obligation_and_can_be_corrected(assembled):
    app, company = assembled
    _, contract, loan, interest, _, _ = six()
    loan["data"]["principal_fen"] = "1"
    actual = app.accounting_apply(request(company, contract, loan, interest))
    assert actual.get("completion") == "complete", actual
    assert result(app, company, interest)["target"]["values"]["interest_fen"] == "0"
    assert result(app, company, interest)["vouchers"] == []
    with app.storage.read_unit(company) as read:
        (zero,) = app.ledger.resource_definitions(
            [{"business_id": interest["business_id"], "slot": "interest"}], "obligation", read
        )
        assert zero.value["cashflow_weights"] == [{"category": "interest_payments", "weight": "1"}]


def test_withdrawal_leaves_tombstone_and_unconsumed_cost_disappears(assembled):
    app, company = assembled
    *_, purchase, _ = six()
    posted = request(company, purchase)
    assert app.accounting_apply(posted)["completion"] == "complete"
    withdrawn = {
        "contract": "ai-accounting-modular/1",
        "company_id": company,
        "command_id": str(uuid4()),
        "mutations": [
            {
                "op": "withdraw",
                "business_id": purchase["business_id"],
                "expected_seq": 1,
            }
        ],
    }
    actual = app.accounting_apply(withdrawn)
    assert actual.get("completion") == "complete", actual
    current = get(app, company, purchase["business_id"])["businesses"][0]
    assert current["state"]["state"] == "withdrawn" and "fact" not in current
    assert app.accounting_apply(posted)["error"] == "conflict"


def test_owner_equity_cash_and_loan_funding_do_not_create_profit(assembled):
    app, company = assembled
    owner = six()[0]
    owner["data"].update(funding_kind="capital", funds_account_id=CASH)
    actual = app.accounting_apply(request(company, owner))
    assert actual.get("completion") == "complete", actual
    with app.storage.read_unit(company) as read:
        movements = app.money.query_movements(
            {
                "company_id": read.company_id,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": read.view,
            },
            MovementQuery(CASH),
            PageQuery(),
            read,
        )
        assert movements.items[0]["movement"]["cashflow_parts"] == [
            {"category": "capital_receipts", "amount_fen": "100000"}
        ]
    assert result(app, company, owner)["vouchers"][0]["lines"][1]["account_code"] == "3001"


def test_current_fact_export_and_company_query_stay_in_owner_family(assembled):
    app, company = assembled
    facts = six()
    assert app.accounting_apply(request(company, *facts))["completion"] == "complete"
    with app.storage.read_unit(company) as read:
        financing, investment = create_financing(), create_investment()
        assert len(financing.export_current([x["business_id"] for x in facts], read)) == 4
        assert len(investment.export_current([x["business_id"] for x in facts], read)) == 2
        query = {
            "company_id": company,
            "family": "investment",
            "period_from": "2026-01",
            "period_to": "2026-01",
            "object_id": COUNTERPARTY,
            "limit": 1,
        }
        page = investment.query_current(query, read)
        assert len(page) == 1
        query["cursor"] = page[0]["business_id"]
        assert len(investment.query_current(query, read)) == 1
