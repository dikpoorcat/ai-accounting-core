"""Independent operation counts and sourced profile counterexamples through the app."""

from copy import deepcopy

import pytest

from ai_accounting_modular.application._context import SubmissionContext
from ai_accounting_modular.domains.financing import _investment_candidate, _investment_rules
from ai_accounting_modular.domains.financing._investment_events import POSITION_KINDS
from ai_accounting_modular.domains.financing._investment_math import Lot
from ai_accounting_modular.domains.financing.public import Investment

from ._investment_support import acquire, dispose, source, terms
from ._support import COUNTERPARTY, PRODUCT, SOURCE, assemble, fact, request, result
from .test_investment_lifecycle import apply_facts


@pytest.fixture
def work(monkeypatch):
    measured = {"replays": [], "wire": 0, "related_inventory": 0, "dependencies": []}
    original_replay = _investment_candidate.replay
    original_wire = Lot.wire
    original_related = SubmissionContext.related_facts
    original_describe = Investment.describe_change

    def replay(terms_id, profile, facts, **kwargs):
        facts = tuple(facts)
        measured["replays"].append(len(facts))
        return original_replay(terms_id, profile, facts, **kwargs)

    def wire(self, **kwargs):
        measured["wire"] += 1
        return original_wire(self, **kwargs)

    def related(self, *args, **kwargs):
        values = original_related(self, *args, **kwargs)
        measured["related_inventory"] += sum(row["kind"] in POSITION_KINDS for row in values)
        return values

    def describe(self, *args, **kwargs):
        value = original_describe(self, *args, **kwargs)
        measured["dependencies"].append(len(value["dependencies"]))
        return value

    monkeypatch.setattr(_investment_candidate, "replay", replay)
    monkeypatch.setattr(Lot, "wire", wire)
    monkeypatch.setattr(SubmissionContext, "related_facts", related)
    monkeypatch.setattr(Investment, "describe_change", describe)
    return measured


@pytest.mark.parametrize("events", [50, 100, 200])
def test_one_ordered_replay_and_linear_source_edges(events, tmp_path, work):
    app, company = assemble(tmp_path)
    holding = terms()
    purchases = [acquire(holding, "3", "1") for _ in range(events // 2)]
    exits = [dispose(holding, "1", "4") for _ in range(events // 2)]
    apply_facts(app, company, holding, *purchases, *exits, initial=True)
    assert work["replays"] == [events]
    assert work["wire"] == events // 2  # Created lots only; no complete lots per event prefix.
    assert max(work["dependencies"]) < 12 * events
    assert work["related_inventory"] <= 5 * events  # Description rounds plus one candidate load.


def _income(holding, day):
    return fact(
        "investment.income",
        {
            "terms_id": holding["business_id"],
            "counterparty_id": COUNTERPARTY,
            "income_character": "fund_distribution",
            "amount_fen": "5",
        },
        period=day[:7],
        actual=day,
    )


@pytest.mark.parametrize("day", ["2026-01-31", "2026-02-01"])
def test_ordinary_income_never_replays_or_loads_old_inventory(day, tmp_path, work):
    app, company = assemble(tmp_path)
    holding = terms()
    purchases = [acquire(holding, "3", "1") for _ in range(50)]
    apply_facts(app, company, holding, *purchases, initial=True)
    for key in work:
        work[key] = [] if key in {"replays", "dependencies"} else 0
    income = _income(holding, day)
    apply_facts(app, company, income)
    assert sum(work["replays"]) == 0
    assert work["related_inventory"] == 0
    body = result(app, company, income)
    assert "remaining_quantity" not in body["target"]["values"]
    assert "quantity" not in body["target"]["values"]


def test_month_checkpoint_preserves_cumulative_rounding_and_withdrawal_changes_epoch(
    tmp_path, work
):
    app, company = assemble(tmp_path)
    holding = terms()
    purchase = acquire(holding, "1", "4")
    first = dispose(holding, "1", "1")
    apply_facts(app, company, holding, purchase, first, initial=True)
    work["replays"].clear()
    second = dispose(holding, "1", "1", "2026-02-01")
    apply_facts(app, company, second)
    assert work["replays"] == [1]
    assert result(app, company, second)["target"]["values"]["cost_fen"] == "1"
    from uuid import uuid4

    work["replays"].clear()
    actual = app.accounting_apply(
        {
            "contract": "ai-accounting-modular/1",
            "company_id": company,
            "command_id": str(uuid4()),
            "mutations": [
                {"op": "withdraw", "business_id": first["business_id"], "expected_seq": 1}
            ],
        }
    )
    assert actual.get("completion") == "complete", actual
    assert work["replays"] == [2]
    assert result(app, company, second)["target"]["values"]["cost_fen"] == "0"


def _bond():
    return terms(
        product_type="bond",
        measurement="long_bond_cost",
        schedule=[
            {"payment_on": "2026-06-30", "coupon_rate": "0.1"},
            {"payment_on": "2026-12-31", "coupon_rate": "0.1"},
        ],
    )


@pytest.mark.parametrize(
    "invalid", ["bypass_amortization", "tax_rights", "missing_intent_known_bypass"]
)
def test_real_terms_reject_profile_and_tax_label_counterexamples(tmp_path, invalid):
    app, company = assemble(tmp_path)
    holding = _bond()
    if invalid in {"bypass_amortization", "missing_intent_known_bypass"}:
        holding["data"]["measurement"] = "other_financial_cost"
        if invalid == "missing_intent_known_bypass":
            holding["data"].pop("initial_holding_intent")
    else:
        holding["data"]["tax_facts"]["instrument_type"] = "securities_fund"
    payload = request(company, holding)
    for obj in payload["objects"]:
        if obj["object_id"] == PRODUCT:
            obj["data"]["category"] = "bond"
    rejected = app.accounting_apply(payload)
    assert rejected["error"] == "invalid_input", rejected


def test_financial_contract_cannot_claim_government_bond_tax_identity(tmp_path):
    app, company = assemble(tmp_path)
    holding = terms(product_type="bank_wealth", measurement="other_financial_cost")
    holding["data"].update(
        legal_form="financial_contract",
        tax_facts={
            "instrument_type": "government_bond",
            "original_issue_on": "2020-01-01",
            "source": {"source_id": SOURCE},
        },
    )
    payload = request(company, holding)
    for obj in payload["objects"]:
        if obj["object_id"] == PRODUCT:
            obj["data"]["category"] = "bank_wealth"
    rejected = app.accounting_apply(payload)
    assert rejected["error"] == "invalid_input", rejected


def test_bank_management_category_and_current_reporting_do_not_change_real_long_bond(tmp_path):
    app, company = assemble(tmp_path)
    holding = _bond()
    holding["data"].update(product_type="bank_wealth", holding_arrangement="current")
    purchased = acquire(holding, "102000", "100000")
    coupon = fact(
        "investment.debt_interest",
        {
            "terms_id": holding["business_id"],
            "source": source(purchased),
            "counterparty_id": COUNTERPARTY,
            "payment_on": "2026-06-30",
        },
        period="2026-06",
        actual="2026-06-30",
    )
    apply_facts(app, company, holding, purchased, coupon, initial=True)
    assert result(app, company, coupon)["target"]["values"]["gain_loss_fen"] == "9000"


def test_current_new_calculation_rechecks_an_old_frozen_invalid_profile(tmp_path, monkeypatch):
    app, company = assemble(tmp_path)
    holding = _bond()
    holding["data"]["measurement"] = "other_financial_cost"
    purchase = acquire(holding, "102000", "100000")
    with monkeypatch.context() as old:
        old.setattr(_investment_rules, "validate_term_compatibility", lambda fact: None)
        apply_facts(app, company, holding, purchase, initial=True)
    frozen = deepcopy(result(app, company, holding))
    with app.storage.write_unit(company) as unit:
        unit.repository("ledger").freeze_heads("2026-01")
    coupon = fact(
        "investment.debt_interest",
        {
            "terms_id": holding["business_id"],
            "source": source(purchase),
            "counterparty_id": COUNTERPARTY,
            "payment_on": "2026-06-30",
        },
        period="2026-06",
        actual="2026-06-30",
    )
    payload = request(company, coupon)
    payload.pop("objects")
    payload.pop("sources")
    rejected = app.accounting_apply(payload)
    assert rejected["error"] == "invalid_input", rejected
    assert result(app, company, holding)["target"] == frozen["target"]
