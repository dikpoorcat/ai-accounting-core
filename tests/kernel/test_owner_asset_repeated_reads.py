"""Reuse successful discovery and page profiles without widening their proof scope."""

from contextlib import nullcontext

import pytest
from close_storage_fixture import replace_stored_manifest, stored_manifest
from stage9_metrics import measure_work
from test_dashboard_metadata import profile
from test_integrity_content import damage
from test_owner_asset_card_page_scope import cards as cards_fixture
from test_owner_asset_metadata_scope import closed_consumption
from test_owner_asset_metadata_scope import metadata_company as metadata_company_fixture
from test_reimbursement_assets import book as book_fixture

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.dashboard_metadata import DashboardMetadata
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth

book, cards, metadata_company = book_fixture, cards_fixture, metadata_company_fixture


def discovery(reads, period):
    return BusinessQueries(reads.engine, reads=reads)._frozen_asset_owner_sources(
        reads.connection, YearMonth(period),
    )


def test_native_discovery_reuses_exact_cutoff_without_sql_or_body_proof(metadata_company):
    company, evidence = metadata_company
    closed_consumption(company, evidence)

    def read():
        with QueryReads.snapshot(company.engine) as reads:
            statements = []
            reads.connection.set_trace_callback(statements.append)
            first = discovery(reads, "2026-02")
            assert statements
            statements.clear()
            assert discovery(reads, "2026-02") is first
            assert statements == []
            later = discovery(reads, "2026-03")
            assert statements and later is not first
            assert set(first[1]) < set(later[1])
            statements.clear()
            assert discovery(reads, "2026-03") is later
            assert statements == []
            assert not reads._asset_members
            assert not reads._verified_sql_outcomes
            assert not reads._verified_source_contents
            retained = reads
        # Exit invalidates the discovery token; a later snapshot rereads sources.
        assert retained._frozen_asset_owner_discovery is None
        with QueryReads.snapshot(company.engine) as reads:
            statements = []
            reads.connection.set_trace_callback(statements.append)
            assert discovery(reads, "2026-03")[2] == later[2]
            assert statements

    work, _ = measure_work(company.engine, read)
    assert work["counters"]["calculation_result_rows_loaded"] == 0
    assert work["counters"]["returned_rows"] > 0


@pytest.mark.parametrize("reader", ["unowned", "fixed_v1"])
def test_unowned_and_fixed_reader_discovery_never_reuse_native_success(metadata_company, reader):
    company, evidence = metadata_company
    closed_consumption(company, evidence)
    engine = company.engine
    scope = QueryReads.snapshot(engine) if reader == "fixed_v1" else (
        engine.store.connection(read_only=True)
    )
    with scope as owned:
        if reader == "fixed_v1":
            reads = owned
            native = discovery(reads, "2026-03")
        else:
            owned.execute("BEGIN")
            reads = QueryReads(engine, owned)
        with historical_content(1) if reader == "fixed_v1" else nullcontext():
            statements = []
            reads.connection.set_trace_callback(statements.append)
            first = discovery(reads, "2026-03")
            statements.clear()
            second = discovery(reads, "2026-03")
            assert first is not second
            assert statements
            if reader == "fixed_v1":
                assert first is not native
            else:
                assert getattr(reads, "_frozen_asset_owner_discovery", None) is None


@pytest.mark.parametrize("bad", ["declaration_tail", "owner_fact"])
@pytest.mark.parametrize("warm", [False, True])
def test_failed_discovery_preserves_only_prior_success_and_retries_actual_sources(
    metadata_company, bad, warm,
):
    company, evidence = metadata_company
    closed_consumption(company, evidence)
    engine = company.engine
    if bad == "declaration_tail":
        with engine.store.connection(read_only=True) as connection:
            manifest = stored_manifest(connection, "2026-03")
        manifest["asset_batch_adoptions"][-1]["membership_digest"] = "g" * 64
        replace_stored_manifest(engine, manifest)
    else:
        with engine.store.connection(read_only=True) as connection:
            fact_id = connection.execute(
                "SELECT fact_id FROM calculation WHERE kind='asset_consumption_month'"
            ).fetchone()[0]
        damage(engine, "fact_revision", "DELETE FROM fact_revision WHERE id=?", (fact_id,),
               foreign_keys=False)
    with QueryReads.snapshot(engine) as reads:
        prior = discovery(reads, "2026-02") if warm else None
        for _ in range(2):
            statements = []
            reads.connection.set_trace_callback(statements.append)
            with pytest.raises(KernelError):
                discovery(reads, "2026-03")
            assert statements
            if warm:
                assert reads._frozen_asset_owner_discovery[1] == {
                    YearMonth("2026-02").ordinal: prior,
                }
            else:
                assert reads._frozen_asset_owner_discovery is None
            assert not reads._asset_members
            assert not reads._verified_sql_outcomes
            assert not reads._verified_source_contents


def test_asset_page_profiles_are_batched_after_twenty_next_and_exact_selection(cards, monkeypatch):
    engine, identities = cards
    selected_profiles = []
    original = DashboardMetadata.prime_profiles

    def prime(metadata, kind, identifiers):
        if kind == "asset":
            selected_profiles.append(set(identifiers))
        return original(metadata, kind, identifiers)

    monkeypatch.setattr(DashboardMetadata, "prime_profiles", prime)
    for index, ident in enumerate(identities):
        profile(engine, "asset", ident, display_name=f"明确资产 {index}")
    dashboard = Dashboard(engine)
    work, first = measure_work(engine, lambda: dashboard.assets("2026-03"))
    first_page = first["data"]["collections"]["assets"]
    assert selected_profiles[-1] == set(identities[:20])
    assert len(first_page["items"]) == 20
    assert [item["name"] for item in first_page["items"]] == [
        f"明确资产 {index}" for index in range(20)
    ]
    assert first_page["page"]["total_count"] == 31
    profile_sql = [item for item in work["sql"] if (
        item["statement"].startswith("SELECT p.* FROM entity_profile_revision")
    )]
    # The twenty card rows use one batch. A creditor without a display name
    # keeps the existing employee/counterparty fallback, at most two more reads.
    # The work recorder groups equal SQL text across asset and party scopes.
    assert sum(item["calls"] for item in profile_sql) <= 3
    assert 20 <= sum(item["returned_rows"] for item in profile_sql) <= 22
    next_page = dashboard.assets(
        "2026-03", section="assets", expected_version=first["snapshot_version"],
        cursor=first_page["page"]["next_cursor"],
    )["data"]
    assert selected_profiles[-1] == set(identities[20:])
    assert [item["asset_id"] for item in next_page["collections"]["assets"]["items"]] == (
        identities[20:]
    )
    exact = dashboard.assets("2026-03", asset_id=identities[-1])["data"]
    assert selected_profiles[-1] == {identities[-1]}
    assert exact["collections"]["assets"]["items"] == [
        next_page["collections"]["assets"]["items"][-1]
    ]
    projects = dashboard.assets("2026-03", section="projects")["data"]
    assert selected_profiles[-1] == set()
    for data in (first["data"], next_page, exact, projects):
        assert data["ledger_cost_fen"] == 3725580
        assert data["ledger_accumulated_fen"] == data["month_charge_fen"] == 310465
        assert data["ledger_net_fen"] == 3415115
        assert data["active_count"] == 31


def test_closed_page_profiles_preserve_frozen_names_and_fill_only_missing_names(
    metadata_company, monkeypatch,
):
    company, _ = metadata_company
    engine = company.engine
    profile(engine, "asset", "computer", display_name="关账时电脑", note="")
    company.close("2026-02")
    profile(engine, "asset", "computer", revision=1, display_name="后来改名电脑", note="后补说明")
    profile(engine, "asset", "chair", display_name="后来补齐椅子")
    with Dashboard(engine)._snapshot("2026-02") as snap:
        snap.metadata.prime_profiles("asset", {"computer", "chair"})
        computer, chair = snap.profile("asset", "computer"), snap.profile("asset", "chair")
        assert computer["display_name"] == "关账时电脑"
        assert computer["field_sources"]["display_name"]["basis"] == "frozen"
        assert computer["note"] == "后补说明"
        assert computer["field_sources"]["note"]["basis"] == "current_supplement"
        assert chair["display_name"] == "后来补齐椅子"
        assert chair["field_sources"]["display_name"]["basis"] == "current_supplement"
    with engine.store.connection(read_only=True) as connection:
        owner_id = connection.execute(
            "SELECT id FROM calculation WHERE kind='asset_activation_batch'"
        ).fetchone()[0]
    header_scopes = []
    original = QueryReads.calculation_identity_headers

    def headers(reads, identifiers):
        identifiers = set(identifiers)
        header_scopes.append(identifiers)
        return original(reads, identifiers)

    monkeypatch.setattr(QueryReads, "calculation_identity_headers", headers)
    data = Dashboard(engine).assets("2026-02", preparation="deferred")["data"]
    # The actual page calls full activation selection and card metadata;
    # discovery authenticates their common frozen owner exactly once.
    assert sum(owner_id in scope for scope in header_scopes) == 1
    assert {item["asset_id"]: item["name"] for item in data["collections"]["assets"]["items"]} == {
        "computer": "关账时电脑", "chair": "后来补齐椅子",
    }
