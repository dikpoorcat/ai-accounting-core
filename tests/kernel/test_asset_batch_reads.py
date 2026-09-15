"""The dashboard reads card contributions only through exact batch adoption."""

import json

from test_payroll_corrections import Company
from test_reimbursement_assets import accepted_batch, activation, asset, batch_card
from test_reimbursement_assets import book as book_fixture

from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.domains.assets import ReimbursedAsset, ReimbursedAssetBatch
from ai_accounting.kernel.periods import Periods
from ai_accounting.kernel.query_reads import QueryReads
from ai_accounting.kernel.read_indexes import (
    CLOSE_ASSET_CARD_ACCEPTANCES,
    CLOSE_ASSET_CARD_ADOPTIONS,
)
from ai_accounting.kernel.types import YearMonth

book = book_fixture


def prepare_batch_assets(company):
    company.save(ReimbursedAssetBatch.model_validate_json(json.dumps(accepted_batch())), "batch")
    computer = company.save(
        ReimbursedAsset.model_validate_json(json.dumps(batch_card())), "computer"
    )
    company.save(ReimbursedAsset.model_validate_json(json.dumps(batch_card(30000))), "chair")
    company.publish("batch", "computer", "chair")
    with company.engine.store.connection(read_only=True) as connection:
        evidence = company.engine.store.fact(connection, computer["fact_id"]).evidence
    members = [
        {"subject_id": "activate-computer", "expected_revision": 0, "data": activation()},
        {
            "subject_id": "activate-chair",
            "expected_revision": 0,
            "data": activation(asset_id="chair", useful_life_months=6),
        },
    ]
    batches = AssetBatches(company.engine)
    options = {"evidence": evidence, "expected_revision": 0}
    preview = batches.prepare_activation_batch("activation-batch", "2026-02", members, **options)
    batches.confirm_activation_batch(
        "activation-batch",
        "2026-02",
        members,
        **options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="activate-batch",
    )


def test_batch_voucher_and_asset_cards_share_exact_amounts_without_double_counting(book):
    engine, save, publish = book
    proof = save("reimbursed_asset", "computer", asset())["fact_id"]
    save("reimbursed_asset", "printer", asset())
    publish("computer", "printer")
    with engine.store.connection(read_only=True) as connection:
        evidence = engine.store.fact(connection, proof).evidence
    batches = AssetBatches(engine)
    members = [
        {"subject_id": "activate-computer", "expected_revision": 0, "data": activation()},
        {
            "subject_id": "activate-printer",
            "expected_revision": 0,
            "data": activation(asset_id="printer", useful_life_months=6),
        },
    ]
    options = {"evidence": evidence, "expected_revision": 0}
    preview = batches.prepare_activation_batch("activation-batch", "2026-02", members, **options)
    batches.confirm_activation_batch(
        "activation-batch",
        "2026-02",
        members,
        **options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="activate-batch",
    )
    activation_voucher = next(
        row
        for row in Dashboard(engine).brief("2026-02", section="vouchers")["data"]["vouchers"]
        if row["kind"] == "asset_activation_batch"
    )
    assert {item["asset_id"] for item in activation_voucher["asset_members"]} == {
        "computer",
        "printer",
    }
    acquisition_vouchers = [
        row
        for row in Dashboard(engine).brief("2026-02", section="vouchers")["data"]["vouchers"]
        if row["kind"] == "reimbursed_asset"
    ]
    assert {row["asset"]["asset_id"] for row in acquisition_vouchers} == {
        "computer",
        "printer",
    }
    preview = batches.prepare_consumption_month("2026-03", **options)
    batches.confirm_consumption_month(
        "2026-03",
        **options,
        preview_digest=preview["digest"],
        epochs=preview["epochs"],
        request_id="consume-month",
    )
    dashboard = Dashboard(engine)
    cards = dashboard.assets("2026-03")["data"]
    assert cards["month_charge_fen"] == cards["ledger_accumulated_fen"] == 30000
    assert cards["card_net_fen"] == cards["ledger_net_fen"] == 210000
    assert cards["reconciled"]
    voucher = dashboard.brief("2026-03", section="vouchers")["data"]["vouchers"][0]
    assert voucher["kind"] == "asset_consumption_month"
    assert voucher["business_amount_fen"] == 30000
    assert len(voucher["lines"]) == 4
    assert {line["asset"]["asset_id"] for line in voucher["lines"]} == {"computer", "printer"}
    assert {item["amount_fen"] for item in voucher["asset_members"]} == {10000, 20000}
    with engine.store.connection(read_only=True) as connection:
        reads = QueryReads(engine, connection)
        queries = BusinessQueries(engine, reads=reads)
        members = queries._selected_asset_members(
            connection, "2026-03", kinds={"asset_consumption"}
        )
        assert len(members) == 2
        assert all(
            row["posting_period"] is None
            for row in reads.metadata(item["calculation_id"] for item in members).values()
        )
        selected = queries._selected_accounting(connection, None, "2026-03")["through_period"]
        assert not any(
            item["kind"] == "asset_consumption"
            for item in (*selected["voucher_events"], *selected["state_results"])
        )
        basis = Display._content_basis(connection, "2026-03", registry=engine.store.registry)
        assert len(basis["accounting_sources"]["asset_batch_members"]) == 4
        assert all(
            item["owner_calculation_id"]
            for item in basis["accounting_sources"]["asset_batch_members"]
        )


def test_close_freezes_batch_backed_asset_card_adoptions(tmp_path):
    company = Company(tmp_path / "asset-card-close.sqlite")
    prepare_batch_assets(company)
    assert Dashboard(company.engine).assets("2026-02")["data"]["reconciled"] is True

    company.close("2026-02")

    assets = Dashboard(company.engine).assets("2026-02")["data"]
    assert assets["reconciled"] is True
    assert assets["unestablished_count"] == 0
    assert assets["card_cost_fen"] == assets["ledger_cost_fen"] == 150000
    with company.engine.store.connection(read_only=True) as connection:
        manifest = json.loads(
            connection.execute(
                "SELECT manifest FROM period_close WHERE period=?",
                (YearMonth("2026-02").ordinal,),
            ).fetchone()[0]
        )
        assert {item["asset_id"] for item in manifest["asset_card_adoptions"]} == {
            "computer",
            "chair",
        }
        indexed = dict(
            connection.execute(
                "SELECT path,count(*) FROM close_reference WHERE close_period=? "
                "AND path IN (?,?) GROUP BY path",
                (
                    YearMonth("2026-02").ordinal,
                    CLOSE_ASSET_CARD_ADOPTIONS,
                    CLOSE_ASSET_CARD_ACCEPTANCES,
                ),
            )
        )
        assert indexed == {
            CLOSE_ASSET_CARD_ADOPTIONS: 2,
            CLOSE_ASSET_CARD_ACCEPTANCES: 2,
        }
    selected = BusinessQueries(company.engine).business_status("computer", "2026-02")[
        "selected_accounting"
    ]["through_period"]
    card = next(item for item in selected["state_results"] if item["kind"] == "reimbursed_asset")
    assert card["selection_proof"]["basis"] == "manifest_asset_card_adoption"


def test_legacy_close_proves_only_the_exact_batch_backed_asset_cards(tmp_path, monkeypatch):
    company = Company(tmp_path / "legacy-asset-card-close.sqlite")
    prepare_batch_assets(company)
    manifest = Periods._manifest

    def legacy_manifest(self, *args, **kwargs):
        result = manifest(self, *args, **kwargs)
        result.pop("asset_card_adoptions")
        return result

    monkeypatch.setattr(Periods, "_manifest", legacy_manifest)
    company.close("2026-02")

    assets = Dashboard(company.engine).assets("2026-02")["data"]
    assert assets["reconciled"] is True
    assert assets["unestablished_count"] == 0
    selected = BusinessQueries(company.engine).business_status("computer", "2026-02")[
        "selected_accounting"
    ]["through_period"]
    card = next(item for item in selected["state_results"] if item["kind"] == "reimbursed_asset")
    with company.engine.store.connection(read_only=True) as connection:
        acceptance_id = QueryReads(company.engine, connection).parents(card["calculation_id"])[0]
    assert card["selection_proof"] == {
        "basis": "legacy_manifest_asset_card_adoption",
        "contract_version": 1,
        "acceptance_calculation_id": acceptance_id,
    }
