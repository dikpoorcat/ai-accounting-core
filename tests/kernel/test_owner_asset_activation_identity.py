"""Card identity uses complete frozen directories; amounts keep complete bodies."""

import json
from unittest.mock import patch

import pytest
from stage9_metrics import measure_work
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import month
from test_integrity_content import damage
from test_payroll_corrections import Company
from test_reimbursement_assets import activate_assets, activation, asset

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.content_history_context import historical_content
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard, _assets
from ai_accounting.kernel.domains.assets import ReimbursedAsset
from ai_accounting.kernel.entities import Entities
from ai_accounting.kernel.query_reads import selected_voucher_sql
from ai_accounting.kernel.types import YearMonth


def old_page(engine, period, *, owner_fact_ids=(), **options):
    def complete_body_lane(queries, connection, selected_period):
        if owner_fact_ids:
            queries._reads(connection).verify_fact_versions(owner_fact_ids)
        return None

    with patch.object(BusinessQueries, "_selected_asset_activation_identities",
                      autospec=True, side_effect=complete_body_lane):
        return Dashboard(engine).assets(period, preparation="deferred", **options)


def closed_cards(tmp_path, count):
    company = Company(tmp_path / "cards.sqlite")
    identities = [f"card-{index:02}" for index in range(count)]
    for index, ident in enumerate(identities):
        company.save(ReimbursedAsset.model_validate_json(json.dumps(asset(
            asset_id=ident, cost_fen=120000 + 12 * index,
            creditors=[{"employee_id": "alice", "amount_fen": 120000 + 12 * index}],
        ))), ident)
    company.publish(*identities)
    engine = company.engine
    with engine.store.connection(read_only=True) as connection:
        evidence = engine.store.current_fact(connection, identities[0]).evidence
    activate_assets(engine, "activate-cards", "2026-02", [
        ("activate-" + ident, activation(asset_id=ident)) for ident in identities
    ], evidence)
    company.close("2026-02")
    month(engine, evidence[0], "2026-03", "consume-march")
    company.close("2026-03")
    month(engine, evidence[0], "2026-04", "consume-april")
    return company, identities


@pytest.mark.parametrize("count", [12, 48])
def test_complete_card_response_and_work_with_object_and_profile_history_growth(tmp_path, count):
    company, identities = closed_cards(tmp_path, count)
    engine = company.engine
    view = Dashboard(engine)
    profiles = Entities(engine)
    # Exact same visible content: these are history growth, not renamed cards.
    with engine.store.connection(read_only=True) as connection:
        record = connection.execute(
            "SELECT revision,content FROM entity_profile_revision WHERE entity_id=? "
            "ORDER BY revision DESC LIMIT 1", (identities[0],),
        ).fetchone()
    for offset in range(3):
        profiles.update_entity_profile(
            identities[0], json.loads(record["content"]), source="same synthetic profile",
            expected_revision=record["revision"] + offset,
            request_id=f"same-profile-{offset}",
        )
    # Discover the control's exact sources outside either work measurement.
    # This fixture has one activation and two consumption owners, with no
    # replacements. Union the complete frozen declarations with all actual
    # current/voucher identities before checking that semantic owner universe;
    # neither a mutable kind filter nor a member index decides missing sources.
    expected_owner_periods = {
        "activate-cards": "2026-02",
        "asset-consumption-month:2026-03": "2026-03",
        "asset-consumption-month:2026-04": "2026-04",
    }
    with view._snapshot("2026-04") as snap:
        _, _, membership, frozen = snap.queries._frozen_asset_owner_sources(
            snap.connection, YearMonth(snap.period),
        )
        actual_ids = {row[0] for row in snap.connection.execute(
            "SELECT calculation_id FROM calculation_current",
        )}
        voucher_query, voucher_parameters = selected_voucher_sql(snap.period)
        actual_ids.update(row["basis_calculation_id"] for row in snap.connection.execute(
            voucher_query, voucher_parameters,
        ))
        # Members have no separate publication: inspect every actual identity,
        # then use the fixture's independent owner subjects for owner headers.
        actual_sources = {row["id"]: dict(row) for row in snap.connection.execute(
            "SELECT c.id,c.subject_id,c.kind FROM json_each(?) ids "
            "LEFT JOIN calculation c ON c.id=ids.value", (json.dumps(sorted(actual_ids)),),
        )}
        assert actual_sources.keys() == actual_ids
        owner_ids = set(membership) | {
            ident for ident, source in actual_sources.items()
            if source["subject_id"] in expected_owner_periods
        }
        assert {source["subject_id"] for source in actual_sources.values()
                if source["kind"] in {
                    "asset_activation_batch", "asset_consumption_month",
                }} == expected_owner_periods.keys()
        headers = frozen | snap.reads.calculation_identity_headers(owner_ids - membership.keys())
        owners = {ident: header for ident, header in headers.items()
                  if header["source_subject"] in expected_owner_periods}
        assert {header["source_subject"] for header in headers.values()
                if header["source_kind"] in {
                    "asset_activation_batch", "asset_consumption_month",
                }} == expected_owner_periods.keys()
        assert set(membership) <= owners.keys()
        assert len(owners) == len(expected_owner_periods)
        assert {header["source_subject"]: str(YearMonth.from_ordinal(header["source_period"]))
                for header in owners.values()} == expected_owner_periods
        owner_fact_ids = {header["source_fact_id"] for header in owners.values()}
        assert len(owner_fact_ids) == len(owners)
    before_work, before = measure_work(engine, lambda: old_page(
        engine, "2026-04", owner_fact_ids=owner_fact_ids,
    ))
    after_work, after = measure_work(engine, lambda: view.assets("2026-04"))
    assert after == before
    before_counts, after_counts = before_work["counters"], after_work["counters"]
    print(json.dumps({"cards": count, "before": before_counts, "after": after_counts}))
    assert after_counts["calculation_result_rows_loaded"] < before_counts[
        "calculation_result_rows_loaded"
    ]
    assert after_counts["calculation_result_json_decodes"] < before_counts[
        "calculation_result_json_decodes"
    ]
    # Both paths certify the same precise owner facts and complete open
    # consumption directory. This control isolates activation decoding;
    # total transfer/VM work is recorded, not claimed to decrease for every shape.
    assert after_counts["typed_fact_json_decodes"] == before_counts["typed_fact_json_decodes"]
    # Keep actual VM work in the report: a single large batch can add necessary
    # identity-check SQL work while reducing transferred and decoded bodies.
    # This is not a claim of lower VM work or page latency for every shape.
    with view._snapshot("2026-04") as snap:
        events = snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
        assert {event["asset_id"] for event in events} == set(identities)
        assert not snap.reads._asset_members
    for options in ({"asset_filter": "fixed"}, {"asset_id": identities[-1]}):
        assert view.assets("2026-04", **options) == old_page(engine, "2026-04", **options)
    first = after["data"]["collections"]["assets"]
    if count > 20:
        options = dict(section="assets", cursor=first["page"]["next_cursor"],
                       expected_version=after["snapshot_version"])
        assert view.assets("2026-04", **options) == old_page(engine, "2026-04", **options)
        with engine.store.connection(read_only=True) as connection:
            owner = connection.execute(
                "SELECT id FROM calculation WHERE kind='asset_activation_batch'"
            ).fetchone()[0]
        damage(engine, "asset_batch_member",
               "DELETE FROM asset_batch_member WHERE owner_calculation_id=? AND asset_id=?",
               (owner, identities[-1]), foreign_keys=False)
        for read in (lambda: view.assets("2026-04", asset_id=identities[0]),
                     lambda: old_page(engine, "2026-04", asset_id=identities[0])):
            with pytest.raises(KernelError):
                read()


@pytest.fixture
def activated_company(tmp_path):
    company = Company(tmp_path / "activation.sqlite")
    prepare_batch_assets(company)
    with company.engine.store.connection(read_only=True) as connection:
        evidence = company.engine.store.current_fact(connection, "computer").evidence[0]
    return company, evidence


def test_open_multiple_months_and_closed_inheritance_preserve_all_amounts(activated_company):
    company, evidence = activated_company
    engine = company.engine
    month(engine, evidence, "2026-02", "consume-zero")
    month(engine, evidence, "2026-03", "consume-march")
    assert Dashboard(engine).assets("2026-03") == old_page(engine, "2026-03")
    company.close("2026-02")
    assert Dashboard(engine).assets("2026-03") == old_page(engine, "2026-03")
    with Dashboard(engine)._snapshot("2026-03") as snap:
        events = snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
        assert {event["asset_id"] for event in events} == {"computer", "chair"}
        assert all(event["frozen_identity"] for event in events)
        assert snap.reads._asset_members == {}
        first_selection = snap.queries._asset_owner_metadata_selection(snap.connection, snap.period)
        assert first_selection is snap.queries._asset_owner_metadata_selection(
            snap.connection, snap.period,
        )


def test_projects_summary_unowned_and_fixed_v1_keep_original_scope(activated_company):
    company, _ = activated_company
    engine = company.engine
    company.close("2026-02")
    view = Dashboard(engine)
    with patch.object(BusinessQueries, "_selected_asset_activation_identities",
                      side_effect=AssertionError("Identity shortcut outside card page")):
        view.assets("2026-02", section="projects")
        with view._snapshot("2026-02") as snap:
            _assets(snap, summary_only=True)
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        assert BusinessQueries(engine)._selected_asset_activation_identities(
            connection, "2026-02",
        ) is None
    with historical_content(1), view._snapshot("2026-02") as snap:
        assert snap.queries._selected_asset_activation_identities(
            snap.connection, snap.period,
        ) is None


@pytest.mark.parametrize("bad", ["other_member", "owner_kind"])
def test_failed_complete_activation_identity_never_publishes_selection_cache(
    activated_company, bad,
):
    company, _ = activated_company
    engine = company.engine
    company.close("2026-02")
    with engine.store.connection(read_only=True) as connection:
        owner = connection.execute(
            "SELECT id FROM calculation WHERE kind='asset_activation_batch'"
        ).fetchone()[0]
    if bad == "other_member":
        damage(engine, "asset_batch_member",
               "DELETE FROM asset_batch_member WHERE owner_calculation_id=? AND asset_id='chair'",
               (owner,), foreign_keys=False)
    else:
        damage(engine, "calculation",
               "UPDATE calculation SET kind='asset_consumption_month' WHERE id=?",
               (owner,))
    with Dashboard(engine)._snapshot("2026-02") as snap:
        for _ in range(2):
            with pytest.raises(KernelError):
                snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
            assert not snap.reads._asset_members
            assert not any(key[0] in {
                "asset_owner_identity_selection", "asset_activation_identity_events",
            }
                           for key in snap.reads._report_snapshot_cache)
    with pytest.raises(KernelError):
        Dashboard(engine).assets("2026-02", asset_id="computer")


def test_latest_member_body_remains_strict_and_failure_is_not_cached(activated_company):
    company, evidence = activated_company
    engine = company.engine
    company.close("2026-02")
    month(engine, evidence, "2026-03", "consume-positive")
    company.close("2026-03")
    with engine.store.connection(read_only=True) as connection:
        member = dict(connection.execute(
            "SELECT m.*,c.outcome FROM asset_batch_member m JOIN calculation o "
            "ON o.id=m.owner_calculation_id JOIN calculation c ON c.id=m.member_calculation_id "
            "WHERE o.kind='asset_consumption_month' AND m.asset_id='chair'"
        ).fetchone())
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
           ('{"values":{},' + member["outcome"][1:], member["member_calculation_id"]))
    with pytest.raises(KernelError):
        Dashboard(engine).assets("2026-03", asset_id="chair")
    with Dashboard(engine)._snapshot("2026-03") as snap:
        snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
        snap.queries._selected_asset_member_heads(snap.connection, snap.period, asset_ids={"chair"})
        for _ in range(2):
            with pytest.raises(KernelError):
                snap.reads.asset_members_many((member["owner_calculation_id"],))
            assert member["owner_calculation_id"] not in snap.reads._asset_members
