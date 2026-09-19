"""Pending lifecycle facts join an unchanged asset owner's reviewed graph."""

import pytest
from test_asset_batches import activate, activation_members, month
from test_asset_batches import asset_engine as shared_asset_engine

from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.integrity import verify_integrity

asset_engine = shared_asset_engine


def pending_disposal(engine, proof):
    activate(engine, proof)
    month(engine, proof, "2026-01", "january")
    month(engine, proof, "2026-02", "february")
    engine.save_fact(
        "asset_disposal",
        "fixed-scrap",
        {
            "period": "2026-02",
            "asset_id": "fixed-a",
            "disposal_date": "2026-02-28",
            "disposal_kind": "scrap",
            "gross_proceeds_fen": 0,
        },
        evidence=(proof,),
        expected_revision=0,
        request_id="save-disposal",
    )


def commands(engine, proof, mode):
    batches = AssetBatches(engine)
    options = {"evidence": (proof,), "expected_revision": 1}
    if mode == "activation":
        options.update(
            subject_id="activation-batch",
            period="2026-01",
            members=activation_members(revision=1),
        )
        return batches.prepare_activation_batch, batches.confirm_activation_batch, options
    options["period"] = "2026-02"
    return batches.prepare_consumption_month, batches.confirm_consumption_month, options


@pytest.mark.parametrize("mode", ["activation", "consumption"])
def test_unchanged_batch_publishes_pending_disposal_once(asset_engine, mode):
    engine, proof = asset_engine
    pending_disposal(engine, proof)
    with pytest.raises(KernelError) as blocked:
        engine.preview(["fixed-scrap"])
    assert blocked.value.code == "asset_batch_command_required"
    prepare, confirm, options = commands(engine, proof, mode)
    with engine.store.connection(read_only=True) as connection:
        before = connection.execute("SELECT count(*) FROM voucher").fetchone()[0]
    preview = prepare(**options)
    assert "fixed-scrap" in preview["subjects"]
    assert preview["fact_changes"] == []
    arguments = dict(
        **options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="publish-disposal",
    )
    result = confirm(**arguments)
    assert confirm(**arguments) == result
    with engine.store.connection(read_only=True) as connection:
        assert not connection.execute("SELECT 1 FROM pending").fetchone()
        assert connection.execute("SELECT count(*) FROM voucher").fetchone()[0] == before + 1
        assert not connection.execute(
            "SELECT 1 FROM calculation_publication p JOIN calculation c "
            "ON c.id=p.calculation_id WHERE c.kind IN ('asset_activation','asset_consumption')"
        ).fetchone()
        assert (
            connection.execute(
                "SELECT count(*) FROM calculation_current a JOIN calculation c "
                "ON c.id=a.calculation_id JOIN calculation_publication p ON p.calculation_id=c.id "
                "WHERE c.subject_id='fixed-scrap'"
            ).fetchone()[0]
            == 1
        )
        verify_integrity(engine, connection)


def test_pending_disposal_batch_failure_rolls_back_and_rejects_stale_preview(asset_engine):
    engine, proof = asset_engine
    pending_disposal(engine, proof)
    prepare, confirm, options = commands(engine, proof, "consumption")
    preview = prepare(**options)
    assert "fixed-scrap" in preview["subjects"]
    with engine.store.connection(read_only=True) as connection:
        before = list(connection.iterdump())

    def fail(stage, connection):
        if stage == "calculation":
            raise RuntimeError("pending-disposal-failure")

    engine.fault = fail
    arguments = dict(
        **options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="publish-disposal",
    )
    with pytest.raises(RuntimeError, match="pending-disposal-failure"):
        confirm(**arguments)
    with engine.store.connection(read_only=True) as connection:
        assert list(connection.iterdump()) == before
    engine.fault = lambda *_: None
    engine.amend_fact(
        "asset_disposal",
        "fixed-scrap",
        {
            "period": "2026-02",
            "asset_id": "fixed-a",
            "disposal_date": "2026-02-27",
            "disposal_kind": "scrap",
            "gross_proceeds_fen": 0,
        },
        evidence=(proof,),
        expected_revision=1,
        recording_error_confirmed=True,
        request_id="amend-disposal",
    )
    with pytest.raises(KernelError) as stale:
        confirm(**arguments)
    assert stale.value.code == "preview_expired"
