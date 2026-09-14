"""The dashboard reads card contributions only through exact batch adoption."""

from test_reimbursement_assets import activation, asset
from test_reimbursement_assets import book as book_fixture

from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.business_queries import BusinessQueries
from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.display import Display
from ai_accounting.kernel.query_reads import QueryReads

book = book_fixture


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
