"""Card identity selection leaves actual asset amounts on strict result reads."""

import json

import pytest
from stage9_metrics import measure_work
from test_integrity_content import damage, verify
from test_payroll_corrections import Company
from test_reimbursement_assets import asset

from ai_accounting.kernel.backup import BackupError, verify_file
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.dashboard import ASSET_KINDS, Dashboard, _asset_card_sources
from ai_accounting.kernel.dashboard_reads import verified_scalar_facts
from ai_accounting.kernel.domains.assets import ReimbursedAsset
from ai_accounting.kernel.types import canonical


def source_ids(snap, identity):
    sources = _asset_card_sources(snap) if identity else {
        "acquisitions": {calc["fact"]["data"]["asset_id"]: calc for calc in
                         snap.calculations_of_kind(*ASSET_KINDS)},
        "activations": {calc["fact"]["data"]["asset_id"]: calc for calc in
                        snap.calculations_of_kind("asset_activation")},
        "disposals": {calc["fact"]["data"]["asset_id"]: calc for calc in
                      snap.calculations_of_kind("asset_disposal")},
    }
    return {key: {asset_id: (calc["id"], calc["fact_id"])
                  for asset_id, calc in sources[key].items()}
            for key in ("acquisitions", "activations", "disposals")}


@pytest.mark.parametrize("mode", ["open_replace", "closed_correction", "late_review"])
def test_asset_identity_keeps_exact_core_head_after_replacement_and_review(
    tmp_path, monkeypatch, mode
):
    from ai_accounting.kernel import engine as engine_module

    company = Company(tmp_path / "asset-replacement.sqlite")
    company.save(ReimbursedAsset.model_validate_json(json.dumps(asset())), "computer")
    company.publish("computer")
    if mode != "open_replace":
        company.close("2026-02")
    if mode == "late_review":
        with monkeypatch.context() as scope:
            scope.setattr(engine_module, "PROGRAM_VERSION", "synthetic-asset-review")
            _, result = company.publish("computer")
        assert result["computer"]["impact"] == "review_no_impact"
    else:
        company.save(ReimbursedAsset.model_validate_json(json.dumps(asset(
            cost_fen=150000,
            creditors=[{"employee_id": "alice", "amount_fen": 150000}],
        ))), "computer", revision=1)
        company.publish("computer", **(
            {"posting_period": "2026-03"} if mode == "closed_correction" else {}
        ))
    company.save(ReimbursedAsset.model_validate_json(json.dumps(
        asset(period="2026-03", asset_id="unpublished")
    )), "unpublished")
    for period in ("2026-02", "2026-03"):
        with Dashboard(company.engine)._snapshot(period) as snap:
            assert source_ids(snap, True) == source_ids(snap, False)


def test_asset_identity_keeps_member_withdrawal_selection(tmp_path):
    from test_asset_batches import activate, activation_members, asset_engine

    from ai_accounting.kernel.asset_batches import AssetBatches

    engine, proof = asset_engine.__wrapped__(tmp_path)
    activate(engine, proof)
    batches = AssetBatches(engine)
    options = dict(subject_id="activation-batch", period="2026-01",
                   members=activation_members(1)[:1], evidence=(proof,), expected_revision=1)
    preview = batches.prepare_activation_batch(**options)
    batches.confirm_activation_batch(**options, preview_digest=preview["digest"],
                                     epochs=preview["epochs"], request_id="withdraw-member")
    with Dashboard(engine)._snapshot("2026-01") as snap:
        narrowed = source_ids(snap, True)
        assert narrowed == source_ids(snap, False)
        assert set(narrowed["activations"]) == {"fixed-a"}


@pytest.mark.parametrize("closed", [False, True])
def test_asset_identity_matches_core_without_result_transfer(tmp_path, closed):
    company = Company(tmp_path / "asset-identity.sqlite")
    company.save(ReimbursedAsset.model_validate_json(json.dumps(asset())), "computer")
    company.publish("computer")
    if closed:
        company.close("2026-02")
    company.save(ReimbursedAsset.model_validate_json(json.dumps(
        asset(period="2026-03", asset_id="unpublished")
    )), "unpublished")

    def selected(identity):
        with Dashboard(company.engine)._snapshot("2026-03") as snap:
            if identity:
                calculations = _asset_card_sources(snap)["acquisitions"].values()
            else:
                calculations = list(snap.calculations.selected(kinds=ASSET_KINDS).values())
                verified_scalar_facts(snap, calculations)
            return {calc["subject_id"]: (calc["id"], calc["fact_id"])
                    for calc in calculations}

    narrow_work, narrow = measure_work(company.engine, lambda: selected(True))
    full_work, full = measure_work(company.engine, lambda: selected(False))
    assert narrow == full
    assert narrow_work["counters"].get("calculation_result_rows_loaded", 0) == 0
    assert full_work["counters"]["calculation_result_rows_loaded"] > 0
    print(json.dumps({"closed": closed, "asset_identity": narrow_work["counters"],
                      "asset_core": full_work["counters"]}, ensure_ascii=False))


def test_unused_asset_body_is_outside_identity_but_amount_detail_and_backup_reject(tmp_path):
    company = Company(tmp_path / "asset-body.sqlite")
    company.save(ReimbursedAsset.model_validate_json(json.dumps(asset())), "computer")
    company.publish("computer")
    company.close("2026-02")
    company.save(ReimbursedAsset.model_validate_json(json.dumps(
        asset(period="2026-03", asset_id="unpublished")
    )), "unpublished")
    with company.engine.store.connection(read_only=True) as connection:
        ident, raw = connection.execute(
            "SELECT c.id,c.outcome FROM calculation_current h JOIN calculation c "
            "ON c.id=h.calculation_id WHERE h.subject_id='computer'"
        ).fetchone()
    body = json.loads(raw)
    body["synthetic_unused_change"] = True
    damage(company.engine, "calculation", "UPDATE calculation SET outcome=? WHERE id=?",
           (canonical(body), ident))
    with Dashboard(company.engine)._snapshot("2026-03") as snap:
        assert _asset_card_sources(snap)["acquisitions"]["computer"]["id"] == ident
        assert ident not in snap.reads._verified_sql_outcomes
        assert ident not in snap.reads._verified_source_contents
        with pytest.raises(KernelError):
            snap.calculations.selected(kinds=ASSET_KINDS)
    with pytest.raises(KernelError):
        Dashboard(company.engine).assets("2026-03", preparation="deferred")
    with pytest.raises(KernelError):
        verify(company.engine)
    with pytest.raises(BackupError):
        verify_file(company.engine.store.path)
