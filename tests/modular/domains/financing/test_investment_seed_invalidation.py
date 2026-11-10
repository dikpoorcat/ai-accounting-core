"""A month checkpoint is reusable only while its numerical inputs stay retained."""

import json
from copy import deepcopy

import pytest

from ai_accounting_modular.domains.financing import _investment_candidate
from tests.modular.ledger.opening.test_investment_cutover import submit

from ._investment_support import acquire, dispose, terms
from ._support import COUNTERPARTY, PRODUCT, assemble, change, get, request, result, six
from .test_investment_lifecycle import apply_facts
from .test_investment_seed import _income, _initial


def month_position(app, company, agreement, month):
    return next(
        body
        for body in get(app, company, agreement["business_id"])["businesses"][0]["results"]
        if body["reference"]["result_slot"] == "position:" + month
    )


def advance(app, company, month):
    funding = six()[0]
    funding.update(period=month, effective_on=month + "-01")
    return apply_facts(app, company, funding)


def rename(app, company, object_id):
    with app.storage.read_unit(company) as read:
        (current,) = app.identity.get_batch((object_id,), read)
    payload = request(company)
    payload.pop("sources")
    payload["objects"] = [
        {
            **{key: current[key] for key in ("object_id", "kind", "data")},
            "name": "更名后的合成投资对象",
            "expected_seq": current["seq"],
        }
    ]
    actual = app.accounting_apply(payload)
    assert actual.get("completion") == "complete", json.dumps(actual, ensure_ascii=False)


def assert_only_earlier_checkpoints(*bodies):
    for body in bodies:
        for reference in body["basis"]["source_results"]:
            slot = reference["result_slot"]
            if slot.startswith("position:"):
                assert slot.removeprefix("position:") < body["basis"]["period"]


def assert_adopts(body, producer):
    reference = producer["reference"]
    assert any(
        all(source[key] == reference[key] for key in ("business_id", "result_id", "result_slot"))
        and source["result_generation"] == reference["generation"]
        for source in body["basis"]["source_results"]
    )


@pytest.fixture
def replays(monkeypatch):
    traces = []
    original = _investment_candidate.replay

    def replay(terms_id, profile, facts, **kwargs):
        facts = tuple(facts)
        traces.append(
            {
                "events": [row["business_id"] for row in facts],
                "start_period": kwargs["start_period"],
                "initial_cost": sum(row.remaining_cost for row in kwargs["initial"]),
            }
        )
        return original(terms_id, profile, facts, **kwargs)

    monkeypatch.setattr(_investment_candidate, "replay", replay)
    return traces


@pytest.mark.parametrize(
    ("category", "measurement"),
    [
        ("money_fund", "short_term_cost"),
        ("bond", "long_bond_cost"),
        ("equity", "long_equity_cost"),
    ],
)
def test_product_rename_after_calendar_advance_recalculates_without_self_seed(
    tmp_path, category, measurement, replays
):
    app, company = assemble(tmp_path)
    schedule = [{"payment_on": "2026-06-30", "coupon_rate": "0.1"}] if category == "bond" else None
    agreement = terms(product_type=category, measurement=measurement, schedule=schedule)
    purchase = acquire(agreement, "100", "3")
    apply_facts(app, company, agreement, purchase, initial=True)
    advance(app, company, "2026-02")
    before = result(app, company, purchase)["target"]["values"]
    replays.clear()

    rename(app, company, PRODUCT)

    purchased = result(app, company, purchase)
    current_terms = result(app, company, agreement)
    january = month_position(app, company, agreement, "2026-01")
    february = month_position(app, company, agreement, "2026-02")
    assert purchased["target"]["values"] == before
    assert replays and all(row["events"] == [purchase["business_id"]] for row in replays)
    assert_adopts(purchased, current_terms)
    for body in (january, february):
        assert body["target"]["values"]["closing_cost_fen"] == "100"
        assert body["target"]["values"]["quantity"] == "3"
        assert_adopts(body, current_terms)
    assert_only_earlier_checkpoints(purchased, january, february)


def test_counterparty_rename_dirties_historical_event_without_a_future_checkpoint(
    tmp_path, replays
):
    app, company = assemble(tmp_path)
    agreement = terms()
    purchase = acquire(agreement, "100", "3")
    apply_facts(app, company, agreement, purchase, initial=True)
    advance(app, company, "2026-02")
    replays.clear()

    rename(app, company, COUNTERPARTY)

    purchased = result(app, company, purchase)
    february = month_position(app, company, agreement, "2026-02")
    assert purchased["target"]["values"]["cost_fen"] == "100"
    assert replays and all(row["events"] == [purchase["business_id"]] for row in replays)
    assert february["target"]["values"]["closing_cost_fen"] == "100"
    assert_only_earlier_checkpoints(purchased, february)


def test_opening_only_holding_product_rename_rebuilds_from_current_terms_source(tmp_path):
    app, company, product, agreement, _, package = _initial(tmp_path)
    actual = submit(app, company, agreement, package, products=[(product, "other_fund")])
    assert actual.get("completion") == "complete", actual
    actual = submit(app, company, _income(agreement))
    assert actual.get("completion") == "complete", actual

    rename(app, company, product)

    adopted = result(app, company, agreement)
    opening = result(app, company, package)
    for month in ("2026-07", "2026-08"):
        body = month_position(app, company, agreement, month)
        assert_adopts(body, adopted)
        assert_adopts(body, opening)
        assert body["target"]["values"]["closing_cost_fen"] == "1000"
        assert body["target"]["values"]["quantity"] == "10"
        assert_only_earlier_checkpoints(body)


def test_actual_terms_input_change_rebuilds_retained_month_classification(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms(measurement="other_financial_cost")
    agreement["data"]["holding_arrangement"] = "current"
    purchase = acquire(agreement, "100", "3")
    apply_facts(app, company, agreement, purchase, initial=True)
    advance(app, company, "2026-02")

    changed = deepcopy(agreement)
    changed["data"]["holding_arrangement"] = "noncurrent"
    actual = app.accounting_apply(change(company, changed))
    assert actual.get("completion") == "complete", actual

    for month in ("2026-01", "2026-02"):
        values = month_position(app, company, agreement, month)["target"]["values"]
        assert values["closing_cost_fen"] == "100"
        assert values["holding_arrangement"] == "noncurrent"
        assert all(row["account_code"] == "189902" for row in values["lots"])


def test_historical_cost_change_and_new_later_exit_share_updated_interval(tmp_path):
    app, company = assemble(tmp_path)
    agreement = terms()
    purchase = acquire(agreement, "100", "3")
    first_exit = dispose(agreement, "1", "50", "2026-01-04")
    apply_facts(app, company, agreement, purchase, first_exit, initial=True)
    advance(app, company, "2026-02")

    changed = deepcopy(purchase)
    changed["data"]["purchase_price_fen"] = "130"
    later_exit = dispose(agreement, "1", "50", "2026-02-02")
    payload = change(company, changed)
    payload["mutations"].append({"op": "create", "expected_seq": 0, "fact": later_exit})
    actual = app.accounting_apply(payload)
    assert actual.get("completion") == "complete", actual

    earlier = result(app, company, first_exit)
    later = result(app, company, later_exit)
    january = month_position(app, company, agreement, "2026-01")
    february = month_position(app, company, agreement, "2026-02")
    assert earlier["target"]["values"]["cost_fen"] == "43"
    assert later["target"]["values"]["cost_fen"] == "44"
    assert january["target"]["values"]["closing_cost_fen"] == "87"
    assert february["target"]["values"]["opening_cost_fen"] == "87"
    assert february["target"]["values"]["closing_cost_fen"] == "43"
    assert february["target"]["values"]["quantity"] == "1"
    updated_purchase = result(app, company, purchase)
    assert_adopts(earlier, updated_purchase)
    assert_adopts(later, updated_purchase)
    assert_only_earlier_checkpoints(earlier, later, january, february)


def test_calendar_only_advance_consumes_stable_prefix_without_replaying_history(
    tmp_path, replays, record_property
):
    app, company = assemble(tmp_path)
    agreement = terms()
    purchase = acquire(agreement, "100", "3")
    first_exit = dispose(agreement, "1", "50", "2026-01-04")
    apply_facts(app, company, agreement, purchase, first_exit, initial=True)
    january = month_position(app, company, agreement, "2026-01")
    replays.clear()
    advance(app, company, "2026-02")
    february = month_position(app, company, agreement, "2026-02")
    assert month_position(app, company, agreement, "2026-01")["reference"] == january["reference"]
    assert_adopts(february, january)
    assert replays and all(row["events"] == [] and row["initial_cost"] == 67 for row in replays)
    february_trace = deepcopy(replays)
    replays.clear()

    later_exit = dispose(agreement, "1", "50", "2026-03-02")
    apply_facts(app, company, later_exit)
    march = month_position(app, company, agreement, "2026-03")
    assert month_position(app, company, agreement, "2026-02")["reference"] == february["reference"]
    assert_adopts(march, february)
    assert replays and all(row["events"] == [later_exit["business_id"]] for row in replays)
    assert all(row["start_period"] == "2026-03" and row["initial_cost"] == 67 for row in replays)
    assert result(app, company, later_exit)["target"]["values"]["cost_fen"] == "34"
    assert march["target"]["values"]["closing_cost_fen"] == "33"
    record_property("calendar_only_february", json.dumps(february_trace))
    record_property("march_exit", json.dumps(replays))
