"""Owner cards omit retired timing reads; core membership remains complete."""

import json
from unittest.mock import patch

import pytest
from stage9_metrics import measure_work
from test_asset_batches import month
from test_integrity_content import damage
from test_owner_asset_activation_identity import closed_cards
from test_payroll_corrections import Company
from test_reimbursement_assets import activate_assets, activation, asset

from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.domains.assets import ReimbursedAsset
from ai_accounting.kernel.maintenance import Maintenance
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.types import YearMonth


def test_many_activation_owners_keep_full_cards_and_consumption_history(tmp_path):
    company = Company(tmp_path / "many-owners.sqlite")
    ids = [f"card-{i:02}" for i in range(24)]
    for i, ident in enumerate(ids):
        company.save(
            ReimbursedAsset.model_validate_json(
                json.dumps(
                    asset(
                        asset_id=ident,
                        cost_fen=120000 + i * 12,
                        creditors=[{"employee_id": "alice", "amount_fen": 120000 + i * 12}],
                    )
                )
            ),
            ident,
        )
    company.publish(*ids)
    engine = company.engine
    with engine.store.connection(read_only=True) as connection:
        evidence = engine.store.current_fact(connection, ids[0]).evidence
    for ident in ids:
        activate_assets(
            engine,
            "batch-" + ident,
            "2026-02",
            [("activate-" + ident, activation(asset_id=ident))],
            evidence,
        )
    company.close("2026-02")
    month(engine, evidence[0], "2026-03", "consume-march")
    company.close("2026-03")
    month(engine, evidence[0], "2026-04", "consume-april")
    view = Dashboard(engine)
    certified_owners = set()
    original_members = QueryReads.asset_members_many

    def recorded_members(reads, owners, *, _decoded_owners=None):
        owners = tuple(owners)
        certified_owners.update(owners)
        return original_members(reads, owners, _decoded_owners=_decoded_owners)

    with (
        patch.object(BusinessQueries, "_selected_asset_member_heads",
                     side_effect=AssertionError("Owner cards do not consume timing heads")),
        patch.object(QueryReads, "asset_members_many", recorded_members),
    ):
        work, after = measure_work(engine, lambda: view.assets("2026-04"))
    with engine.store.connection(read_only=True) as connection:
        owner_periods = {
            row["id"]: row["period"] for row in connection.execute(
                "SELECT id,period FROM calculation WHERE kind='asset_consumption_month'"
            )
        }
    # The open April owner is necessary; March's closed mutable directory is
    # outside this owner money read, which uses its authenticated carrying root.
    assert {owner_periods[ident] for ident in certified_owners if ident in owner_periods} == {
        YearMonth("2026-04").ordinal
    }
    assert view.assets("2026-04") == after
    collection = after["data"]["collections"]["assets"]
    assert collection["page"]["total_count"] == 24
    assert collection["page"]["returned_count"] == 20
    assert after["data"]["active_count"] == 24
    assert all("latest_charge_period" not in item and "charge_state_label" not in item
               for item in collection["items"])
    for options in (
        {"asset_id": ids[-1]},
        {
            "section": "assets",
            "cursor": collection["page"]["next_cursor"],
            "expected_version": after["snapshot_version"],
        },
    ):
        with patch.object(BusinessQueries, "_selected_asset_member_heads",
                          side_effect=AssertionError("Retired owner timing read")):
            selected = view.assets("2026-04", **options)
        assert selected["data"]["ledger_net_fen"] == after["data"]["ledger_net_fen"]
        assert selected["data"]["active_count"] == 24
    print(json.dumps({"owner_work": work["counters"]}))
    with view._snapshot("2026-04") as snap:
        activations = snap.queries._selected_asset_activation_identities(
            snap.connection, snap.period
        )
        assert {event["asset_id"] for event in activations} == set(ids)
        original_selection = snap.queries._asset_owner_metadata_selection(
            snap.connection, snap.period
        )
        heads = snap.queries._selected_asset_member_heads(
            snap.connection, snap.period, asset_ids={ids[0]}
        )
        assert {event["kind"] for event in heads} == {"asset_consumption"}
        assert {event["calculation_period"] for event in heads} == {"2026-03", "2026-04"}
        assert original_selection is snap.queries._asset_owner_metadata_selection(
            snap.connection, snap.period
        )
        assert any(event["kind"] == "asset_activation_batch" for event in original_selection[0])


@pytest.mark.parametrize("kind", ["asset_activation_batch", "asset_consumption_month"])
def test_off_page_sibling_directory_damage_is_rejected_without_new_success_prefix(tmp_path, kind):
    company, ids = closed_cards(tmp_path, 3)
    engine = company.engine
    with engine.store.connection(read_only=True) as connection:
        owner = connection.execute(
            "SELECT o.id FROM calculation o JOIN asset_batch_member m "
            "ON m.owner_calculation_id=o.id "
            "WHERE o.kind=? AND m.asset_id=? ORDER BY o.period DESC LIMIT 1",
            (kind, ids[-1]),
        ).fetchone()[0]
    damage(
        engine,
        "asset_batch_member",
        "DELETE FROM asset_batch_member WHERE owner_calculation_id=? AND asset_id=?",
        (owner, ids[-1]),
        foreign_keys=False,
    )
    view = Dashboard(engine)
    with pytest.raises(KernelError):
        view.assets("2026-04", asset_id=ids[0])
    with view._snapshot("2026-04") as snap:
        if kind == "asset_consumption_month":
            snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
            cache_before = dict(snap.reads._report_snapshot_cache)
            members_before = dict(snap.reads._asset_members)
            for _ in range(2):
                with pytest.raises(KernelError):
                    snap.queries._selected_asset_member_heads(
                        snap.connection, snap.period, asset_ids={ids[0]}
                    )
                assert snap.reads._report_snapshot_cache == cache_before
                assert snap.reads._asset_members == members_before
        else:
            for _ in range(2):
                with pytest.raises(KernelError):
                    snap.queries._selected_asset_activation_identities(snap.connection, snap.period)
                assert not any(
                    k[0] in {"asset_owner_identity_selection", "asset_activation_identity_events"}
                    for k in snap.reads._report_snapshot_cache
                )
                assert not snap.reads._asset_members


def test_selection_none_keeps_original_identity_fallback_arguments(tmp_path):
    company, ids = closed_cards(tmp_path, 2)
    view = Dashboard(company.engine)
    with view._snapshot("2026-04") as snap:
        with (
            patch.object(BusinessQueries, "_asset_owner_metadata_selection", return_value=None),
            patch.object(
                BusinessQueries, "_asset_member_identity_events", return_value=["fallback"]
            ) as read,
        ):
            assert snap.queries._selected_asset_member_heads(
                snap.connection, snap.period, asset_ids={ids[0]}
            ) == ["fallback"]
            assert read.call_args.kwargs == {"asset_ids": {ids[0]}, "selection": None}


def test_closed_consumption_directory_remains_required_by_public_detail_and_integrity(tmp_path):
    company, ids = closed_cards(tmp_path, 3)
    engine = company.engine
    view = Dashboard(engine)
    before = view.assets("2026-04", asset_id=ids[0])
    with engine.store.connection(read_only=True) as connection:
        row = connection.execute(
            "SELECT o.id,m.member_subject_id FROM calculation o JOIN asset_batch_member m "
            "ON m.owner_calculation_id=o.id WHERE o.kind='asset_consumption_month' "
            "AND o.period=(SELECT period FROM period_close ORDER BY period DESC LIMIT 1) "
            "AND m.asset_id=?", (ids[0],),
        ).fetchone()
        owner, member_subject = row
    damage(engine, "asset_batch_member",
           "DELETE FROM asset_batch_member WHERE owner_calculation_id=? AND asset_id=?",
           (owner, ids[-1]), foreign_keys=False)
    # The legitimate frozen carrying root and the open April publication are
    # unchanged. Owner amounts do not claim to certify the old mutable directory.
    assert view.assets("2026-04", asset_id=ids[0]) == before
    with pytest.raises(KernelError):
        BusinessQueries(engine).business_status(member_subject, "2026-03")
    with pytest.raises(KernelError):
        Maintenance(engine).verify_integrity()
