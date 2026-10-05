"""Full asset money remains complete while card details follow the requested page."""

import pytest
from stage9_metrics import measure_work
from test_asset_batches import month
from test_reimbursement_assets import activate_assets, activation, asset, fact_evidence
from test_reimbursement_assets import book as book_fixture

from ai_accounting.kernel.dashboard import Dashboard, _assets

book = book_fixture


@pytest.fixture
def cards(book):
    engine, save, publish = book
    identities = [f"card-{index:02}" for index in range(31)]
    for index, ident in enumerate(identities):
        cost = 120000 + 12 * index
        save("reimbursed_asset", ident, asset(
            asset_id=ident, cost_fen=cost,
            creditors=[{"employee_id": "alice", "amount_fen": cost}],
        ))
    publish(*identities)
    evidence = fact_evidence(engine, identities[0])
    activate_assets(
        engine, "activate-cards", "2026-02",
        [("activate-" + ident, activation(asset_id=ident)) for ident in identities],
        evidence,
    )
    month(engine, evidence[0], "2026-03", "consume-cards")
    return engine, identities


def test_default_twenty_next_page_and_exact_jump_keep_complete_money_and_card_dates(cards):
    engine, identities = cards
    dashboard = Dashboard(engine)
    page_work, first = measure_work(engine, lambda: dashboard.assets("2026-03"))
    full_work, complete = measure_work(
        engine, lambda: dashboard.assets("2026-03", limit=50)
    )
    first_collection = first["data"]["collections"]["assets"]
    complete_items = complete["data"]["collections"]["assets"]["items"]
    assert len(first_collection["items"]) == 20
    assert first_collection["page"]["total_count"] == 31
    assert first_collection["items"] == complete_items[:20]
    next_page = dashboard.assets(
        "2026-03", section="assets", expected_version=first["snapshot_version"],
        cursor=first_collection["page"]["next_cursor"],
    )["data"]["collections"]["assets"]
    assert next_page["items"] == complete_items[20:]
    assert not next_page["page"]["has_more"]
    exact = dashboard.assets("2026-03", asset_id=identities[-1])["data"]
    assert exact["collections"]["assets"]["items"] == [complete_items[-1]]
    projects = dashboard.assets("2026-03", section="projects")["data"]
    with dashboard._snapshot("2026-03") as snap:
        summary = _assets(snap, summary_only=True)
    for data in (first["data"], complete["data"], exact, projects, summary):
        assert data["ledger_cost_fen"] == 3725580
        assert data["ledger_accumulated_fen"] == data["month_charge_fen"] == 310465
        assert data["ledger_net_fen"] == 3415115
        assert data["active_count"] == 31
    assert all("latest_charge_period" not in item and "charge_state_label" not in item
               for item in complete_items)
    assert all(item["month_charge_fen"] == 10000 + index
               for index, item in enumerate(complete_items))
    # The complete batch money/identity scope is necessary in both requests.
    # The additional cards must cost additional real detail transfers/decoding,
    # rather than being built eagerly and sliced only in the response.
    assert page_work["counters"]["returned_value_bytes"] < (
        full_work["counters"]["returned_value_bytes"]
    )
    assert page_work["counters"]["typed_fact_json_decodes"] < (
        full_work["counters"]["typed_fact_json_decodes"]
    )
