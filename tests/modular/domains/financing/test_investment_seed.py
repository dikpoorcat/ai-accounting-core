"""Legacy checkpoint fallback and same-command opening corrections are explicit."""

from copy import deepcopy

from ai_accounting_modular.application._context import SubmissionContext
from ai_accounting_modular.domains.financing import _investment_position
from ai_accounting_modular.domains.financing.public import create_investment
from tests.modular.ledger.opening._support import (
    PARTY,
    assemble,
    fact,
    get,
    opening,
    replace,
)
from tests.modular.ledger.opening.test_investment_cutover import (
    equity,
    holding,
    investment_terms,
    submit,
)


def _initial(tmp_path):
    from uuid import uuid4

    app, company = assemble(tmp_path, investment_accounting=create_investment())
    product = str(uuid4())
    terms = investment_terms(product, "other_fund", "other_financial_cost")
    member = holding(terms)
    package = opening(member, equity(1000))
    return app, company, product, terms, member, package


def _income(terms):
    return fact(
        "investment.income",
        {
            "terms_id": terms["business_id"],
            "counterparty_id": PARTY,
            "income_character": "fund_distribution",
            "amount_fen": "5",
        },
        period="2026-08",
        actual="2026-08-01",
    )


def _position(app, company, terms, month):
    return next(
        body
        for body in get(app, company, terms["business_id"])["businesses"][0]["results"]
        if body["reference"]["result_slot"] == "position:" + month
    )


def test_legacy_checkpoint_declares_full_opening_before_fallback(tmp_path, monkeypatch):
    app, company, product, terms, member, package = _initial(tmp_path)
    original = _investment_position.position_plan

    def legacy(*args, **kwargs):
        plan = original(*args, **kwargs)
        plan["target"]["values"].pop("inventory_revision")
        for quantity in [*plan["target"]["values"]["lots"], *plan["basis"]["adopted_quantities"]]:
            quantity.pop("allocation_state", None)
        return plan

    with monkeypatch.context() as old:
        old.setattr(_investment_position, "position_plan", legacy)
        # public.py imports the plan through _investment_plans, too.
        from ai_accounting_modular.domains.financing import _investment_plans

        old.setattr(_investment_plans, "position_plan", legacy)
        actual = submit(app, company, terms, package, products=[(product, "other_fund")])
        assert actual.get("completion") == "complete", actual
    assert "inventory_revision" not in _position(app, company, terms, "2026-07")["target"]["values"]
    actual = submit(app, company, _income(terms))
    assert actual.get("completion") == "complete", actual
    august = _position(app, company, terms, "2026-08")
    assert august["target"]["values"]["closing_cost_fen"] == "1000"
    assert august["target"]["values"]["inventory_revision"] == 2
    assert any(
        row["business_id"] == package["business_id"] for row in august["basis"]["source_results"]
    )


def test_opening_replacement_and_calendar_advance_cannot_reuse_stale_seed(tmp_path):
    app, company, product, terms, member, package = _initial(tmp_path)
    actual = submit(app, company, terms, package, products=[(product, "other_fund")])
    assert actual.get("completion") == "complete", actual
    corrected = deepcopy(package)
    for row in corrected["data"]["members"]:
        if row["data"]["type"] == "investment":
            row["data"].update(historical_cost_fen="1200", carrying_fen="1200")
        else:
            row["data"]["balance_fen"] = "1200"
    payload = replace(company, corrected)
    payload["mutations"].append({"op": "create", "expected_seq": 0, "fact": _income(terms)})
    actual = app.accounting_apply(payload)
    assert actual.get("completion") == "complete", actual
    august = _position(app, company, terms, "2026-08")["target"]["values"]
    assert august["opening_cost_fen"] == august["closing_cost_fen"] == "1200"


def test_each_memo_hit_guards_the_exact_seed_declared_for_that_slot(tmp_path, monkeypatch):
    from ._investment_support import acquire, dispose, terms
    from ._support import assemble as finance_app
    from .test_investment_lifecycle import apply_facts

    app, company = finance_app(tmp_path)
    agreement = terms()
    apply_facts(app, company, agreement, acquire(agreement, "100", "10"), initial=True)
    guarded = []
    original = SubmissionContext.require_result_sources

    def guard(self, refs):
        refs = tuple(refs)
        if any(
            row["business_id"] == agreement["business_id"] and row["slot"] == "position:2026-01"
            for row in refs
        ):
            guarded.append(tuple(self._active_slots))
        return original(self, refs)

    monkeypatch.setattr(SubmissionContext, "require_result_sources", guard)
    exits = [dispose(agreement, "1", "12", f"2026-02-{day:02d}") for day in (2, 3, 4)]
    apply_facts(app, company, *exits)
    assert all(any((item["business_id"], "main") in slots for slots in guarded) for item in exits)
    assert len(guarded) >= 5  # First build, three events, month result and final verification.


def test_resumed_fifo_ambiguity_points_to_original_acquisition_not_opening_members(tmp_path):
    from ._investment_support import acquire, dispose, terms
    from ._support import assemble as finance_app
    from ._support import request
    from .test_investment_lifecycle import apply_facts

    app, company = finance_app(tmp_path)
    agreement = terms()
    purchases = [acquire(agreement, value, "3") for value in ("100", "300")]
    apply_facts(app, company, agreement, *purchases, initial=True)
    payload = request(company, dispose(agreement, "1", "120", "2026-02-01"))
    payload.pop("objects")
    payload.pop("sources")
    rejected = app.accounting_apply(payload)
    assert rejected["error"] == "needs_information", rejected
    assert any(
        issue["field"] == "/data/transaction_sequence"
        and issue["business_id"] in {row["business_id"] for row in purchases}
        for issue in rejected["fact_issues"]
    )
