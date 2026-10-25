"""A complete tax point is the sole authority for an advance's tax state."""

from copy import deepcopy
from uuid import uuid4

import pytest

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


def legal_point(ids, advance, *, invoiced=True, tax_month="2026-01"):
    source = {"source_id": ids["source"], "location": "已保存的服务及开票资料"}
    events = [
        {
            "event_id": str(uuid4()),
            "type": kind,
            "month": day[:7],
            "on": day,
            "source": source,
            "ranges": [{"from_fen": "0", "to_fen": "10100"}],
        }
        for kind, day in (("service_start", "2026-01-09"), ("invoice", tax_month + "-10"))
    ]
    point = fact(
        ids,
        "taxes.tax_point",
        {
            "target_business_id": advance["business_id"],
            "transaction_mode": "ordinary_service",
            "service_state": "started" if invoiced else "not_started",
            "contractual_entitlement": "not_set",
            "events": events if invoiced else [],
        },
    )
    advance["data"]["tax_point"] = {
        "type": "derived",
        "tax_point_id": point["business_id"],
    }
    return point


def policy_dependency(app, company, advance, rule):
    with app.storage.read_unit(company) as read:
        return next(
            edge
            for edge in read.repository("application").dependencies([advance["business_id"]])
            if edge["producer_id"] == rule["business_id"] and edge["consumer_result_slot"] == "main"
        )


@pytest.mark.parametrize("point_type", ["explicit", "derived"])
@pytest.mark.parametrize("raw_status", [None, "not_due"])
def test_complete_tax_point_determines_advance_state_without_second_confirmation(
    system, point_type, raw_status
):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, "2026-01", advance=True)
    advance["data"].pop("tax_status")
    if raw_status is not None:
        advance["data"]["tax_status"] = raw_status
    points = [legal_point(ids, advance)] if point_type == "derived" else []
    save(app, company, advance, rule, *points)

    main = result(app, company, advance)
    assert main["basis"]["input"]["tax_status"] == "due"
    assert main["target"]["values"]["vat_fen"] == "100"
    assert sales(app, company) == [(advance["business_id"], "2026-01", "100")]
    assert get(app, company, advance)["fact"] == advance


def test_new_legal_events_update_derived_state_without_rewriting_stale_raw_status(system):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, advance=True)
    point = legal_point(ids, advance, invoiced=False)
    save(app, company, advance, point, rule)
    assert result(app, company, advance)["basis"]["input"]["tax_status"] == "not_due"
    final = legal_point(ids, deepcopy(advance))
    final["business_id"] = point["business_id"]
    save(
        app,
        company,
        mutations=[{"op": "replace", "expected_seq": 1, "fact": final}],
    )
    assert result(app, company, advance)["basis"]["input"]["tax_status"] == "due"
    assert get(app, company, advance)["fact"] == advance
    assert sales(app, company) == [(advance["business_id"], "2026-01", "100")]


@pytest.mark.parametrize(
    "point_type,raw_status",
    [
        (None, "not_due"),
        ("owner_recognition_default", None),
        ("owner_recognition_default", "due"),
        ("derived", None),
        ("derived", "due"),
    ],
)
def test_untaxed_advance_does_not_adopt_future_vat_but_fulfillment_does(
    system, point_type, raw_status
):
    app, company, ids = system
    rule = policy(ids)
    rule["data"]["policy"]["effective_from"] = "2026-02-01"
    advance = sale(ids, rule, advance=True)
    advance["data"].pop("tax_point")
    points = []
    if point_type is not None:
        advance["data"].pop("tax_status")
        if point_type == "derived":
            points.append(legal_point(ids, advance, invoiced=False))
        else:
            advance["data"]["tax_point"] = {"type": point_type}
    if raw_status is not None:
        advance["data"]["tax_status"] = raw_status
    save(app, company, advance, rule, *points)
    main = result(app, company, advance)
    assert main["basis"]["input"]["tax_status"] == "not_due"
    assert "vat" not in main["basis"]["input"]
    assert main["basis"]["policies"] == []
    assert rule["business_id"] not in {
        row["business_id"] for row in main["basis"]["source_results"]
    }
    expected_mode = "result" if point_type == "derived" else "fact"
    assert policy_dependency(app, company, advance, rule)["read_mode"] == expected_mode
    assert sales(app, company) == []

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
    save(app, company, fulfilled)
    basis = result(app, company, fulfilled)["basis"]
    assert rule["business_id"] in {row["business_id"] for row in basis["source_results"]}
    assert basis["policies"][0]["policy_business_id"] == rule["business_id"]
    assert policy_dependency(app, company, fulfilled, rule)["read_mode"] == "result"
    assert sales(app, company) == [(fulfilled["business_id"], "2026-02", "100")]


@pytest.mark.parametrize("point_type", ["explicit", "derived"])
def test_future_tax_point_still_prices_and_adopts_its_policy(system, point_type):
    app, company, ids = system
    rule = policy(ids)
    rule["data"]["policy"]["effective_from"] = "2026-03-01"
    advance = sale(ids, rule, advance=True)
    advance["data"].pop("tax_status")
    points = [legal_point(ids, advance, tax_month="2026-03")] if point_type == "derived" else []
    save(app, company, advance, rule, *points)
    main = result(app, company, advance)
    assert main["basis"]["input"]["tax_status"] == "not_due"
    assert main["basis"]["input"]["vat"]["policy_id"] == rule["business_id"]
    assert rule["business_id"] in {row["business_id"] for row in main["basis"]["source_results"]}
    assert policy_dependency(app, company, advance, rule)["read_mode"] == "result"
    save(app, company, reach(ids, "2026-03"))
    assert sales(app, company) == [(advance["business_id"], "2026-03", "100")]


@pytest.mark.parametrize("point_type", ["explicit", "derived"])
@pytest.mark.parametrize("fully_refunded", [False, True])
def test_unpriced_future_advance_requires_only_its_surviving_tax_basis(
    system, point_type, fully_refunded
):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, advance=True)
    advance["data"].pop("tax_status")
    advance["data"].pop("vat")
    points = [legal_point(ids, advance, tax_month="2026-03")] if point_type == "derived" else []
    save(app, company, advance, *points)
    before = deepcopy(result(app, company, advance))
    assert before["basis"]["input"]["tax_status"] == "not_due"
    assert before["basis"]["policies"] == []
    assert sales(app, company) == []
    if fully_refunded:
        refunded = fact(
            ids,
            "operations.advance_refund",
            {"advance_id": advance["business_id"], "gross_fen": "10100"},
            period="2026-02",
            actual="2026-02-10",
        )
        save(app, company, refunded, reach(ids, "2026-03"))
        released = result(app, company, advance, "tax:2026-03")
        assert released["target"]["values"]["gross_fen"] == "0"
        assert released["basis"]["policies"] == []
        assert released["vouchers"] == []
        assert sales(app, company) == []
        assert refunded["business_id"] in {
            row["business_id"] for row in released["basis"]["source_results"]
        }
        return
    with app.storage.read_unit(company) as read:
        head = read.head
    rejected = app.accounting_apply(command(company, reach(ids, "2026-03")))
    assert rejected["completion"] == "not_saved" and rejected["error"] == "needs_information"
    assert any(
        row["business_id"] == advance["business_id"] and row["field"] == "/data/vat"
        for row in rejected["fact_issues"]
    )
    with app.storage.read_unit(company) as read:
        assert read.head == head
    assert result(app, company, advance) == before


def test_actual_tax_month_day_conflict_still_fails_atomically(system):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, "2026-01", advance=True)
    advance["data"].pop("tax_status")
    advance["data"]["tax_point"]["tax_on"] = "2026-02-01"
    with app.storage.read_unit(company) as read:
        before = read.head
    rejected = app.accounting_apply(command(company, advance, rule))
    assert rejected["completion"] == "not_saved", rejected
    assert rejected["error"] == "invalid_input", rejected
    assert any(row["field"] == "/data/tax_point/tax_on" for row in rejected["fact_issues"])
    with app.storage.read_unit(company) as read:
        assert read.head == before
    absent = app.accounting_get(
        {
            "company_id": company,
            "business_ids": [advance["business_id"], rule["business_id"]],
            "include": ["facts", "results"],
        }
    )
    assert absent["businesses"] == []


def test_later_receipt_updates_open_basis_without_rewriting_closed_advance_history(system):
    app, company, ids = system
    rule = policy(ids)
    advance = sale(ids, rule, advance=True)
    point = fact(
        ids,
        "taxes.tax_point",
        {"type": "owner_receipt_default", "target_business_id": advance["business_id"]},
    )
    advance["data"]["tax_point"] = {
        "type": "derived",
        "tax_point_id": point["business_id"],
    }
    opening = fact(ids, "opening.package", {"basis": "newly_established", "members": []})
    statement = fact(
        ids,
        "money.bank_statement",
        {"funds_account_id": ids["bank"], "opening_fen": "0", "closing_fen": "0", "rows": []},
    )
    reconciliation = fact(
        ids, "money.reconciliation", {"statement_id": statement["business_id"], "matches": []}
    )
    save(app, company, advance, point, rule, opening, statement, reconciliation)
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
        before = deepcopy(app.ledger.get_results([advance["business_id"]], read))
    save(app, company, receipt(ids, advance), correction_period="2026-03")
    with app.storage.read_unit(company, view="as_posted", as_of="2026-01") as read:
        assert app.ledger.get_results([advance["business_id"]], read) == before
    assert get(app, company, advance)["fact"] == advance
    assert sales(app, company) == [(advance["business_id"], "2026-03", "100")]
