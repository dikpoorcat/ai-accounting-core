"""Independent accounting oracles through the real application and SQLite ledger."""

from copy import deepcopy

import pytest
from test_tax_lifecycle import (
    command,
    fact,
    get,
    point,
    policy,
    reach,
    result,
    sale,
)
from test_tax_lifecycle import system as system

from ai_accounting_modular.application.public import create_backup_verifier


def _balances(app, company, *businesses):
    balances = {}
    for business in businesses:
        for calculation in get(app, company, business)["results"]:
            for voucher in calculation["vouchers"]:
                for line in voucher["lines"]:
                    code = line["account_code"]
                    balances[code] = (
                        balances.get(code, 0) + int(line["debit_fen"]) - int(line["credit_fen"])
                    )
    return balances


@pytest.mark.parametrize("red_vat,fulfillment_vat", [(0, 1), (1, 0)])
def test_original_three_percent_rounding_and_actual_red_vat_both_survive_complete_consumption(
    system, red_vat, fulfillment_vat
):
    app, company, ids = system
    rule = policy(ids)
    rule["data"].update(tax_base_rate="0.03", payable_rate="0.03")
    original = sale(ids, rule, "2026-01", advance=True)
    original["data"]["amount_fen"] = "18"
    returned = fact(
        ids,
        "operations.advance_refund",
        {
            "advance_id": original["business_id"],
            "gross_fen": "10",
            "tax_returns": [
                {
                    "state": "taxed",
                    "original_tax_period": "2026-01",
                    "ranges": [{"from_fen": "0", "to_fen": "10"}],
                    "vat_fen": str(red_vat),
                }
            ],
        },
        period="2026-02",
    )
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": original["business_id"], "gross_fen": "8"},
        period="2026-03",
    )
    saved = app.accounting_apply(command(company, fulfilled, returned, original, rule))
    assert saved["completion"] == "complete", saved
    assert result(app, company, original)["target"]["values"]["vat_fen"] == "1"
    assert result(app, company, fulfilled)["target"]["values"]["vat_fen"] == str(fulfillment_vat)
    # Refund liability remains 10; the original net advance is fully consumed.
    balances = _balances(app, company, original, returned, fulfilled)
    assert balances["2203"] == -10
    assert balances["222101"] == -fulfillment_vat


@pytest.mark.parametrize("source_month", ["2026-01", "2026-03"])
@pytest.mark.parametrize("fulfillment_day", ["2026-03-05", "2026-03-25"])
def test_known_same_month_fulfillment_and_pre_tax_refund_never_leave_deferred_debit(
    system, source_month, fulfillment_day
):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, "2026-03", advance=True)
    original["period"] = source_month
    original["data"]["tax_status"] = "due" if source_month == "2026-03" else "not_due"
    original["data"]["tax_point"]["tax_on"] = "2026-03-20"
    returned = fact(
        ids,
        "operations.advance_refund",
        {"advance_id": original["business_id"], "gross_fen": "2020"},
        period="2026-03",
        actual="2026-03-10",
    )
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": original["business_id"], "gross_fen": "8080"},
        period="2026-03",
        actual=fulfillment_day,
    )
    saved = app.accounting_apply(command(company, fulfilled, returned, original, rule))
    assert saved["completion"] == "complete", saved
    balances = _balances(app, company, original, returned, fulfilled)
    assert balances.get("222104", 0) == 0
    assert balances["222101"] == -80
    assert balances["5001"] == -8000
    assert balances["2203"] == -2020
    with app.storage.read_unit(company) as read:
        sales = app.ledger.tax_sales("2026-01", "2026-03", read)
    assert sum(int(item.sale["vat_fen"]) for item in sales) == 80


def test_several_gross_coordinates_keep_not_due_tax_separate_from_actual_red_tax(system):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, "2026-01", advance=True)
    tax_point = point(ids, original, [("2026-01", 0, 3030), ("2026-03", 3030, 7070)])
    original["data"]["tax_point"] = {"type": "derived", "tax_point_id": tax_point["business_id"]}
    # Fully release two established tax sources and the unresolved portion.
    # Only the last portion gets a new, independently sourced tax month.
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {
            "advance_id": original["business_id"],
            "gross_fen": "10100",
            "tax_point": {
                "type": "explicit",
                "tax_period": "2026-04",
                "source": {"source_id": ids["source"]},
            },
        },
        period="2026-02",
    )
    saved = app.accounting_apply(command(company, fulfilled, tax_point, original, rule))
    assert saved["completion"] == "complete", saved
    advanced = app.accounting_apply(command(company, reach(ids, "2026-04")))
    assert advanced["completion"] == "complete", advanced
    balances = _balances(app, company, original, fulfilled)
    assert balances.get("222104", 0) == 0
    assert balances["222101"] == -100
    assert balances["5001"] == -10000
    assert balances["2203"] == 0
    with app.storage.read_unit(company) as read:
        sales = app.ledger.tax_sales("2026-01", "2026-04", read)
    assert [(item.sale["tax_period"], item.sale["vat_fen"]) for item in sales] == [
        ("2026-01", "30"),
        ("2026-03", "40"),
        ("2026-04", "30"),
    ]


def test_later_refund_cannot_reassign_an_earlier_implicit_fulfillment_interval(system):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, "2026-01", advance=True)
    earlier = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": original["business_id"], "gross_fen": "8080"},
        period="2026-02",
    )
    saved = app.accounting_apply(command(company, earlier, original, rule))
    assert saved["completion"] == "complete", saved
    before = deepcopy(result(app, company, earlier))
    later = fact(
        ids,
        "operations.advance_refund",
        {
            "advance_id": original["business_id"],
            "gross_fen": "2020",
            "tax_returns": [
                {
                    "state": "taxed",
                    "original_tax_period": "2026-01",
                    "vat_fen": "20",
                    "ranges": [{"from_fen": "0", "to_fen": "2020"}],
                }
            ],
        },
        period="2026-03",
    )
    refused = app.accounting_apply(command(company, later))
    assert refused["completion"] == "not_saved", refused
    assert result(app, company, earlier) == before
    assert get(app, company, original)["fact"] == original


def test_mixed_fulfillment_accepts_the_real_old_and_new_tax_months_without_relabeling_them(
    system,
):
    app, company, ids = system
    rule = policy(ids)
    original = sale(ids, rule, "2026-01", advance=True)
    original_point = point(ids, original, [("2026-01", 0, 5050)])
    original["data"]["tax_point"] = {
        "type": "derived",
        "tax_point_id": original_point["business_id"],
    }
    fulfilled = fact(
        ids,
        "operations.advance_fulfillment",
        {"advance_id": original["business_id"], "gross_fen": "10100"},
        period="2026-02",
    )
    fulfillment_point = point(ids, fulfilled, [("2026-01", 0, 5050), ("2026-03", 5050, 10100)])
    fulfilled["data"]["tax_point"] = {
        "type": "derived",
        "tax_point_id": fulfillment_point["business_id"],
    }
    saved = app.accounting_apply(
        command(company, fulfillment_point, fulfilled, original_point, original, rule)
    )
    assert saved["completion"] == "complete", saved
    assert result(app, company, fulfilled)["target"]["values"]["revenue_fen"] == "10000"
    with app.storage.read_unit(company) as read:
        declared = read.repository("application").relations([fulfilled["business_id"]])
        assert {row["producer_id"] for row in declared} == {
            original["business_id"],
            rule["business_id"],
            fulfillment_point["business_id"],
        }
        verifier = create_backup_verifier(
            identity=app.identity,
            ledger=app.ledger,
            records=app.records,
            domains=tuple(
                dict.fromkeys(app.registry.owner(kind) for kind in app.registry.supported_kinds)
            ),
            repositories=(),
        )
        verifier.verify_snapshot(read)


def test_fully_refunded_future_advance_does_not_require_an_irrelevant_tax_rate(system):
    app, company, ids = system
    original = sale(ids, policy(ids), "2026-03", advance=True)
    del original["data"]["vat"]
    refunded = fact(
        ids,
        "operations.advance_refund",
        {"advance_id": original["business_id"], "gross_fen": "10100"},
        period="2026-02",
    )
    saved = app.accounting_apply(command(company, refunded, original))
    assert saved["completion"] == "complete", saved
    advanced = app.accounting_apply(command(company, reach(ids, "2026-03")))
    assert advanced["completion"] == "complete", advanced
    release = result(app, company, original, "tax:2026-03")
    assert release["target"]["values"] == {
        "gross_fen": "0",
        "net_sales_fen": "0",
        "vat_fen": "0",
    }
    assert not release["vouchers"]
