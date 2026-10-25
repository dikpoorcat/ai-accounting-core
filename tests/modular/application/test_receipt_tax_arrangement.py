"""Retained receipt-month arrangements follow real allocations and keep their sources."""

from copy import deepcopy

import pytest

from ai_accounting_modular.ledger.public import LedgerQuery
from tests.modular.domains.operations.test_tax_lifecycle import (
    command,
    fact,
    get,
    policy,
    reach,
    result,
    sale,
)
from tests.modular.domains.operations.test_tax_lifecycle import system as system

from ._tax_default_fixtures import receipt, sales, save


def receipt_arrangement(ids, earned, *, sourced=False):
    data = {"type": "owner_receipt_default", "target_business_id": earned["business_id"]}
    if sourced:
        data["source"] = {"source_id": ids["source"], "location": "老板长期收款月口径"}
    point = fact(ids, "taxes.tax_point", data)
    earned["data"]["tax_point"] = {"type": "derived", "tax_point_id": point["business_id"]}
    return point


def replace(app, company, current, **kwargs):
    state = get(app, company, current)["state"]
    return save(
        app,
        company,
        mutations=[{"op": "replace", "expected_seq": state["seq"], "fact": current}],
        **kwargs,
    )


@pytest.mark.parametrize("sourced", [False, True])
def test_unreceived_revenue_has_no_fake_tax_month_or_contract_event(system, sourced):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule)
    point = receipt_arrangement(ids, earned, sourced=sourced)
    original = deepcopy(point)
    save(app, company, point, earned, rule)

    adopted = result(app, company, point)
    resolved = adopted["target"]["values"]
    assert resolved["basis"] == "owner_receipt_default"
    assert resolved["segments"] == [
        {"state": "not_due", "ranges": [{"from_fen": "0", "to_fen": "10100"}]}
    ]
    assert point == original
    assert get(app, company, point)["fact"] == original
    assert "events" not in adopted["basis"]["input"]
    values = result(app, company, earned)["target"]["values"]
    assert values["revenue_fen"] == "10000"
    assert values["deferred_vat_fen"] == "100"
    assert sales(app, company) == []


def test_new_partial_receipts_automatically_release_only_their_months_and_amounts(system):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule)
    point = receipt_arrangement(ids, earned)
    save(app, company, earned, point, rule)
    first = receipt(ids, earned, "5050", "2026-02-08")
    second = receipt(ids, earned, "5050", "2026-03-08")
    save(app, company, first)
    assert sales(app, company) == [(earned["business_id"], "2026-02", "50")]
    save(app, company, second)
    assert sales(app, company) == [
        (earned["business_id"], "2026-02", "50"),
        (earned["business_id"], "2026-03", "50"),
    ]
    segments = result(app, company, point)["target"]["values"]["segments"]
    assert [(row["tax_period"], row["tax_on"], row["receipt_ids"]) for row in segments] == [
        ("2026-02", "2026-02-08", [first["business_id"]]),
        ("2026-03", "2026-03-08", [second["business_id"]]),
    ]
    assert all(
        row["trigger"] == "owner_receipt_default" and row["event_ids"] == [] for row in segments
    )
    with app.storage.read_unit(company) as read:
        assert (
            app.ledger.pre_tax("2026-01", "2026-03", read).fields["operating_revenue"]["amount_fen"]
            == "10000"
        )
        position = app.ledger.query_obligations(LedgerQuery((earned["business_id"],)), read)
    assert position.items[0]["balance"]["remaining_fen"] == "0"


def test_actual_receipt_date_correction_and_withdrawal_update_tax_without_editing_policy(system):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule)
    point = receipt_arrangement(ids, earned)
    paid = receipt(ids, earned, day="2026-02-08")
    save(app, company, paid, point, earned, rule)
    assert sales(app, company) == [(earned["business_id"], "2026-02", "100")]

    paid.update(period="2026-03", effective_on="2026-03-09")
    replace(app, company, paid)
    assert sales(app, company) == [(earned["business_id"], "2026-03", "100")]
    state = get(app, company, paid)["state"]
    save(
        app,
        company,
        mutations=[
            {"op": "withdraw", "business_id": paid["business_id"], "expected_seq": state["seq"]}
        ],
    )
    assert sales(app, company) == []
    assert get(app, company, point)["fact"] == point
    assert result(app, company, point)["target"]["values"]["segments"][0]["state"] == "not_due"


def test_unrelated_business_does_not_refresh_receipt_tax_results(system):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule)
    point = receipt_arrangement(ids, earned)
    save(app, company, earned, point, rule, receipt(ids, earned))
    before = [get(app, company, item) for item in (earned, point)]
    save(app, company, reach(ids, "2026-03"))
    assert [get(app, company, item) for item in (earned, point)] == before


def test_cash_allocated_to_another_business_cannot_supply_the_arrangements_tax_month(system):
    app, company, ids = system
    rule = policy(ids)
    earned, another = sale(ids, rule), sale(ids, rule, "2026-03")
    point = receipt_arrangement(ids, earned)
    save(app, company, earned, point, another, rule)
    before = get(app, company, point)
    save(app, company, receipt(ids, another))
    assert get(app, company, point) == before
    assert sales(app, company) == [(another["business_id"], "2026-03", "100")]
    wrong_party = receipt(ids, earned)
    wrong_party["data"]["counterparty_id"] = ids["beneficiary"]
    with app.storage.read_unit(company) as read:
        head = read.head
    rejected = app.accounting_apply(command(company, wrong_party))
    assert rejected["completion"] == "not_saved", rejected
    with app.storage.read_unit(company) as read:
        assert read.head == head
    assert get(app, company, point) == before


@pytest.mark.parametrize("raw_status", [None, "not_due"])
def test_customer_advance_inherits_partial_receipt_tax_through_later_fulfillment(
    system, raw_status
):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, advance=True)
    advance["data"].pop("tax_status")
    if raw_status is not None:
        advance["data"]["tax_status"] = raw_status
    point = receipt_arrangement(ids, advance)
    save(app, company, advance, point, rule)
    assert result(app, company, advance)["basis"]["input"]["tax_status"] == "not_due"
    save(app, company, receipt(ids, advance, "5050", "2026-01-08"))
    assert sales(app, company) == [(advance["business_id"], "2026-01", "50")]
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": advance["business_id"], "gross_fen": "5050"},
        period="2026-02",
    )
    save(app, company, fulfilled)
    assert result(app, company, fulfilled)["target"]["values"]["revenue_fen"] == "5000"
    assert sales(app, company) == [(advance["business_id"], "2026-01", "50")]
    save(app, company, receipt(ids, advance, "5050", "2026-03-08"))
    assert sales(app, company) == [
        (advance["business_id"], "2026-01", "50"),
        (advance["business_id"], "2026-03", "50"),
    ]
    assert get(app, company, advance)["fact"] == advance
    with app.storage.read_unit(company) as read:
        assert (
            app.ledger.pre_tax("2026-01", "2026-03", read).fields["operating_revenue"]["amount_fen"]
            == "5000"
        )


def test_later_receipt_preserves_closed_history_and_does_not_recognize_revenue_again(system):
    app, company, ids = system
    rule = policy(ids)
    earned = sale(ids, rule)
    point = receipt_arrangement(ids, earned)
    opening = fact(ids, "opening.package", {"basis": "newly_established", "members": []})
    statement = fact(
        ids,
        "money.bank_statement",
        {"funds_account_id": ids["bank"], "opening_fen": "0", "closing_fen": "0", "rows": []},
    )
    reconciliation = fact(
        ids, "money.reconciliation", {"statement_id": statement["business_id"], "matches": []}
    )
    save(app, company, earned, point, rule, opening, statement, reconciliation)
    checked = app.accounting_period({"company_id": company, "action": "check", "period": "2026-01"})
    assert checked["ready"], checked["blocking_issues"]
    closed = app.accounting_period(
        {
            "company_id": company,
            "action": "close",
            "period": "2026-01",
            "expected_head": checked["head"],
        }
    )
    assert closed["state"] == "closed", closed
    with app.storage.read_unit(company, view="as_posted", as_of="2026-01") as read:
        before = deepcopy(
            app.ledger.get_results([earned["business_id"], point["business_id"]], read)
        )
    save(app, company, receipt(ids, earned), correction_period="2026-03")
    with app.storage.read_unit(company, view="as_posted", as_of="2026-01") as read:
        assert app.ledger.get_results([earned["business_id"], point["business_id"]], read) == before
    assert sales(app, company) == [(earned["business_id"], "2026-03", "100")]
    with app.storage.read_unit(company) as read:
        assert (
            app.ledger.pre_tax("2026-01", "2026-03", read).fields["operating_revenue"]["amount_fen"]
            == "10000"
        )
