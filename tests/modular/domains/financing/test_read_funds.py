"""Money-fund display is a projection of formal values and allocated actual cash."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError, PageQuery
from ai_accounting_modular.domains.financing.public import create_investment
from tests.modular.application._working_case import case as case

from ._support import COUNTERPARTY, PRODUCT, objects, payment, six


def scope(case, month="2026-01", view="current"):
    return {"company_id": case.company, "period_from": month, "period_to": month, "view": view}


def submit(case, *facts):
    facts = deepcopy(facts)
    for fact in facts:
        fact["evidence_ids"] = [case.source_id]
    response = case.app.accounting_apply(case.request(*facts))
    assert response["completion"] == "complete", response
    return facts


def initialize(case):
    case.seed(expense=False)
    response = case.app.accounting_apply(case.request(objects=objects()))
    assert response["completion"] == "complete", response
    return create_investment(ledger=case.kernel.ledger, identity=case.kernel.identity)


def test_confirmations_cost_and_later_cash_are_independent(case):
    owner = initialize(case)
    *_, purchase, redeemed = six()
    purchase["data"]["purchase_price_fen"] = "500"
    redeemed["data"]["costs"][0]["amount_fen"] = "200"
    redeemed["data"]["net_proceeds_fen"] = "210"
    submit(case, purchase, redeemed)
    with case.kernel.storage.read_unit(case.company) as read:
        actual = owner.money_fund_activity(scope(case), read)
        assert actual.opening_cost == {"state": "known", "amount_fen": "0"}
        assert actual.closing_cost == {"state": "known", "amount_fen": "300"}
        assert (
            actual.subscription_cost_fen,
            actual.redemption_cost_fen,
            actual.investment_income_fen,
        ) == (500, 200, 10)
        assert (actual.actual_payments_fen, actual.actual_receipts_fen, actual.event_count) == (
            0,
            0,
            2,
        )
        assert actual.products[0].fund_id == PRODUCT
        assert actual.events == owner.query_money_fund_events(scope(case), PageQuery(), read).items
        assert all(item.actual_on is None for item in actual.events)
    paid = payment(purchase, 500, party=COUNTERPARTY, slot="primary", day="2026-02-02")
    received = payment(
        redeemed, 210, party=COUNTERPARTY, slot="primary", direction="inflow", day="2026-02-04"
    )
    submit(case, paid, received)
    with case.kernel.storage.read_unit(case.company) as read:
        january = owner.money_fund_activity(scope(case), read)
        february = owner.money_fund_activity(scope(case, "2026-02"), read)
        assert (january.actual_payments_fen, january.actual_receipts_fen) == (0, 0)
        assert (
            february.subscription_cost_fen,
            february.redemption_cost_fen,
            february.investment_income_fen,
        ) == (0, 0, 0)
        assert (february.actual_payments_fen, february.actual_receipts_fen) == (500, 210)
        assert (
            february.opening_cost
            == february.closing_cost
            == {"state": "known", "amount_fen": "300"}
        )
        found = owner.query_money_fund_events(scope(case, "2026-02"), PageQuery(), read)
        assert {item.kind for item in found.items} == {"purchase_payment", "redemption_receipt"}
        assert all(item.business_period == "2026-01" for item in found.items)


def test_mixed_payment_is_allocated(case):
    owner = initialize(case)
    *_, purchase, _ = six()
    purchase["data"]["purchase_price_fen"] = "300"
    other = case.expense("700")
    other["data"]["creditor_id"] = COUNTERPARTY
    paid = payment(purchase, 1000, party=COUNTERPARTY, slot="primary")
    paid["data"]["allocations"][0]["amount_fen"] = "300"
    paid["data"]["allocations"].append(
        {"source": {"business_id": other["business_id"], "slot": "primary"}, "amount_fen": "700"}
    )
    submit(case, purchase, other, paid)
    with case.kernel.storage.read_unit(case.company) as read:
        actual = owner.money_fund_activity(scope(case), read)
        assert actual.actual_payments_fen == 300
        events = owner.query_money_fund_events(scope(case), PageQuery(), read)
        assert [
            item.settlement_fen for item in events.items if item.kind == "purchase_payment"
        ] == [300]
        money = case.kernel.money.account_activity(scope(case), read)
        assert money.outflow_fen == 1000
        assert len(money.movements) == 1
        assert sorted(part.amount_fen for part in money.movements[0]["allocations"]) == [300, 700]


@pytest.mark.parametrize("view", ["as_posted", "current"])
def test_complete_product_costs_are_captured_once(case, monkeypatch, view):
    owner = initialize(case)
    *_, purchase, _ = six()
    submit(case, purchase)
    ledger = case.kernel.ledger
    capture = ledger.capture_cost_positions
    calls = []

    def captured(query, read, *, selection):
        result = capture(query, read, selection=selection)
        calls.append((selection.view, selection.as_of, result.filtered_count))
        return result

    monkeypatch.setattr(ledger, "capture_cost_positions", captured)
    monkeypatch.setattr(
        ledger, "query_cost_positions", lambda *_args, **_kwargs: pytest.fail("paged positions")
    )
    with case.kernel.storage.read_unit(case.company) as read:
        result = owner.money_fund_activity(scope(case, view=view), read)
    assert result.closing_cost == {
        "state": "known",
        "amount_fen": purchase["data"]["purchase_price_fen"],
    }
    assert len(result.products) == 1 and result.products[0].fund_id == PRODUCT
    assert calls == [(view, "2026-01" if view == "as_posted" else None, 1)]


def test_501_sources_are_fully_counted_and_pages_do_not_repeat(case):
    owner = initialize(case)
    *_, prototype, _ = six()
    prototype["data"]["purchase_price_fen"] = "1"
    purchases = []
    for _ in range(501):
        item = deepcopy(prototype)
        item["business_id"] = str(uuid4())
        purchases.append(item)
    submit(case, *purchases[:500])
    submit(case, purchases[-1])
    paid = payment(purchases[0], 501, party=COUNTERPARTY, slot="primary")
    paid["data"]["allocations"] = [
        {"source": {"business_id": item["business_id"], "slot": "primary"}, "amount_fen": "1"}
        for item in purchases
    ]
    submit(case, paid)
    with case.kernel.storage.read_unit(case.company) as read:
        summary = owner.money_fund_activity(scope(case), read)
        assert (
            summary.subscription_cost_fen,
            summary.actual_payments_fen,
            summary.event_count,
        ) == (501, 501, 1002)
        assert len(summary.events) == 1002
        found, cursor = [], None
        while True:
            page = owner.query_money_fund_events(scope(case), PageQuery(500, cursor), read)
            assert page.total_count == page.filtered_count == 1002
            found.extend(item.event_id for item in page.items)
            if page.next_cursor is None:
                break
            cursor = page.next_cursor
        assert len(found) == len(set(found)) == 1002


def test_event_version_is_scoped_and_stale_pages_are_rejected(case):
    owner = initialize(case)
    *_, purchase, redeemed = six()
    submit(case, purchase, redeemed)
    with case.kernel.storage.read_unit(case.company) as read:
        first = owner.query_money_fund_events(scope(case), PageQuery(1), read)
        summary = owner.money_fund_activity(scope(case), read)
    submit(case, case.expense("7"))
    with case.kernel.storage.read_unit(case.company) as read:
        assert owner.money_fund_activity(scope(case), read) == summary
    unrelated = case.expense("9", "2026-02")
    submit(case, unrelated)
    with case.kernel.storage.read_unit(case.company) as read:
        assert owner.query_money_fund_events(scope(case), PageQuery(1), read) == first
    extra = deepcopy(purchase)
    extra["business_id"] = str(uuid4())
    submit(case, extra)
    with (
        case.kernel.storage.read_unit(case.company) as read,
        pytest.raises(ContractError) as caught,
    ):
        owner.query_money_fund_events(scope(case), PageQuery(1, first.next_cursor), read)
    assert caught.value.code == "snapshot_changed"


def test_closed_correction_keeps_old_and_new_fund_dates_and_balances(case):
    owner = initialize(case)
    *_, purchase, _ = six()
    purchase["data"]["purchase_price_fen"] = "100"
    purchase["effective_on"] = "2026-01-04"
    (purchase,) = submit(case, purchase)
    checked = case.check()
    assert checked["ready"], checked
    assert case.close(checked)["state"] == "closed"
    other = str(uuid4())
    purchase["data"].update(fund_id=other, purchase_price_fen="90")
    purchase["effective_on"] = "2026-01-09"
    actual = case.app.accounting_apply(
        case.request(
            correction_period="2026-02",
            objects=[
                {
                    "object_id": other,
                    "kind": "investment_product",
                    "name": "另一合成基金",
                    "expected_seq": 0,
                    "data": {"category": "money_fund"},
                }
            ],
            mutations=[{"op": "replace", "expected_seq": 1, "fact": purchase}],
        )
    )
    assert actual["completion"] == "complete", actual
    with case.kernel.storage.read_unit(case.company) as read:
        old = owner.query_money_fund_events(scope(case, view="as_posted"), PageQuery(), read)
        assert [(item.fund_id, item.cost_fen, item.actual_on) for item in old.items] == [
            (PRODUCT, 100, "2026-01-04")
        ]
        corrected = owner.query_money_fund_events(
            scope(case, "2026-02", "as_posted"), PageQuery(), read
        )
        assert {(item.fund_id, item.cost_fen, item.actual_on) for item in corrected.items} == {
            (PRODUCT, -100, "2026-01-04"),
            (other, 90, "2026-01-09"),
        }
        summary = owner.money_fund_activity(scope(case, "2026-02", "as_posted"), read)
        assert (
            summary.opening_cost["amount_fen"],
            summary.closing_cost["amount_fen"],
            summary.subscription_cost_fen,
        ) == ("100", "90", -10)
        assert summary.actual_payments_fen == summary.actual_receipts_fen == 0
        current = owner.query_money_fund_events(scope(case), PageQuery(), read)
        assert [(item.fund_id, item.cost_fen) for item in current.items] == [(other, 90)]


def test_missing_opening_remains_unknown_instead_of_purchase_total_as_balance(tmp_path):
    from ._support import assemble, request

    app, company = assemble(tmp_path)
    *_, purchase, _ = six()
    other = deepcopy(purchase)
    other["business_id"], other["data"]["fund_id"] = str(uuid4()), str(uuid4())
    call = request(company, purchase, other)
    call["objects"].append(
        {
            "object_id": other["data"]["fund_id"],
            "kind": "investment_product",
            "name": "合成另一基金",
            "expected_seq": 0,
            "data": {"category": "money_fund"},
        }
    )
    assert app.accounting_apply(call)["completion"] == "complete"
    owner = create_investment(ledger=app.ledger, identity=app.identity)
    with app.storage.read_unit(company) as read:
        actual = owner.money_fund_activity(
            {
                "company_id": company,
                "period_from": "2026-01",
                "period_to": "2026-01",
                "view": "current",
            },
            read,
        )
        assert actual.subscription_cost_fen == 200000
        assert actual.opening_cost["state"] == actual.closing_cost["state"] == "unestablished"
        cells = [
            (actual.opening_cost, "/accounts/1101/all/opening"),
            (actual.closing_cost, "/accounts/1101/all/ending"),
        ]
        for product in actual.products:
            cells.extend(
                [
                    (product.opening_cost, f"/accounts/1101/{product.fund_id}/opening"),
                    (product.closing_cost, f"/accounts/1101/{product.fund_id}/ending"),
                ]
            )
        for cell, field in cells:
            assert cell["state"] == "unestablished"
            assert field in {actual.issues[index]["field"] for index in cell["issue_indexes"]}
