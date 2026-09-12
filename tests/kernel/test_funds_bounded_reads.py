"""Page keys precede fact expansion; bank child rows never hydrate the parent fact."""

import pytest
from test_banking import book as _bank_book
from test_banking import entry, funding, opening, statement
from test_dashboard_funds_alignment import _publish_filter_funding

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_funds import funds

bank_book = _bank_book


@pytest.mark.parametrize("count", [3, 503])
def test_statement_page_and_summary_do_not_decode_complete_parent(bank_book, monkeypatch, count):
    engine, save, publish, _ = bank_book
    opening(save, publish)
    funding(save, publish)
    statement(save, publish, [entry(f"row-{index}", amount=1) for index in range(count)])
    loaded = set()
    original = engine.store.facts

    def watched(connection, identifiers):
        loaded.update(identifiers)
        result = original(connection, identifiers)
        assert all(item.fact.kind != "bank_statement" for item in result.values())
        return result

    monkeypatch.setattr(engine.store, "facts", watched)
    with Dashboard(engine)._snapshot("2026-09") as snap:
        data = funds(snap, sections={"statements"}, limit=2)
        assert len(data["bank_statement"]["rows"]) == 2
        assert data["bank_statement"]["transaction_count"] == count
        assert data["collections"]["statements"]["page"]["filtered_count"] == count
        assert data["bank_statement"]["unmatched_totals"] == {
            "count": count,
            "inflow_fen": count,
            "outflow_fen": 0,
        }
        assert loaded == set()
    with Dashboard(engine)._snapshot("2026-09") as snap:
        summary = funds(snap, summary_only=True)
        assert summary["bank_statement"]["rows"] == summary["movements"] == []
        assert (
            summary["bank_statement"]["unmatched_totals"]
            == data["bank_statement"]["unmatched_totals"]
        )
        assert loaded == set()


def test_movement_expansion_is_limited_after_filtering(bank_book, monkeypatch):
    engine, _, _, _ = bank_book
    _publish_filter_funding(engine, ["bank-a"] * 37 + ["bank-b"] * 4)
    loaded = set()
    original = engine.store.facts

    def watched(connection, identifiers):
        loaded.update(identifiers)
        return original(connection, identifiers)

    monkeypatch.setattr(engine.store, "facts", watched)
    with Dashboard(engine)._snapshot("2026-09") as snap:
        summary = funds(snap, summary_only=True)
        assert summary["inflow_fen"] == 41
        assert loaded == set()
    with Dashboard(engine)._snapshot("2026-09") as snap:
        page = funds(
            snap,
            sections={"movements"},
            limit=2,
            filters={"movement_account_type": "bank", "movement_account_id": "bank-b"},
        )
        assert page["inflow_fen"] == 41
        assert {row["account_id"] for row in page["movements"]} == {"bank-b"}
        assert len(loaded) == len(page["movements"]) == 2
        assert page["collections"]["movements"]["page"]["total_count"] == 41
        assert page["collections"]["movements"]["page"]["filtered_count"] == 4
