"""Asset cards retain exact payment scope while dropping payment drilldowns."""

import pytest
from test_reimbursement_assets import asset
from test_reimbursement_assets import book as book_fixture

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _asset_payment_summary, _Snapshot

book = book_fixture


def obligation(key, subject, amount, paid=0, other=0, remaining=None):
    return {
        "key": key, "source_business": {"subject_id": subject},
        "source_amount_fen": amount, "paid_fen": paid, "other_settled_fen": other,
        "remaining_fen": amount - paid - other if remaining is None else remaining,
    }


def test_card_summary_deduplicates_sources_and_keeps_large_integer_amounts():
    amount = 10**18 + 1
    rows = [obligation("a", "a", amount, 3, 4), obligation("b", "b", 9, 1, 2),
            obligation("unrelated", "other", 100)]
    shared = {"issues": ["AI 会计核对中"], "obligations": rows}
    result = _asset_payment_summary(shared, [{"subject_id": "a"}, {"subject_id": "b"},
                                          {"subject_id": "a"}])
    assert result == {
        "obligation_count": 2, "checking": True, "amount_fen": amount + 9,
        "paid_fen": 4, "other_settled_fen": 6, "remaining_fen": amount - 1,
    }


@pytest.mark.parametrize("field", ["source_amount_fen", "paid_fen", "other_settled_fen",
                                  "remaining_fen"])
def test_card_summary_preserves_unknown_amounts(field):
    row = obligation("a", "a", 100)
    row[field] = None
    result = _asset_payment_summary({"issues": [], "obligations": [row]}, [{"subject_id": "a"}])
    assert result["obligation_count"] == 1
    assert all(result[field] is None for field in (
        "amount_fen", "paid_fen", "other_settled_fen", "remaining_fen"))


def test_no_confirmed_obligation_keeps_checking_state():
    result = _asset_payment_summary({"issues": ["unresolved"], "obligations": []},
                                   [{"subject_id": "a"}])
    assert result == {"obligation_count": 0, "checking": True, "amount_fen": 0,
                      "paid_fen": 0, "other_settled_fen": 0, "remaining_fen": 0}


def test_asset_dashboard_rejects_removed_payment_collection_and_keeps_ai_read(book, monkeypatch):
    engine, save, publish = book
    save("reimbursed_asset", "computer", asset())
    publish("computer")
    reads = []
    original = _Snapshot.settlement_summary

    def watched(snapshot, **kwargs):
        result = original(snapshot, **kwargs)
        reads.append((kwargs, set(snapshot.settlement_summaries),
                      hasattr(snapshot, "people_asset_settlements")))
        return result

    monkeypatch.setattr(_Snapshot, "settlement_summary", watched)
    dashboard = Dashboard(engine)
    response = dashboard.assets("2026-02")
    assert response["schema_version"] == 10
    item = response["data"]["collections"]["assets"]["items"][0]
    assert "settlements" not in item
    assert item["payment_summary"]["amount_fen"] == 120000
    assert len(reads) == 1
    options, cache_keys, people_cache_created = reads[0]
    assert options == {"subject_ids": {"computer"}, "include_history_counts": False}
    assert not people_cache_created
    assert cache_keys == {(False, frozenset({"computer"}), False)}
    with pytest.raises(KernelError) as failure:
        dashboard.assets("2026-02", section="settlement_events", asset_id="computer")
    assert failure.value.code == "invalid_command"
    diagnostic = dashboard.business_status("2026-02", "computer")["data"]
    assert diagnostic["settlements"]["obligations"]
