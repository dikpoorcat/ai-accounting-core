"""One owner body serves selection and membership; accounting proof stays exact."""

import json

import pytest
from stage9_metrics import measure_work
from test_asset_batch_reads import prepare_batch_assets
from test_asset_batches import activate, month
from test_asset_batches import asset_engine as asset_engine_fixture
from test_integrity_content import damage
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.types import canonical, digest

asset_engine = asset_engine_fixture


def complete_head_events(snap, asset_ids):
    fields = (
        "calculation_id", "kind", "calculation_period", "adoption_period", "asset_id",
        "owner_calculation_id", "voucher_version_id", "voucher_number", "direction",
    )
    return [
        {field: event[field] for field in fields}
        for event in snap.queries._selected_asset_members(
            snap.connection, snap.period, asset_ids=asset_ids,
        )
    ]


@pytest.mark.parametrize("months", [1, 3])
def test_selected_members_reuse_owner_body_and_keep_complete_business_results(
    asset_engine, months
):
    engine, evidence = asset_engine
    activate(engine, evidence)
    for number in range(1, months + 1):
        month(engine, evidence, f"2026-{number:02}", f"consume-{number}")

    def selected():
        with Dashboard(engine)._snapshot(f"2026-{months:02}") as snap:
            return snap.queries._selected_asset_member_heads(
                snap.connection, snap.period, asset_ids={"fixed-a", "intangible-b"}
            )

    new_work, actual = measure_work(engine, selected)
    def complete():
        with Dashboard(engine)._snapshot(f"2026-{months:02}") as snap:
            return complete_head_events(snap, {"fixed-a", "intangible-b"})

    old_work, independently_read = measure_work(engine, complete)
    assert {row["kind"] for row in independently_read} == {
        "asset_activation", "asset_consumption",
    }
    assert actual == [row for row in independently_read if row["kind"] == "asset_consumption"]
    assert {row["asset_id"] for row in independently_read} == {"fixed-a", "intangible-b"}
    assert {row["asset_id"] for row in actual} == (
        {"intangible-b"} if months == 1 else {"fixed-a", "intangible-b"}
    )
    assert {row["adoption_period"] for row in actual} >= {"2026-01"}
    newer, older = new_work["counters"], old_work["counters"]
    assert newer["calculation_result_bytes_loaded"] < older["calculation_result_bytes_loaded"]
    assert newer["calculation_result_json_decodes"] < older["calculation_result_json_decodes"]
    print(json.dumps({"months": months, "combined": newer, "independent": older}))


def test_owner_decode_failure_publishes_no_local_body_or_success_proof(asset_engine):
    engine, evidence = asset_engine
    activate(engine, evidence)
    month(engine, evidence, "2026-01", "consume")
    with engine.store.connection(read_only=True) as connection:
        owners = list(connection.execute(
            "SELECT id,outcome FROM calculation WHERE kind IN "
            "('asset_activation_batch','asset_consumption_month') ORDER BY id"
        ))
    assert len(owners) == 2
    broken = json.loads(owners[-1]["outcome"])
    broken["synthetic_damage"] = True
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
           (canonical(broken), owners[-1]["id"]))
    ids = {row["id"] for row in owners}
    with Dashboard(engine)._snapshot("2026-01") as snap:
        local = {}
        with pytest.raises(KernelError, match="摘要不一致"):
            snap.reads.metadata(ids, _decoded_outcomes=local)
        assert local == {}
        assert not ids & snap.reads._verified_sql_outcomes
        assert not ids & snap.reads._metadata.keys()


@pytest.mark.parametrize("broken_reusable", [False, True])
def test_mixed_owner_batch_failure_preserves_prior_proofs_and_publishes_no_half_batch(
    asset_engine, broken_reusable
):
    engine, evidence = asset_engine
    activate(engine, evidence)
    month(engine, evidence, "2026-01", "consume")
    with engine.store.connection(read_only=True) as connection:
        owners = {
            row["kind"]: dict(row) for row in connection.execute(
                "SELECT id,kind,outcome FROM calculation WHERE kind IN "
                "('asset_activation_batch','asset_consumption_month')"
            )
        }
    reusable = owners["asset_activation_batch"]
    fresh = owners["asset_consumption_month"]
    damaged = reusable if broken_reusable else fresh
    broken = json.loads(damaged["outcome"])
    if broken_reusable:
        # Saved-byte proof and fixed Outcome shape are separate obligations.
        # Matching bytes must not let a malformed reused Outcome pass state.
        broken["lines"] = {}
        damage(engine, "calculation", "UPDATE calculation SET outcome=?,digest=? WHERE id=?",
               (canonical(broken), digest(broken), damaged["id"]))
    else:
        broken["synthetic_damage"] = True
        damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
               (canonical(broken), damaged["id"]))
    ids = {reusable["id"], fresh["id"]}
    with Dashboard(engine)._snapshot("2026-01") as snap:
        snap.reads.verify_selected_content({reusable["id"]})
        metadata_before = dict(snap.reads._metadata)
        sql_proofs_before = set(snap.reads._verified_sql_outcomes)
        content_proofs_before = dict(snap.reads._verified_source_contents)
        local = {}
        with pytest.raises(KernelError, match="结构不一致" if broken_reusable else "摘要不一致"):
            snap.reads.metadata(ids, _decoded_outcomes=local)
        assert local == {}
        assert snap.reads._metadata == metadata_before
        assert snap.reads._verified_sql_outcomes == sql_proofs_before
        assert snap.reads._verified_source_contents == content_proofs_before


def test_frozen_owner_uses_same_adoption_without_reloading_its_body(tmp_path):
    company = Company(tmp_path / "frozen-owner.sqlite")
    prepare_batch_assets(company)
    company.close("2026-02")

    def selected():
        with Dashboard(company.engine)._snapshot("2026-02") as snap:
            return snap.queries._selected_asset_member_heads(
                snap.connection, snap.period, asset_ids={"computer", "chair"}
            )

    combined, actual = measure_work(company.engine, selected)
    def complete():
        with Dashboard(company.engine)._snapshot("2026-02") as snap:
            return complete_head_events(snap, {"computer", "chair"})

    baseline, expected = measure_work(company.engine, complete)
    assert actual == [row for row in expected if row["kind"] == "asset_consumption"] == []
    assert {row["kind"] for row in expected} == {"asset_activation"}
    assert {row["asset_id"] for row in expected} == {"computer", "chair"}
    assert {row["adoption_period"] for row in expected} == {"2026-02"}
    print(json.dumps({"frozen": combined["counters"], "independent": baseline["counters"]}))
    assert combined["counters"]["calculation_result_bytes_loaded"] < (
        baseline["counters"]["calculation_result_bytes_loaded"]
    )
    assert combined["counters"]["calculation_result_json_decodes"] < (
        baseline["counters"]["calculation_result_json_decodes"]
    )


def test_ambiguous_owner_body_still_rejects_before_membership(asset_engine):
    engine, evidence = asset_engine
    activate(engine, evidence)
    with engine.store.connection(read_only=True) as connection:
        owner = connection.execute(
            "SELECT id,outcome FROM calculation WHERE kind='asset_activation_batch'"
        ).fetchone()
    # Both interpreters would otherwise choose different values for this field.
    malformed = '{"values":{},' + owner["outcome"][1:]
    damage(engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
           (malformed, owner["id"]))
    with Dashboard(engine)._snapshot("2026-01") as snap:
        with pytest.raises(KernelError, match="重复字段"):
            snap.queries._selected_asset_member_heads(
                snap.connection, snap.period, asset_ids={"fixed-a"}
            )
