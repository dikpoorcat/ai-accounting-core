"""Brief counts interpret activation identities, with complete owner authority."""

import json
from unittest.mock import patch

import pytest
from stage9_metrics import measure_work
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import month
from test_asset_owner_kind_sources import change_owner_kind
from test_integrity_content import damage
from test_owner_asset_activation_identity import closed_cards
from test_payroll_corrections import Company

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import (
    Dashboard,
    _active_asset_counts,
    _long_term_assets,
    _Snapshot,
)
from ai_accounting.kernel.domains.transactions import Expense
from ai_accounting.kernel.query_reads import QueryReads


def summary(engine, period):
    with Dashboard(engine)._snapshot(period) as snap:
        return _long_term_assets(snap)


def test_brief_asset_summary_loads_fewer_bodies_and_keeps_growing_frozen_authority(tmp_path):
    company, ids = closed_cards(tmp_path, 6)
    engine = company.engine
    with patch.object(BusinessQueries, "_selected_asset_activation_identities", return_value=None):
        before_work, before = measure_work(engine, lambda: summary(engine, "2026-04"))
    after_work, after = measure_work(engine, lambda: summary(engine, "2026-04"))
    assert after == before == {
        "net_fen": sum(100000 + index * 10 for index in range(6)),
        "fixed_active_count": len(ids), "intangible_active_count": 0,
    }
    assert after_work["counters"]["calculation_result_rows_loaded"] < (
        before_work["counters"]["calculation_result_rows_loaded"]
    )
    company.close("2026-04")
    month(engine, company.owner_confirmation, "2026-05", "brief-consume-may")
    company.close("2026-05")
    month(engine, company.owner_confirmation, "2026-06", "brief-consume-june")
    grown_work, grown = measure_work(engine, lambda: summary(engine, "2026-06"))
    assert grown == {
        "net_fen": sum(80000 + index * 8 for index in range(6)),
        "fixed_active_count": len(ids), "intangible_active_count": 0,
    }
    assert grown_work["counters"]["calculation_result_rows_loaded"] <= (
        after_work["counters"]["calculation_result_rows_loaded"] + 1
    )
    assert grown_work["counters"]["typed_fact_json_decodes"] >= (
        after_work["counters"]["typed_fact_json_decodes"]
    )
    for index in range(8):
        company.save(Expense(
            period="2026-06", counterparty_id="supplier", amount_fen=index + 1,
            expense_class="administration", creditor_kind="supplier",
        ), f"brief-unrelated-{index}")
    company.publish(*(f"brief-unrelated-{index}" for index in range(8)))
    unrelated_work, unrelated = measure_work(engine, lambda: summary(engine, "2026-06"))
    assert unrelated == grown
    for field in ("calculation_result_rows_loaded", "typed_fact_json_decodes"):
        assert unrelated_work["counters"][field] == grown_work["counters"][field]
    print(json.dumps({"before": before_work["counters"], "after": after_work["counters"],
                      "grown": grown_work["counters"], "unrelated": unrelated_work["counters"]}))


@pytest.mark.parametrize("broken", ["consumption_kind", "consumption_source", "activation_member"])
def test_brief_count_scope_keeps_cross_kind_and_member_source_checks(tmp_path, broken):
    company, _ = closed_cards(tmp_path, 2)
    engine = company.engine
    assert summary(engine, "2026-04")["fixed_active_count"] == 2
    if broken == "consumption_kind":
        change_owner_kind(engine, "asset_consumption_month", "asset_activation_batch")
    elif broken == "consumption_source":
        with engine.store.connection(read_only=True) as connection:
            fact_id = connection.execute(
                "SELECT fact_id FROM calculation WHERE kind='asset_consumption_month' "
                "ORDER BY period LIMIT 1",
            ).fetchone()[0]
        damage(engine, "fact_revision", "DELETE FROM fact_revision WHERE id=?",
               (fact_id,), foreign_keys=False)
    else:
        damage(engine, "asset_batch_member", "DELETE FROM asset_batch_member "
               "WHERE member_subject_id='activate-card-01'")
    for _ in range(2):
        with pytest.raises(KernelError):
            summary(engine, "2026-04")


@pytest.mark.parametrize("reader", ["unowned", "fixed_v1"])
def test_brief_asset_summary_keeps_original_full_reader_fallback(tmp_path, reader):
    company, _ = closed_cards(tmp_path, 2)
    engine = company.engine
    expected = summary(engine, "2026-04")
    if reader == "fixed_v1":
        with historical_content(1):
            assert summary(engine, "2026-04") == expected
    else:
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            snap = _Snapshot(engine, connection, "2026-04", reads=QueryReads(engine, connection))
            try:
                assert _long_term_assets(snap) == expected
            finally:
                snap.release()


def test_open_consumption_owners_keep_typed_proof_without_count_body_expansion(tmp_path):
    company = Company(tmp_path / "brief-open-count.sqlite", opening_period="2026-02")
    prepare_batch_assets(company)
    engine = company.engine
    company.save(Expense(
        period="2026-05", counterparty_id="supplier", amount_fen=999,
        expense_class="administration", creditor_kind="supplier",
    ), "brief-may-source")
    before_work, before = measure_work(engine, lambda: summary(engine, "2026-05"))
    for period in ("2026-03", "2026-04", "2026-05"):
        month(engine, company.owner_confirmation, period, "brief-open-" + period)
    after_work, after = measure_work(engine, lambda: summary(engine, "2026-05"))
    assert before["fixed_active_count"] == after["fixed_active_count"] == 2
    # The financial amount consumer still needs current monetary results;
    # additional historical count-only owner bodies are not loaded.
    assert after_work["counters"]["typed_fact_json_decodes"] > (
        before_work["counters"]["typed_fact_json_decodes"]
    )
    print(json.dumps({"open_before": before_work["counters"],
                      "open_after": after_work["counters"]}))


def test_count_scope_does_not_replace_full_owner_consumer_in_same_snapshot(tmp_path):
    company = Company(tmp_path / "brief-scope-reuse.sqlite", opening_period="2026-02")
    prepare_batch_assets(company)
    engine = company.engine
    month(engine, company.owner_confirmation, "2026-03", "brief-scope-march")
    with Dashboard(engine)._snapshot("2026-03") as snap:
        assert _active_asset_counts(snap) == {"fixed": 2}
        # The full consumer follows the narrow consumer in the same snapshot.
        # It must still interpret its complete open consumption owner body.
        full = snap.queries._selected_asset_activation_identities(
            snap.connection, snap.period,
        )
        # The narrow success cannot supply the default complete decoded owner
        # set: the latter includes the independently named consumption owner.
        with engine.store.connection(read_only=True) as connection:
            consumption = connection.execute(
                "SELECT id FROM calculation WHERE kind='asset_consumption_month'",
            ).fetchone()[0]
        full_selection = snap.reads._report_snapshot_cache[
            ("asset_owner_identity_selection", snap.month)
        ]
        assert consumption in full_selection[3]
        assert len(full) == 2
        assert _active_asset_counts(snap) == {"fixed": 2}
        assert _long_term_assets(snap)["fixed_active_count"] == 2
    with Dashboard(engine)._snapshot("2026-03") as fresh:
        assert fresh.queries._selected_asset_activation_identities(
            fresh.connection, fresh.period,
        ) == full


def test_count_reuses_exact_scoped_frozen_directory_without_full_body_cache(tmp_path):
    company, _ = closed_cards(tmp_path, 6)
    engine = company.engine
    original = BusinessQueries._selected_asset_activation_identities

    def without_identity_reuse(queries, connection, period, **options):
        result = original(queries, connection, period, **options)
        cache = queries._reads(connection)._report_snapshot_cache
        for key in list(cache):
            if isinstance(key, tuple) and key[0] in {
                "asset_owner_identity_selection", "asset_activation_identity_events",
            }:
                cache.pop(key)
        return result

    with patch.object(BusinessQueries, "_selected_asset_activation_identities",
                      without_identity_reuse):
        before_work, before = measure_work(engine, lambda: summary(engine, "2026-04"))
    after_work, after = measure_work(engine, lambda: summary(engine, "2026-04"))
    assert after == before
    for field in ("calculation_result_rows_loaded", "calculation_result_bytes_loaded",
                  "calculation_result_json_decodes"):
        assert after_work["counters"][field] < before_work["counters"][field]
    with Dashboard(engine)._snapshot("2026-04") as snap:
        assert _active_asset_counts(snap) == {"fixed": 6}
        assert ("asset_owner_identity_selection", snap.month) not in (
            snap.reads._report_snapshot_cache
        )
    print(json.dumps({"repeat": before_work["counters"], "exact_reuse": after_work["counters"]}))
