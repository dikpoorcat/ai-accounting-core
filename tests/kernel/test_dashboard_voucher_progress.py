"""Voucher progress presence shares exact objects and checked relationship scopes."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_banking import book as book
from test_engine import calculate, close, engine as engine, publish, save

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import Outcome
from ai_accounting.kernel.dashboard import Dashboard, _voucher_profile_progress


def _voucher():
    return {
        "summary": "本月费用", "list_summary": "费用", "type": "费用",
        "business_amount_label": "确认费用", "asset": {"asset_id": "asset", "name": "设备"},
        "asset_members": [],
        "lines": [{"party": "甲", "source_label": "购置设备",
                   "parties": [{"id": "party", "name": "甲"}]}],
    }


def _profiles(**groups):
    return {"deleted": False, "profiles": {"business": {"values": {}}, **groups}}


def _profile(ident, name):
    return {"entity_id": ident, "values": {"display_name": name}}


def test_profile_presence_excludes_exact_visible_text_and_objects_per_version():
    voucher = _voucher()
    context = _profiles(counterparties=[_profile("party", "甲")], assets=[_profile("asset", "设备")])
    context["profiles"]["business"]["values"] = {"purpose": "购置设备", "note": "确认费用"}
    assert _voucher_profile_progress(voucher, context) is False
    # A different saved voucher of the same business has different visible text.
    old = deepcopy(voucher)
    old["lines"][0]["source_label"] = "原购置用途"
    assert _voucher_profile_progress(old, context) is True
    context["deleted"] = True
    assert _voucher_profile_progress(voucher, context) is True


@pytest.mark.parametrize("groups", [
    {"employees": [_profile("party", "甲")], "counterparties": [_profile("party", "甲")]},
    {"counterparties": [_profile("another-party", "甲")]},
    {"assets": [_profile("another-asset", "设备")]},
    {"fund_accounts": [_profile("party", "甲")]},
])
def test_profile_presence_retains_distinct_identity_roles_and_fund_accounts(groups):
    assert _voucher_profile_progress(_voucher(), _profiles(**groups)) is True


def test_no_progress_page_and_focus_never_read_full_business_status(engine, monkeypatch):
    for index in range(4):
        save(engine, subject=f"charge-{index}", request=f"save-{index}")
    publish(engine, [f"charge-{index}" for index in range(4)])

    def unexpected(*args, **kwargs):
        raise AssertionError("bounded progress presence must not read complete status")

    monkeypatch.setattr(BusinessQueries, "_business_status", unexpected)
    calls = []
    original = BusinessQueries.business_progress

    def observed(queries, connection, period, subjects):
        calls.append(set(subjects))
        return original(queries, connection, period, subjects)

    monkeypatch.setattr(BusinessQueries, "business_progress", observed)
    response = Dashboard(engine).brief("2026-01", section="vouchers", limit=2, voucher_number=4)["data"]
    vouchers = response["collections"]["vouchers"]["items"]
    assert [item["has_business_progress"] for item in vouchers] == [False, False]
    assert response["focused_voucher"]["has_business_progress"] is False
    assert calls == [{"charge-0", "charge-1", "charge-3"}]


def test_closed_and_open_replacement_profiles_use_selected_adoption(engine):
    save(engine)
    publish(engine)
    close(engine)
    save(engine, amount=125, revision=1, request="replace")
    publish(engine, request="replacement", posting_period="2026-02")
    for period in ("2026-01", "2026-02"):
        vouchers = Dashboard(engine).brief(period)["data"]["collections"]["vouchers"]["items"]
        assert all(item["has_business_progress"] is False for item in vouchers)


@pytest.mark.parametrize("closed", [False, True])
def test_new_obligation_respects_historical_source_cutoff(engine, monkeypatch, closed):
    save(engine)
    publish(engine)
    if closed:
        close(engine)

    def with_obligation(version, context):
        original = calculate(version, context)
        return Outcome(original.lines, {**original.values, "obligations": [{
            "key": "test_charge:charge:primary", "name": "primary", "amount_fen": 125,
            "account": "2202", "normal": "credit", "category": "payable",
            "counterparty_id": None,
        }]})

    monkeypatch.setitem(engine.store.registry.evaluators, "test_charge", with_obligation)
    save(engine, amount=125, revision=1, request="add-obligation")
    publish(engine, request="new-obligation", posting_period="2026-02" if closed else None)
    dashboard = Dashboard(engine)
    status = dashboard.business_status("2026-01", "charge")["data"]
    vouchers = dashboard.brief("2026-01")["data"]["collections"]["vouchers"]["items"]
    # An open replacement belongs to this month. A future source is outside
    # this historical month even in its current settlement followup view.
    assert bool(status["settlements"]["obligations"]) is (not closed)
    assert bool(status["current_followups"]["settlements"]["obligations"]) is (not closed)
    assert all(item["has_business_progress"] is (not closed) for item in vouchers)


def test_narrow_presence_includes_obligation_and_source_and_payment_subjects(book):
    engine, save_fact, publish_fact, _ = book
    save_fact("expense", "office", {
        "period": "2026-09", "counterparty_id": "supplier", "amount_fen": 1000,
        "expense_class": "administration", "creditor_kind": "supplier",
    })
    publish_fact("office")
    save_fact("payment", "paid", {
        "period": "2026-09", "actual_date": "2026-09-10", "direction": "outflow",
        "bank_account_id": "bank", "counterparty_id": "supplier", "amount_fen": 1000,
        "allocations": [{"source_kind": "expense", "source_id": "office",
                         "obligation": "primary", "amount_fen": 1000}],
    })
    publish_fact("paid")
    with Dashboard(engine)._snapshot("2026-09") as snapshot:
        assert snapshot.queries.business_progress(snapshot.connection, "2026-09", {"office", "paid"}) == {
            "office": True, "paid": True,
        }


def test_narrow_presence_keeps_conflicting_declared_source_scope(monkeypatch):
    calc = {"id": "calc", "subject_id": "payment"}
    reads = SimpleNamespace(
        related_subjects=lambda subjects: subjects | {"payment"},
        prime_calculations=lambda ids: None,
        relations_many=lambda ids, **kwargs: {"calc": {"settlements": [{
            "index": 0, "source_business": {"subject_id": "frozen-source"},
            "settlement_business": {"subject_id": "payment"}, "state": "unresolved",
        }]}},
        calculation=lambda ident: calc,
        declared_subjects=lambda calc: {"declared-source"},
        declared_sources=lambda calc: [{"source_id": "declared-source"}],
    )
    selected = {"through_period": {"voucher_events": [{"calculation_id": "calc"}], "state_results": []}}
    queries = BusinessQueries(SimpleNamespace(store=None))
    monkeypatch.setattr(queries, "_reads", lambda connection: reads)
    monkeypatch.setattr(queries, "_selected_accounting", lambda *args, **kwargs: selected)
    subjects = {"declared-source", "frozen-source", "payment", "unrelated"}
    monkeypatch.setattr(queries, "_current_settlement_selection", lambda *args, **kwargs: (subjects, selected, "2026-09"))
    assert queries.business_progress(None, "2026-09", subjects) == {
        "declared-source": True, "frozen-source": True, "payment": True, "unrelated": False,
    }
