"""Carrying roots replace unused historical charges while fallback stays exact."""

import json

import pytest
from stage9_metrics import measure_work
from test_asset_batches import month
from test_dashboard_model_transactions import raw_read
from test_integrity_content import damage
from test_owner_asset_activation_identity import closed_cards
from test_reimbursement_assets import consume_assets

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains.assets import AssetDisposal
from ai_accounting.kernel.types import YearMonth


def _items(response):
    return {item["asset_id"]: item
            for item in response["data"]["collections"]["assets"]["items"]}


def _dispose_second(company, ids):
    company.save(AssetDisposal(
        period="2026-04", asset_id=ids[1], disposal_date="2026-04-30",
        disposal_kind="scrap", gross_proceeds_fen=0,
    ), "dispose-second")
    consume_assets(company.engine, "2026-04", (company.owner_confirmation,),
                   expected_revision=1, request_id="consume-after-disposal")


def test_complete_carrying_amounts_do_not_load_growing_closed_charge_bodies(tmp_path):
    company, ids = closed_cards(tmp_path, 2)
    view = Dashboard(company.engine)
    with raw_read():
        before_work, before = measure_work(company.engine, lambda: view.assets("2026-04"))
    assert before["data"]["ledger_net_fen"] == 200010
    for index, ident in enumerate(ids):
        assert _items(before)[ident]["accumulated_charge_fen"] == 20000 + index * 2
        assert _items(before)[ident]["month_charge_fen"] == 10000 + index
    company.close("2026-04")
    month(company.engine, company.owner_confirmation, "2026-05", "consume-may")
    company.close("2026-05")
    month(company.engine, company.owner_confirmation, "2026-06", "consume-june")
    with raw_read():
        after_work, after = measure_work(company.engine, lambda: view.assets("2026-06"))
    assert after["data"]["ledger_net_fen"] == 160008
    assert after["data"]["active_count"] == 2
    for index, ident in enumerate(ids):
        assert _items(after)[ident]["accumulated_charge_fen"] == 40000 + index * 4
        assert _items(after)[ident]["month_charge_fen"] == 10000 + index
    # Historical scalar/adoption discovery may grow. Full archived charge
    # bodies are unnecessary for these authenticated current carrying roots.
    assert after_work["counters"]["calculation_result_rows_loaded"] <= (
        before_work["counters"]["calculation_result_rows_loaded"] + 2
    )
    print(json.dumps({"before": before_work["counters"], "after": after_work["counters"]}))


def test_disposed_fallback_and_current_card_keep_independent_complete_amounts(tmp_path):
    company, ids = closed_cards(tmp_path, 2)
    view = Dashboard(company.engine)
    with raw_read():
        fast_work, _ = measure_work(company.engine, lambda: view.assets("2026-04"))
    _dispose_second(company, ids)
    with raw_read():
        fallback_work, response = measure_work(company.engine, lambda: view.assets("2026-04"))
    items = _items(response)
    assert items[ids[0]]["accumulated_charge_fen"] == 20000
    assert items[ids[0]]["month_charge_fen"] == 10000
    assert items[ids[0]]["book_value_fen"] == 100000
    assert items[ids[1]]["status"] == "disposed"
    assert items[ids[1]]["accumulated_charge_fen"] == 20002
    assert items[ids[1]]["month_charge_fen"] == 10001
    assert items[ids[1]]["book_value_fen"] == 0
    assert response["data"]["active_count"] == 1
    assert response["data"]["ledger_net_fen"] == 100000
    assert fallback_work["counters"]["calculation_result_rows_loaded"] > (
        fast_work["counters"]["calculation_result_rows_loaded"]
    )
    print(json.dumps({"fast": fast_work["counters"], "mixed": fallback_work["counters"]}))


@pytest.mark.parametrize("mixed", [False, True])
def test_required_source_damage_is_not_hidden_by_card_or_fallback_scope(tmp_path, mixed):
    company, ids = closed_cards(tmp_path, 2)
    view = Dashboard(company.engine)
    if mixed:
        _dispose_second(company, ids)
    # In a mixed scope, the old owner must be read for the disposed sibling.
    # Its whole source remains checked even when damage targets the fast card.
    period = "2026-03" if mixed else "2026-04"
    with company.engine.store.connection(read_only=True) as connection:
        if mixed:
            fact_id = connection.execute(
                "SELECT f.revision_id FROM fact_asset_consumption f "
                "JOIN calculation c ON c.fact_id=f.revision_id "
                "WHERE f.asset_id=? AND c.period=? LIMIT 1",
                (ids[0], YearMonth(period).ordinal),
            ).fetchone()[0]
        else:
            fact_id = connection.execute(
                "SELECT fact_id FROM calculation WHERE subject_id=? LIMIT 1",
                ("asset-consumption-month:" + period,),
            ).fetchone()[0]
    if mixed:
        damage(company.engine, "fact_asset_consumption",
               "UPDATE fact_asset_consumption SET asset_id=? WHERE revision_id=?",
               (ids[1], fact_id))
    else:
        damage(company.engine, "fact_revision", "DELETE FROM fact_revision WHERE id=?",
               (fact_id,), foreign_keys=False)
    # Exercise the native carrying/fallback source consumer, not a cached card.
    with raw_read(), pytest.raises(KernelError) as failure:
        view.assets("2026-04", asset_id=ids[0])
    assert failure.value.code == "content_integrity_failed"
