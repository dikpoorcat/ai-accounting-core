"""Real investment sources prove first booking and bounded mixed-cash event reads."""

from copy import deepcopy

from _financial_snapshot_review_support import (
    BANK,
    PARTY_A,
    assemble,
    expense,
    fact,
    identity,
    opening,
)

from ai_accounting_modular.contracts.public import PageQuery, QuerySelection
from ai_accounting_modular.ledger.public import AccountBalanceKey, SettlementEventQuery

FUND = identity("read-fund")


def fund_object():
    return {
        "object_id": FUND,
        "kind": "investment_product",
        "name": "合成基金",
        "expected_seq": 0,
        "data": {"category": "money_fund"},
    }


def purchase(name, value="1"):
    return fact(
        name,
        "investment.money_fund_purchase",
        {
            "fund_id": FUND,
            "counterparty_id": PARTY_A,
            "classification": "readily_redeemable_held_not_over_one_year",
            "cost_basis": "documented_cost",
            "purchase_price_fen": value,
            "acquisition_fees_fen": "0",
            "excludes_declared_unpaid_distributions": True,
        },
    )


def test_501_fund_sources_and_one_other_allocation_keep_true_cash_and_page_only_uses(
    tmp_path, monkeypatch
):
    book = assemble(tmp_path, include_investment=True)
    book.submit(opening(cash="1000"), objects=[fund_object()])
    purchases = [purchase("read-purchase-" + str(index)) for index in range(501)]
    book.submit(*purchases[:250])
    book.submit(*purchases[250:])
    fee = expense("read-mixed-fee", "7")
    paid = fact(
        "read-mixed-payment",
        "money.movement",
        {
            "type": "settlement",
            "funds_account_id": BANK,
            "direction": "outflow",
            "amount_fen": "508",
            "payment_method": "individual",
            "allocations": [
                {
                    "source": {"business_id": source["business_id"], "slot": "primary"},
                    "amount_fen": value,
                }
                for source, value in [*((source, "1") for source in purchases), (fee, "7")]
            ],
        },
        on="2026-01-21",
    )
    book.submit(fee, paid)
    query = SettlementEventQuery(
        "2026-01", "2026-01", source_business_kinds=("investment.money_fund_purchase",)
    )
    with book.app.storage.read_unit(book.company) as read:
        repo = read.repository("ledger")
        sizes = []
        original = type(repo).read_event_payloads

        def counted(owner, headers):
            sizes.append(len(headers))
            return original(owner, headers)

        monkeypatch.setattr(type(repo), "read_event_payloads", counted)
        first = book.app.ledger.query_settlement_events(query, PageQuery(500), read)
        second = book.app.ledger.query_settlement_events(
            query, PageQuery(500, first.next_cursor, first.version), read
        )
        assert (first.total_count, first.filtered_count, len(first.items), len(second.items)) == (
            502,
            501,
            500,
            1,
        )
        assert sizes == [500, 1]
        all_items = (*first.items, *second.items)
        assert sum(int(item.use.value["amount_fen"]) for item in all_items) == 501
        assert {item.movement.value["amount_fen"] for item in all_items} == {"508"}
        assert len({item.movement.value["movement_id"] for item in all_items}) == 1


def test_first_formal_fund_month_survives_closed_target_correction(tmp_path):
    book = assemble(tmp_path, include_investment=True)
    bought = purchase("read-first-purchase", "100")
    book.submit(opening(), bought, objects=[fund_object()])
    book.freeze_formal_heads_for_test("2026-01")
    changed = deepcopy(bought)
    changed["data"]["purchase_price_fen"] = "120"
    book.replace(changed, correction_period="2026-03")
    key = AccountBalanceKey("1101", FUND)
    with book.app.storage.read_unit(book.company) as read:
        current = book.app.ledger.account_balances([key], "2026-03", read)[0]
        original = book.app.ledger.account_balances(
            [key], "2026-01", read, selection=QuerySelection("as_posted", "2026-01")
        )[0]
    assert current.ending["amount_fen"] == "120"
    assert original.ending["amount_fen"] == "100"
    assert current.first_recorded_period == original.first_recorded_period == "2026-01"
