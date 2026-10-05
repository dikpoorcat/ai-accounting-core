"""Required asset identities survive candidate damage and legitimate withdrawal."""

import json
from contextlib import contextmanager

import pytest
from test_asset_batches import activate
from test_asset_batches import asset_engine as asset_engine_fixture
from test_integrity_content import damage
from test_opening_continuation import _close_without_current_business
from test_opening_continuation import book as _opening_book
from test_payroll_corrections import Company
from test_reimbursement_assets import asset

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains.assets import ReimbursedAsset
from ai_accounting.kernel.types import YearMonth, canonical, digest

opening_book = _opening_book
asset_engine = asset_engine_fixture


def _warm_unrelated_close(snap):
    closes = snap.reads.authoritative_close_rows(periods=[YearMonth("2026-01").ordinal])
    snap.reads.close_accounting_many(
        closes, subjects={"unrelated-absent"},
        subjects_by_period={row["period"]: set() for row in closes},
    )


def _read_page(dashboard, period="2026-02"):
    try:
        result = dashboard.assets(period, preparation="deferred")["data"]
        return {"count": result["collections"]["assets"]["page"]["total_count"]}
    except KernelError as error:
        return {"error": error.code, "reason": error.details.get("reason")}


@pytest.mark.parametrize("missing", ["publication", "calculation"])
def test_frozen_zero_net_card_missing_source_default_and_core_fail_cold_and_warm(
    opening_book, monkeypatch, missing,
):
    engine, _save, _publish, package, proof = opening_book
    package([("opening_asset", "machine", {
        "asset_id": "machine", "asset_type": "fixed", "cost_fen": 1200,
        "accumulated_fen": 1200, "in_use_date": "2024-12-15", "useful_life_months": 12,
        "completed_months": 12, "residual_fen": 0, "benefit_area": "administration",
        "rounding_policy": "floor_final_remainder",
    })])
    _close_without_current_business(engine, "2026-01", proof)
    _close_without_current_business(engine, "2026-02", proof)
    dashboard = Dashboard(engine)
    before = dashboard.assets("2026-02", preparation="deferred")["data"]
    card = before["collections"]["assets"]["items"][0]
    assert card["asset_id"] == "machine" and card["book_value_fen"] == 0
    assert before["collections"]["assets"]["page"]["total_count"] == 1
    table = "calculation_publication" if missing == "publication" else "calculation"
    damage(engine, table, f"DELETE FROM {table} WHERE subject_id='machine'", foreign_keys=False)
    outcomes = {}
    original_snapshot = dashboard._snapshot
    for warm in (False, True):
        if warm:
            @contextmanager
            def warmed_snapshot(period):
                with original_snapshot(period) as snap:
                    _warm_unrelated_close(snap)
                    yield snap

            monkeypatch.setattr(dashboard, "_snapshot", warmed_snapshot)
        outcomes[f"default_{warm}"] = _read_page(dashboard)
        with original_snapshot("2026-02") as snap:
            if warm:
                _warm_unrelated_close(snap)
            try:
                snap.queries._selected_accounting(
                    snap.connection, {"machine"}, snap.period, include_vouchers=False,
                )
                outcomes[f"core_{warm}"] = {"accepted": True}
            except KernelError as error:
                outcomes[f"core_{warm}"] = {
                    "error": error.code, "reason": error.details.get("reason"),
                }
    print(json.dumps({"missing_source": missing, "outcomes": outcomes}, ensure_ascii=False))
    assert all(value.get("error") == "content_integrity_failed" for value in outcomes.values())


@pytest.mark.parametrize("warm", [False, True])
def test_current_zero_net_card_without_frozen_anchor_missing_subject_is_not_absence(
    opening_book, monkeypatch, warm,
):
    engine, _save, _publish, package, _proof = opening_book
    package([("opening_asset", "machine", {
        "asset_id": "machine", "asset_type": "fixed", "cost_fen": 1200,
        "accumulated_fen": 1200, "in_use_date": "2024-12-15", "useful_life_months": 12,
        "completed_months": 12, "residual_fen": 0, "benefit_area": "administration",
        "rounding_policy": "floor_final_remainder",
    })])
    dashboard = Dashboard(engine)
    before = dashboard.assets("2026-01", preparation="deferred")["data"]
    cards = before["collections"]["assets"]["items"]
    assert len(cards) == 1 and cards[0]["asset_id"] == "machine"
    assert cards[0]["book_value_fen"] == 0
    damage(engine, "subject", "DELETE FROM subject WHERE id='machine'", foreign_keys=False)
    if warm:
        original_snapshot = dashboard._snapshot

        @contextmanager
        def warmed_snapshot(period):
            with original_snapshot(period) as snap:
                snap.month_journal.sql()
                yield snap

        monkeypatch.setattr(dashboard, "_snapshot", warmed_snapshot)
    actual = _read_page(dashboard, "2026-01")
    print(json.dumps({"missing_subject": True, "warm": warm, "outcome": actual}))
    assert actual.get("error") == "content_integrity_failed"


def test_legitimate_asset_withdrawal_then_close_matches_empty_core(tmp_path):
    company = Company(tmp_path / "withdrawn-asset.sqlite")
    company.save(ReimbursedAsset.model_validate_json(json.dumps(asset(period="2026-01"))), "card")
    company.publish("card")
    proof = company.owner_confirmation
    preview = company.engine.preview_delete("card", recording_error_evidence=proof)
    company.engine.delete(
        "card", preview_digest=preview["digest"], epochs=preview["epochs"],
        recording_error_evidence=proof, request_id=company.request(),
    )
    company.close("2026-01")
    company.close("2026-02")
    dashboard = Dashboard(company.engine)
    with dashboard._snapshot("2026-02") as snap:
        core = snap.queries._selected_accounting(
            snap.connection, {"card"}, snap.period,
        )["through_period"]
        assert core["voucher_events"] == core["state_results"] == []
    actual = _read_page(dashboard)
    print(json.dumps({"legitimate_withdrawal": actual}, ensure_ascii=False))
    assert actual == {"count": 0}


def test_unpublished_activation_members_keep_the_exact_owner_lane(asset_engine):
    engine, evidence = asset_engine
    activate(engine, evidence)
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute(
            "SELECT count(*) FROM calculation c JOIN calculation_current a "
            "ON a.calculation_id=c.id LEFT JOIN calculation_publication p "
            "ON p.calculation_id=c.id WHERE c.kind='asset_activation' AND p.id IS NULL"
        ).fetchone()[0] == 2
    actual = Dashboard(engine).assets("2026-01", preparation="deferred")["data"]
    cards = actual["collections"]["assets"]["items"]
    assert {card["asset_id"] for card in cards} == {"fixed-a", "intangible-b"}
    assert all(card["status"] == "active" for card in cards)


def test_real_future_publication_keeps_its_unused_body_outside_old_card_scope(opening_book):
    engine, save, publish, package, proof = opening_book
    package([("opening_asset", "machine", {
        "asset_id": "machine", "asset_type": "fixed", "cost_fen": 1200,
        "accumulated_fen": 1200, "in_use_date": "2024-12-15", "useful_life_months": 12,
        "completed_months": 12, "residual_fen": 0, "benefit_area": "administration",
        "rounding_policy": "floor_final_remainder",
    })])
    _close_without_current_business(engine, "2026-01", proof)
    save("reimbursed_asset", "future", asset(period="2026-02"))
    publish("future")
    with engine.store.connection(read_only=True) as connection:
        saved = connection.execute(
            "SELECT id,outcome FROM calculation WHERE subject_id='future'"
        ).fetchone()
    body = json.loads(saved["outcome"])
    body["synthetic_changed_result"] = True
    damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
           (canonical(body), digest(body), saved["id"]))
    actual = Dashboard(engine).assets("2026-01", preparation="deferred")["data"]
    cards = actual["collections"]["assets"]["items"]
    assert len(cards) == 1 and cards[0]["asset_id"] == "machine"
    assert cards[0]["book_value_fen"] == 0
