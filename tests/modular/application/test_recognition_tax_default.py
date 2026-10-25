"""Owner's income-month VAT convention is independent of cash collection."""

from copy import deepcopy

import pytest

from ai_accounting_modular.ledger.public import LedgerQuery
from tests.modular.domains.operations.test_tax_lifecycle import (
    fact,
    get,
    policy,
    reach,
    result,
    sale,
)
from tests.modular.domains.operations.test_tax_lifecycle import system as system

from ._tax_default_fixtures import receipt, sales, save


def earned_business(ids, rule, *, advance=False):
    earned = sale(ids, rule, advance=advance)
    earned["data"]["tax_point"] = {"type": "owner_recognition_default"}
    earned["data"].pop("tax_status", None)
    return earned


def revenue(app, company, start="2026-01", end="2026-12"):
    with app.storage.read_unit(company) as read:
        return app.ledger.pre_tax(start, end, read).fields["operating_revenue"]["amount_fen"]


def resolution(main, business_id):
    return next(
        item["resolution"]
        for item in main["basis"]["adopted_quantities"]
        if item["type"] == "tax_point" and item["resolution"]["target_business_id"] == business_id
    )


def test_unreceived_service_recognizes_tax_in_its_income_month_without_fake_dates(system):
    app, company, ids = system
    rule = policy(ids)
    earned = earned_business(ids, rule)
    save(app, company, earned, rule)
    main = result(app, company, earned)
    assert main["target"]["values"] == {
        "gross_fen": "10100",
        "revenue_fen": "10000",
        "vat_fen": "100",
        "current_tax_net_fen": "10000",
        "current_tax_vat_fen": "100",
        "deferred_vat_fen": "0",
    }
    assert main["basis"]["input"]["tax_point"] == {"type": "owner_recognition_default"}
    resolved = resolution(main, earned["business_id"])
    assert resolved["basis"] == "owner_recognition_default"
    assert resolved["segments"] == [
        {
            "state": "due",
            "ranges": [{"from_fen": "0", "to_fen": "10100"}],
            "tax_period": "2026-01",
            "trigger": "owner_recognition_default",
            "event_ids": [],
        }
    ]
    assert sales(app, company) == [(earned["business_id"], "2026-01", "100")]
    assert get(app, company, earned)["fact"] == earned
    with app.storage.read_unit(company) as read:
        balance = app.ledger.query_obligations(LedgerQuery((earned["business_id"],)), read)
    assert balance.items[0]["balance"]["remaining_fen"] == "10100"


def test_late_cash_date_changes_and_withdrawal_never_move_the_income_month_tax(system):
    app, company, ids = system
    rule = policy(ids)
    earned = earned_business(ids, rule)
    save(app, company, earned, rule)
    baseline = deepcopy(result(app, company, earned))
    paid = receipt(ids, earned)
    save(app, company, paid)
    assert result(app, company, earned) == baseline
    paid.update(period="2026-04", effective_on="2026-04-09")
    save(
        app,
        company,
        mutations=[
            {"op": "replace", "expected_seq": get(app, company, paid)["state"]["seq"], "fact": paid}
        ],
    )
    assert result(app, company, earned) == baseline
    save(
        app,
        company,
        mutations=[
            {
                "op": "withdraw",
                "expected_seq": get(app, company, paid)["state"]["seq"],
                "business_id": paid["business_id"],
            }
        ],
    )
    assert result(app, company, earned) == baseline
    assert sales(app, company) == [(earned["business_id"], "2026-01", "100")]
    assert revenue(app, company) == "10000"


def test_collected_advance_waits_for_each_partial_income_month_not_cash_month(system):
    app, company, ids = system
    rule = policy(ids)
    advance = earned_business(ids, rule, advance=True)
    save(app, company, advance, rule, receipt(ids, advance, day="2026-01-08"))
    main = result(app, company, advance)
    assert main["basis"]["input"]["tax_status"] == "not_due"
    assert resolution(main, advance["business_id"])["segments"] == [
        {"state": "not_due", "ranges": [{"from_fen": "0", "to_fen": "10100"}]}
    ]
    assert sales(app, company) == []
    assert revenue(app, company) == "0"
    fulfilled = []
    for month in ("2026-02", "2026-03"):
        current = fact(
            ids,
            "operations.advance_fulfillment",
            {
                "advance_id": advance["business_id"],
                "gross_fen": "5050",
                "tax_point": {"type": "owner_recognition_default"},
            },
            period=month,
        )
        save(app, company, current)
        fulfilled.append(current)
        assert result(app, company, current)["target"]["values"]["revenue_fen"] == "5000"
    assert sales(app, company) == sorted(
        (current["business_id"], current["period"], "50") for current in fulfilled
    )
    assert revenue(app, company) == "10000"


def test_fulfillment_keeps_receipt_tax_source_and_defaults_only_untaxed_scope(system):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, advance=True)
    advance["data"].pop("tax_status")
    previous_point = fact(
        ids,
        "taxes.tax_point",
        {"type": "owner_receipt_default", "target_business_id": advance["business_id"]},
    )
    advance["data"]["tax_point"] = {
        "type": "derived",
        "tax_point_id": previous_point["business_id"],
    }
    paid = receipt(ids, advance, "5050", "2026-01-08")
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {
            "advance_id": advance["business_id"],
            "gross_fen": "10100",
            "tax_point": {"type": "owner_recognition_default"},
        },
        period="2026-02",
    )
    save(app, company, advance, previous_point, paid, fulfilled, rule)
    assert sales(app, company) == sorted(
        [(advance["business_id"], "2026-01", "50"), (fulfilled["business_id"], "2026-02", "50")]
    )
    main = result(app, company, fulfilled)
    resolved = resolution(main, fulfilled["business_id"])
    assert resolved["basis"] == "owner_recognition_default"
    assert resolved["segments"] == [
        {
            "state": "due",
            "ranges": [{"from_fen": "0", "to_fen": "5050"}],
            "tax_period": "2026-01",
            "tax_on": "2026-01-08",
            "trigger": "owner_receipt_default",
            "event_ids": [],
            "receipt_ids": [paid["business_id"]],
        },
        {
            "state": "due",
            "ranges": [{"from_fen": "5050", "to_fen": "10100"}],
            "tax_period": "2026-02",
            "trigger": "owner_recognition_default",
            "event_ids": [],
        },
    ]
    original = result(app, company, advance)
    adopted = next(
        source
        for source in main["basis"]["source_results"]
        if source["business_id"] == advance["business_id"] and source["result_slot"] == "main"
    )
    assert adopted["result_id"] == original["reference"]["result_id"]
    assert adopted["result_generation"] == original["reference"]["generation"]
    assert any(
        source["business_id"] == previous_point["business_id"]
        for source in original["basis"]["source_results"]
    )
    assert revenue(app, company) == "10000"


def test_explicit_tax_month_preserves_owner_source_despite_late_cash(system):
    app, company, ids = system
    rule = policy(ids)
    first, second = sale(ids, rule, "2026-02"), sale(ids, rule, "2026-03")
    second["period"] = "2026-02"
    for earned in (first, second):
        earned["data"]["tax_point"]["source"]["location"] = "老板明确：本年均采用业务次月"
    save(app, company, first, second, rule, reach(ids, "2026-03"))
    save(app, company, receipt(ids, first), receipt(ids, second))
    assert sales(app, company) == sorted(
        [(first["business_id"], "2026-02", "100"), (second["business_id"], "2026-03", "100")]
    )
    assert revenue(app, company, "2026-01", "2026-01") == "10000"
    assert revenue(app, company, "2026-02", "2026-02") == "10000"
    for earned in (first, second):
        basis = result(app, company, earned)["basis"]
        assert basis["input"]["tax_point"] == {
            "type": "explicit",
            "tax_period": earned["data"]["tax_point"]["tax_period"],
        }
        assert earned["data"]["tax_point"]["source"] in basis["source_locations"]


def test_unrelated_cash_does_not_refresh_default_income_or_tax_results(system):
    app, company, ids = system
    rule = policy(ids)
    earned, another = earned_business(ids, rule), sale(ids, rule, "2026-03")
    save(app, company, earned, another, rule)
    baseline = deepcopy(get(app, company, earned))
    save(app, company, receipt(ids, another))
    assert get(app, company, earned) == baseline


def test_late_collection_preserves_closed_income_tax_and_frozen_original_results(system):
    app, company, ids = system
    rule = policy(ids)
    earned = earned_business(ids, rule)
    opening = fact(ids, "opening.package", {"basis": "newly_established", "members": []})
    statement = fact(
        ids,
        "money.bank_statement",
        {"funds_account_id": ids["bank"], "opening_fen": "0", "closing_fen": "0", "rows": []},
    )
    reconciliation = fact(
        ids, "money.reconciliation", {"statement_id": statement["business_id"], "matches": []}
    )
    save(app, company, earned, rule, opening, statement, reconciliation)
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
        before = deepcopy(app.ledger.get_results([earned["business_id"]], read))
    current_before = deepcopy(result(app, company, earned))
    save(app, company, receipt(ids, earned), correction_period="2026-03")
    with app.storage.read_unit(company, view="as_posted", as_of="2026-01") as read:
        assert app.ledger.get_results([earned["business_id"]], read) == before
    assert result(app, company, earned) == current_before
    assert sales(app, company) == [(earned["business_id"], "2026-01", "100")]
    assert revenue(app, company) == "10000"


@pytest.mark.parametrize("filing", ["month", "quarter"])
def test_reported_revenue_tax_sales_and_assessment_share_period_and_net_meaning(system, filing):
    app, company, ids = system
    rule = policy(ids)
    rule["data"]["threshold_period"] = filing
    earned = []
    for month, amount in (("2026-01", "10100"), ("2026-02", "20200"), ("2026-03", "10100")):
        current = earned_business(ids, rule)
        current["period"] = month
        current["data"]["gross_fen"] = amount
        earned.append(current)
    returned = fact(
        ids,
        "operations.sale_return",
        {
            "sale_id": earned[0]["business_id"],
            "customer_id": ids["party"],
            "gross_fen": "3030",
            "credit_note_vat_fen": "30",
        },
        period="2026-03",
    )
    opening = fact(ids, "opening.package", {"basis": "newly_established", "members": []})
    extra = fact(
        ids,
        "taxes.surtax_policy",
        {
            "policy": {
                "version": "synthetic-surtax-1",
                "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "official_url": "https://example.gov.cn/synthetic-surtax",
            },
            "urban_rate": "0",
            "education_rate": "0",
            "local_education_rate": "0",
            "payable_fraction": "1",
        },
    )
    assessments = [
        fact(
            ids,
            "taxes.assessment",
            {
                "type": "vat",
                "start_month": start,
                "end_month": end,
                "vat_policy_id": rule["business_id"],
                "surtax_policy_id": extra["business_id"],
                "filing_profile": {
                    "taxpayer_type": "small_scale",
                    "form_scope": "nonpilot",
                    "source": {"source_id": ids["source"]},
                },
            },
            period=end,
        )
        for start, end in (
            [("2026-01", "2026-03")]
            if filing == "quarter"
            else [(month, month) for month in ("2026-01", "2026-02", "2026-03")]
        )
    ]
    save(app, company, *assessments, returned, *earned, rule, extra, opening)
    expected_monthly = {"2026-01": "10000", "2026-02": "20000", "2026-03": "7000"}
    with app.storage.read_unit(company) as read:
        for month, expected in expected_monthly.items():
            report = app.ledger.financial_snapshot(
                {
                    "company_id": company,
                    "period_from": month,
                    "period_to": month,
                    "view": "current",
                },
                read,
            )
            assert report["profit_statement"]["1"]["current"]["amount_fen"] == expected
            tax_rows = app.ledger.tax_sales(month, month, read)
            assert str(sum(int(row.sale["net_sales_fen"]) for row in tax_rows)) == expected
    quarter = app.accounting_report({"company_id": company, "year": 2026, "quarter": 1})
    assert quarter["profit_statement"]["1"]["current"]["amount_fen"] == "37000"
    for assessed in assessments:
        month = assessed["data"]["start_month"]
        expected = "37000" if filing == "quarter" else expected_monthly[month]
        values = result(app, company, assessed)["target"]["values"]
        assert values["net_sales_fen"] == values["filing_fields"]["net_sales_fen"] == expected
    tax_before = sales(app, company)
    report_before = quarter["profit_statement"]
    save(app, company, receipt(ids, earned[0], amount="7070", day="2026-04-08"))
    assert sales(app, company) == tax_before
    assert (
        app.accounting_report({"company_id": company, "year": 2026, "quarter": 1})[
            "profit_statement"
        ]
        == report_before
    )
