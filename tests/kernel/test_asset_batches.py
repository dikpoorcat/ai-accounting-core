"""Focused end-to-end accounting ownership checks for asset batch publication."""


import pytest

from ai_accounting.kernel.asset_batches import AssetBatches, frozen_members
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.service import default_registry
from ai_accounting.kernel.storage import Store


@pytest.fixture
def asset_engine(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "batch.sqlite",
            default_registry(),
            "company",
            "91310000123456789A",
            "database",
        )
    )
    evidence = engine.register_evidence(
        b"asset acceptance", "text/plain", "acceptance", request_id="evidence"
    )["digest"]
    for asset_id, kind, amount in (
        ("fixed-a", "fixed", 10001),
        ("intangible-b", "intangible", 20003),
    ):
        engine.save_fact(
            "asset",
            asset_id,
            {
                "period": "2026-01",
                "asset_type": kind,
                "acquisition_date": "2026-01-02",
                "supplier_id": "supplier",
                "cost_fen": amount,
                "acquisition_basis": "direct_purchase",
            },
            evidence=(evidence,),
            expected_revision=0,
            request_id="save-" + asset_id,
        )
    preview = engine.preview(["fixed-a", "intangible-b"])
    engine.confirm(
        ["fixed-a", "intangible-b"],
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="acquire",
    )
    return engine, evidence


def activation_members(revision=0, benefit="administration"):
    return [
        {
            "subject_id": "activate-" + asset_id,
            "expected_revision": revision,
            "data": {
                "period": "2026-01",
                "asset_id": asset_id,
                "in_use_date": "2026-01-03",
                "useful_life_months": 3,
                "residual_fen": 0,
                "benefit_area": benefit,
                "rounding_policy": "floor_final_remainder",
            },
        }
        for asset_id in ("fixed-a", "intangible-b")
    ]


def activate(engine, evidence, *, revision=0, benefit="administration", request="activate"):
    batches = AssetBatches(engine)
    kwargs = dict(
        subject_id="activation-batch",
        period="2026-01",
        members=activation_members(revision, benefit),
        evidence=(evidence,),
        expected_revision=0,
    )
    preview = batches.prepare_activation_batch(**kwargs)
    result = batches.confirm_activation_batch(
        **kwargs, preview_digest=preview["digest"], epochs=preview["epochs"], request_id=request
    )
    return preview, result


def month(engine, evidence, period, request):
    batches = AssetBatches(engine)
    kwargs = dict(period=period, evidence=(evidence,), expected_revision=0)
    preview = batches.prepare_consumption_month(**kwargs)
    result = batches.confirm_consumption_month(
        **kwargs, preview_digest=preview["digest"], epochs=preview["epochs"], request_id=request
    )
    return preview, result


def test_batch_one_voucher_card_lines_and_balances(asset_engine):
    engine, evidence = asset_engine
    preview, result = activate(engine, evidence)
    assert len([r for r in result["results"] if r["voucher_number"] is not None]) == 1
    with engine.store.connection(read_only=True) as connection:
        owner = next(r for r in result["results"] if r["subject_id"] == "activation-batch")
        members = frozen_members(connection, owner["calculation_id"])
        assert [m["line_start"] for m in members] == [1, 3]
        assert (
            connection.execute(
            "SELECT count(*) FROM calculation_publication p "
            "JOIN calculation c ON c.id=p.calculation_id "
            "WHERE c.kind='asset_activation'"
            ).fetchone()[0]
            == 0
        )
        voucher_version = connection.execute(
            "SELECT id FROM voucher_version WHERE calculation_id=?",
            (owner["calculation_id"],),
        ).fetchone()[0]
    owner_trace = engine.trace(voucher_version_id=voucher_version)
    assert [item["asset_id"] for item in owner_trace["asset_batch"]["members"]] == [
        "fixed-a",
        "intangible-b",
    ]
    member_trace = engine.trace(calculation_id=members[0]["member_calculation_id"])
    assert member_trace["asset_batch_owners"][0]["owner_calculation_id"] == owner[
        "calculation_id"
    ]
    month(engine, evidence, "2026-01", "january")
    preview, result = month(engine, evidence, "2026-02", "february")
    owner = next(r for r in preview["results"] if r["kind"] == "asset_consumption_month")
    assert owner["values"]["member_count"] == 2
    assert len(owner["lines"]) == 4
    before = engine.overview("2026-02")
    engine.rebuild_projections(request_id="rebuild")
    assert before == engine.overview("2026-02")


def test_preview_readonly_and_atomic_failure(asset_engine):
    engine, evidence = asset_engine
    batches = AssetBatches(engine)
    kwargs = dict(
        subject_id="activation-batch",
        period="2026-01",
        members=activation_members(),
        evidence=(evidence,),
        expected_revision=0,
    )
    preview = batches.prepare_activation_batch(**kwargs)
    with engine.store.connection(read_only=True) as connection:
        assert (
            connection.execute(
                "SELECT count(*) FROM subject WHERE kind='asset_activation'"
            ).fetchone()[0]
            == 0
        )
    engine.fault = lambda stage, _: (
        (_ for _ in ()).throw(RuntimeError("injected")) if stage == "calculation" else None
    )
    with pytest.raises(RuntimeError, match="injected"):
        batches.confirm_activation_batch(
            **kwargs,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id="failure",
        )
    with engine.store.connection(read_only=True) as connection:
        assert connection.execute("SELECT count(*) FROM asset_batch_member").fetchone()[0] == 0
        assert (
            connection.execute(
                "SELECT count(*) FROM subject WHERE kind='asset_activation'"
            ).fetchone()[0]
            == 0
        )
        assert connection.execute("SELECT next_number FROM state").fetchone()[0] == 3


def test_direct_card_registration_blocked(asset_engine):
    engine, evidence = asset_engine
    member = activation_members()[0]
    with pytest.raises(KernelError, match="类型化命令"):
        engine.save_fact(
            "asset_activation",
            member["subject_id"],
            member["data"],
            evidence=(evidence,),
            expected_revision=0,
            request_id="bypass",
        )


def test_open_activation_amendment_rebuilds_month_preserving_numbers(asset_engine):
    engine, evidence = asset_engine
    _, initial = activate(engine, evidence)
    _, january = month(engine, evidence, "2026-01", "january")
    expected = {
        r["subject_id"]: r["voucher_number"]
        for r in (*initial["results"], *january["results"])
        if r["voucher_number"]
    }
    batches = AssetBatches(engine)
    kwargs = dict(
        subject_id="activation-batch",
        period="2026-01",
        members=activation_members(1, "sales"),
        evidence=(evidence,),
        expected_revision=1,
    )
    preview = batches.prepare_activation_batch(**kwargs)
    changed = batches.confirm_activation_batch(
        **kwargs, preview_digest=preview["digest"], epochs=preview["epochs"], request_id="amend"
    )
    assert {
        r["subject_id"]: r["voucher_number"] for r in changed["results"] if r["voucher_number"]
    } == expected
    assert (
        next(r for r in preview["results"] if r["kind"] == "asset_consumption_month")["lines"][0][
            "account"
        ]
        == "560103"
    )


def test_open_activation_batch_withdraws_member_and_preserves_number(asset_engine):
    engine, evidence = asset_engine
    _, initial = activate(engine, evidence)
    original_number = next(
        row["voucher_number"]
        for row in initial["results"]
        if row["subject_id"] == "activation-batch"
    )
    batches = AssetBatches(engine)
    kwargs = dict(
        subject_id="activation-batch",
        period="2026-01",
        members=activation_members(1)[:1],
        evidence=(evidence,),
        expected_revision=1,
    )
    preview = batches.prepare_activation_batch(**kwargs)
    result = batches.confirm_activation_batch(
        **kwargs,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="withdraw-intangible",
    )
    owner = next(row for row in result["results"] if row["subject_id"] == "activation-batch")
    assert owner["voucher_number"] == original_number
    with engine.store.connection(read_only=True) as connection:
        assert [row["asset_id"] for row in frozen_members(connection, owner["calculation_id"])] == [
            "fixed-a"
        ]
        assert (
            connection.execute(
                "SELECT 1 FROM fact_current WHERE subject_id='activate-intangible-b'"
            ).fetchone()
            is None
        )
        disposition = connection.execute(
            "SELECT action FROM disposition "
            "WHERE subject_id='activate-intangible-b' ORDER BY rowid DESC"
        ).fetchone()
        assert disposition[0] == "withdrawn"


def test_activation_withdrawal_rejects_real_consumption_dependants(asset_engine):
    engine, evidence = asset_engine
    activate(engine, evidence)
    month(engine, evidence, "2026-01", "january")
    with pytest.raises(KernelError) as error:
        AssetBatches(engine).prepare_activation_batch(
            "activation-batch",
            "2026-01",
            activation_members(1)[:1],
            evidence=(evidence,),
            expected_revision=1,
        )
    assert error.value.code == "has_dependents"
