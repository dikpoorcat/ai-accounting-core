"""Owner investment reads retain sourced amounts without tax or dormant product work."""

from copy import deepcopy
from uuid import uuid4

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.financing.public import create_investment
from tests.modular.application._working_case import case as case
from tests.modular.domains.taxes._fixtures import evidence
from tests.modular.domains.taxes.test_investment_tax_application import (
    advance,
    inputs,
    qualification,
)

from ._investment_support import acquire, dispose, source, terms
from ._support import COUNTERPARTY, PRODUCT, assemble, fact, payment, request, six
from .test_investment_lifecycle import apply_facts


def scope(company, month="2026-01", view="current"):
    return {"company_id": company, "period_from": month, "period_to": month, "view": view}


def book(case):
    case.seed(expense=False)
    return case.app, case.company


def positions(app, company, month="2026-01", view="current"):
    owner = create_investment(ledger=app.ledger, identity=app.identity)
    with app.storage.read_unit(company) as read:
        return owner.investment_positions(scope(company, month, view), read)


@pytest.mark.parametrize("view", ["current", "as_posted"])
def test_positions_share_each_holding_cash_and_income_with_complete_activity(case, view):
    app, company = book(case)
    first, second = terms(), terms(measurement="other_financial_cost")
    first_buy, second_buy = acquire(first, "100"), acquire(second, "200")
    funding, *_, legacy_buy, legacy_sale = six()
    legacy_buy["data"]["purchase_price_fen"] = "300"
    legacy_sale["data"]["costs"][0]["amount_fen"] = "300"
    legacy_sale["data"]["net_proceeds_fen"] = "330"
    sold = dispose(first, "1", "40")
    income = fact(
        "investment.income",
        {
            "terms_id": second["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "fund_distribution",
            "amount_fen": "7",
        },
    )
    cash = [
        payment(item, value, slot="primary", party=COUNTERPARTY, day="2026-01-03")
        for item, value in ((first_buy, 100), (second_buy, 200), (legacy_buy, 300))
    ] + [
        payment(item, value, slot=slot, party=COUNTERPARTY, direction="inflow", day="2026-01-05")
        for item, value, slot in (
            (sold, 40, "primary"),
            (income, 7, "income"),
            (legacy_sale, 330, "primary"),
        )
    ]
    apply_facts(
        app,
        company,
        funding,
        first,
        second,
        first_buy,
        second_buy,
        legacy_buy,
        legacy_sale,
        sold,
        income,
        *cash,
        initial=True,
    )
    owner = create_investment(ledger=app.ledger, identity=app.identity)
    with app.storage.read_unit(company) as read:
        actual = owner.investment_positions(scope(company, view=view), read)
        complete = owner.investment_activity(scope(company, view=view), read)
    assert actual.products == complete.products
    assert actual.closing_cost == complete.closing_cost == {"state": "known", "amount_fen": "267"}
    assert {
        item.terms_id: (item.actual_payments_fen, item.actual_receipts_fen)
        for item in actual.products
    } == {
        first["business_id"]: (100, 40),
        second["business_id"]: (200, 7),
        None: (300, 330),
    }
    assert {first_buy["business_id"], second_buy["business_id"], legacy_buy["business_id"]} <= set(
        actual.inputs.business_ids
    )
    assert "5111" not in actual.inputs.account_codes
    assert not any(kind.startswith("taxes.") for kind in actual.inputs.posted_result_kinds)


def test_tax_change_keeps_position_version_and_does_not_load_tax_contents(case, monkeypatch):
    app, company = book(case)
    values = inputs()
    payload = request(company, *values[:-1], qualification(values[0]))
    payload["sources"].append(evidence())
    actual = app.accounting_apply(payload)
    assert actual.get("completion") == "complete", actual
    before = positions(app, company, "2026-07")
    apply_facts(app, company, values[-1])
    owner = create_investment(ledger=app.ledger, identity=app.identity)
    requested, kinds = [], []
    get_contents, contributions = app.ledger.get_adopted_contents, app.ledger.profit_contributions

    def contents(refs, read):
        requested.extend(ref["business_id"] for ref in refs)
        return get_contents(refs, read)

    def profit(scope_, page, read, *, business_kinds=()):
        kinds.append(business_kinds)
        return contributions(scope_, page, read, business_kinds=business_kinds)

    monkeypatch.setattr(app.ledger, "get_adopted_contents", contents)
    monkeypatch.setattr(app.ledger, "profit_contributions", profit)
    monkeypatch.setattr(owner, "investment_activity", lambda *_: pytest.fail("complete activity"))
    monkeypatch.setattr(owner, "money_fund_activity", lambda *_: pytest.fail("complete legacy"))
    with app.storage.read_unit(company) as read:
        after = owner.investment_positions(scope(company, "2026-07"), read)
    assert before == after
    assert values[-1]["business_id"] not in requested
    assert len(kinds) == 1 and kinds[0]
    assert not any(kind.startswith("taxes.") for kind in kinds[0])
    kinds.clear()
    with app.storage.read_unit(company) as read:
        complete = create_investment(ledger=app.ledger, identity=app.identity).investment_activity(
            scope(company, "2026-07"), read
        )
    assert kinds == [()]
    assert complete.recognized_income_fen == 102000
    assert complete.vat_adjustment_fen == -2000
    assert complete.investment_income_fen == 100000


def test_dormant_legacy_is_excluded_before_names_balances_and_contents(case, monkeypatch):
    app, company = book(case)
    funding, *_, old_buy, old_sale = six()
    active_id, tail_id = str(uuid4()), str(uuid4())
    active, tail_buy = deepcopy(old_buy), deepcopy(old_buy)
    active["business_id"], tail_buy["business_id"] = str(uuid4()), str(uuid4())
    active["data"].update(fund_id=active_id, purchase_price_fen="70")
    tail_buy["data"].update(fund_id=tail_id, purchase_price_fen="20")
    tail_sale = deepcopy(old_sale)
    tail_sale["business_id"] = str(uuid4())
    tail_sale["data"].update(fund_id=tail_id, net_proceeds_fen="22")
    tail_sale["data"]["costs"] = [{"source": source(tail_buy), "amount_fen": "20"}]
    payload = request(
        company,
        funding,
        old_buy,
        old_sale,
        active,
        tail_buy,
        tail_sale,
        payment(old_buy, 100000, slot="primary", party=COUNTERPARTY),
        payment(old_sale, 103000, slot="primary", party=COUNTERPARTY, direction="inflow"),
        payment(tail_buy, 20, slot="primary", party=COUNTERPARTY),
    )
    payload["objects"].extend(
        {
            "object_id": identifier,
            "kind": "investment_product",
            "name": "合成存续基金",
            "expected_seq": 0,
            "data": {"category": "money_fund"},
        }
        for identifier in (active_id, tail_id)
    )
    actual = app.accounting_apply(payload)
    assert actual.get("completion") == "complete", actual
    named, balanced, loaded = [], [], []
    get_batch, balances, contents = (
        app.identity.get_batch,
        app.ledger.account_balances,
        app.ledger.get_adopted_contents,
    )

    def names(ids, read):
        named.extend(ids)
        return get_batch(ids, read)

    def account(keys, *args, **kwargs):
        balanced.extend(key.object_id for key in keys)
        return balances(keys, *args, **kwargs)

    def bodies(refs, read):
        loaded.extend(ref["business_id"] for ref in refs)
        return contents(refs, read)

    monkeypatch.setattr(app.identity, "get_batch", names)
    monkeypatch.setattr(app.ledger, "account_balances", account)
    monkeypatch.setattr(app.ledger, "get_adopted_contents", bodies)
    actual = positions(app, company, "2026-02")
    assert {item.fund_id for item in actual.products} == {active_id, tail_id}
    assert actual.closing_cost == {"state": "known", "amount_fen": "70"}
    assert PRODUCT not in named and PRODUCT not in balanced
    assert old_buy["business_id"] not in loaded and old_sale["business_id"] not in loaded
    (tail,) = [item for item in actual.products if item.fund_id == tail_id]
    assert tail.pending_receipts_fen == 22 and tail.closing_cost["amount_fen"] == "0"


def test_zero_cost_shares_and_zero_stock_tail_keep_their_formal_identity(case):
    app, company = book(case)
    live, exited = terms(), terms(measurement="other_financial_cost")
    live_buy, exited_buy = acquire(live, "0"), acquire(exited, "20")
    sold = dispose(exited, "3", "22")
    apply_facts(
        app, company, live, exited, live_buy, exited_buy, sold, advance("2026-02"), initial=True
    )
    actual = positions(app, company, "2026-02")
    by_terms = {item.terms_id: item for item in actual.products}
    assert set(by_terms) == {live["business_id"], exited["business_id"]}
    assert by_terms[live["business_id"]].quantity == "3"
    assert by_terms[live["business_id"]].closing_cost == {"state": "known", "amount_fen": "0"}
    assert by_terms[exited["business_id"]].quantity == "0"
    assert by_terms[exited["business_id"]].pending_receipts_fen == 22
    assert by_terms[exited["business_id"]].activity_only is False


def test_missing_inventory_snapshot_cannot_become_an_income_only_position(case, monkeypatch):
    app, company = book(case)
    agreement = terms()
    apply_facts(app, company, agreement, acquire(agreement), initial=True)
    from ai_accounting_modular.contracts.public import ReadPage

    def no_positions(*args, **kwargs):
        return ReadPage("0" * 64, 0, 0, (), None)

    monkeypatch.setattr(app.ledger, "query_investment_holdings", no_positions)
    monkeypatch.setattr(app.ledger, "query_adopted_targets", no_positions)
    with pytest.raises(ContractError, match="库存活动缺少") as error:
        positions(app, company)
    assert error.value.code == "storage_corrupt"


def test_cleared_legacy_does_not_turn_unknown_company_opening_into_zero(tmp_path):
    app, company = assemble(tmp_path)
    funding, *_, bought, sold = six()
    response = app.accounting_apply(
        request(
            company,
            funding,
            bought,
            sold,
            payment(bought, 100000, slot="primary", party=COUNTERPARTY),
            payment(sold, 103000, slot="primary", party=COUNTERPARTY, direction="inflow"),
        )
    )
    assert response.get("completion") == "complete", response
    actual = positions(app, company, "2026-02")
    assert actual.products == ()
    assert actual.closing_cost["state"] == "unestablished"
    assert "amount_fen" not in actual.closing_cost
    assert actual.closing_cost["issue_indexes"]
    assert "/accounts/1101/all/ending" in {
        actual.issues[index]["field"] for index in actual.closing_cost["issue_indexes"]
    }
