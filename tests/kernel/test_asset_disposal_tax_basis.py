"""Fictional asset-sale contracts exercise the real typed lifecycle graph."""

import pytest
from entity_fixture import seed_entities
from tax_point_fixture import contract_tax_basis
from test_asset_batches import activate, month
from test_asset_batches import asset_engine as asset_engine

from ai_accounting.kernel.asset_batches import AssetBatches
from ai_accounting.kernel.contracts import NeedsInformation, Read


def sale_inputs(engine, proof):
    activate(engine, proof)
    month(engine, proof, "2026-01", "january")
    month(engine, proof, "2026-02", "february")
    seed_entities(engine, (("buyer", "organization", None),))
    policy = engine.save_fact(
        "used_asset_vat_policy", "used-vat", {
            "period": "2026-01", "policy": {
                "version": "synthetic-used-asset-2026", "effective_from": "2026-01-01",
                "effective_to": "2026-12-31",
                "source_url": "https://www.chinatax.gov.cn/synthetic-used-asset-rule",
                "tax_base_rate_percent": "3", "payable_rate_percent": "2",
            },
        }, evidence=(proof,), expected_revision=0, request_id="used-vat-policy",
    )
    sale = engine.save_fact(
        "asset_disposal", "sale-fixed", {
            "period": "2026-02", "asset_id": "fixed-a", "disposal_date": "2026-02-28",
            "disposal_kind": "sale", "gross_proceeds_fen": 5150, "buyer_id": "buyer",
            "vat_policy_id": "used-vat", "exemption_eligible": False,
        }, evidence=(proof,), expected_revision=0, request_id="asset-sale-input",
    )
    return policy, sale


def test_asset_sale_uses_explicit_contract_tax_period_and_exact_source_versions(asset_engine):
    engine, proof = asset_engine
    policy, sale = sale_inputs(engine, proof)
    basis = contract_tax_basis(
        engine, "asset_disposal", "sale-fixed", business_period="2026-02",
        contract_period="2026-02", contract_date="2026-02-26", reviewed_through="2026-02",
    )
    batches = AssetBatches(engine)
    options = {"period": "2026-02", "evidence": (proof,), "expected_revision": 1}
    preview = batches.prepare_consumption_month(**options)
    result = batches.confirm_consumption_month(
        **options, preview_digest=preview["digest"], epochs=preview["epochs"],
        request_id="confirm-explicit-sale",
    )
    assert "sale-fixed" in {item["subject_id"] for item in result["results"]}
    with engine.store.connection(read_only=True) as connection:
        calculated = engine.store.select(
            connection, Read("calculation", "asset_disposal", "@sale-fixed")
        )[0]
        lines = list(connection.execute(
            "SELECT l.account,l.debit,l.credit FROM voucher_line l "
            "JOIN voucher_version v ON v.id=l.version_id WHERE v.calculation_id=? "
            "ORDER BY l.line_no", (calculated.id,),
        ))
        dependencies = {row[0] for row in connection.execute(
            "SELECT fact_id FROM dependency_fact WHERE calculation_id=?", (calculated.id,),
        )}
    assert calculated.values["accrued_vat_fen"] == 100
    assert calculated.values["net_sales_fen"] == 5000
    assert calculated.values["gain_loss_fen"] == -1618
    assert calculated.values["carrying_fen"] == 0
    assert calculated.values["tax_point_basis"]["basis_fact_id"] == basis["fact_id"]
    interval = calculated.values["tax_point_basis"]["intervals"][0]
    assert interval["period"] == "2026-02"
    assert interval["actual_date"] == "2026-02-26"
    assert tuple(lines[0]) == ("1601", 0, 10001)
    assert tuple(lines[1]) == ("1602", 3333, 0)
    assert ("222101", 0, 100) in [tuple(line) for line in lines]
    assert sum(line["debit"] for line in lines) == sum(line["credit"] for line in lines)
    assert {sale["fact_id"], policy["fact_id"], basis["fact_id"]} <= dependencies


def test_asset_sale_without_tax_point_basis_blocks_atomically(asset_engine):
    engine, proof = asset_engine
    sale_inputs(engine, proof)
    with engine.store.connection(read_only=True) as connection:
        before = list(connection.iterdump())
    with pytest.raises(NeedsInformation) as failure:
        AssetBatches(engine).prepare_consumption_month(
            "2026-02", evidence=(proof,), expected_revision=1,
        )
    assert failure.value.issues[0]["field"] == "tax_point_basis"
    with engine.store.connection(read_only=True) as connection:
        assert list(connection.iterdump()) == before
        assert not connection.execute(
            "SELECT 1 FROM calculation_current WHERE subject_id='sale-fixed'"
        ).fetchone()


def test_reusing_identical_contract_preserves_exact_tax_basis_version(asset_engine):
    engine, proof = asset_engine
    original = contract_tax_basis(
        engine, "asset_disposal", "sale-fixed", business_period="2026-02",
        contract_period="2026-02", contract_date="2026-02-26", reviewed_through="2026-02",
    )
    with engine.store.connection(read_only=True) as connection:
        before = list(connection.iterdump())
    reused = contract_tax_basis(
        engine, "asset_disposal", "sale-fixed", business_period="2026-02",
        contract_period="2026-02", contract_date="2026-02-26", reviewed_through="2026-02",
    )
    assert reused["fact_id"] == original["fact_id"]
    assert reused["revision"] == original["revision"]
    with engine.store.connection(read_only=True) as connection:
        assert list(connection.iterdump()) == before


def adopt_empty_quarter(engine, proof):
    from test_business_domains import surtax_policy, vat_policy
    for kind, subject, policy in (
        ("vat_policy", "vat", vat_policy()), ("surtax_policy", "surtax", surtax_policy()),
    ):
        engine.save_fact(kind, subject, {"period": "2026-01",
                         "policy": policy.model_dump(mode="json")},
                         evidence=(proof,), expected_revision=0, request_id="future-" + subject)
    engine.save_fact("tax_assessment", "first-quarter", {
        "period": "2026-03", "period_start": "2026-01-01", "period_end": "2026-03-31",
        "vat_policy_id": "vat", "surtax_policy_id": "surtax",
    }, evidence=(proof,), expected_revision=0, request_id="empty-quarter-input")
    preview = engine.preview(["first-quarter"])
    engine.confirm(["first-quarter"], preview_digest=preview["digest"], epochs=preview["epochs"],
                   request_id="publish-empty-quarter")


@pytest.mark.parametrize("future_kind", ["asset_disposal", "advance_fulfillment"])
def test_proven_future_tax_scope_does_not_block_adopted_prior_quarter(asset_engine, future_kind):
    from ai_accounting.kernel.tax_filing_data import TaxFilingData

    engine, proof = asset_engine
    seed_entities(engine, (("buyer", "organization", None),))
    adopt_empty_quarter(engine, proof)
    if future_kind == "asset_disposal":
        data = {"period": "2026-04", "asset_id": "fixed-a", "disposal_date": "2026-04-28",
                "disposal_kind": "sale", "gross_proceeds_fen": 5150, "buyer_id": "buyer",
                "vat_policy_id": "used-vat", "exemption_eligible": False}
    else:
        contract_tax_basis(engine, "advance", "advance", business_period="2026-03",
                           contract_period="2026-04", reviewed_through="2026-04")
        engine.save_fact("advance", "advance", {
            "period": "2026-03", "counterparty_id": "buyer", "amount_fen": 10100,
            "side": "customer", "contractual_obligation_established": True,
            "vat_due_on_advance": False,
        }, evidence=(proof,), expected_revision=0, request_id="future-advance-input")
        preview = engine.preview(["advance"])
        engine.confirm(["advance"], preview_digest=preview["digest"], epochs=preview["epochs"],
                       request_id="future-advance-publish")
        data = {"period": "2026-04", "advance_id": "advance", "fulfilled_gross_fen": 10100,
                "vat_policy_id": "vat", "exemption_eligible": False}
    contract_tax_basis(engine, future_kind, "future-source", business_period="2026-04",
                       contract_period="2026-04", reviewed_through="2026-04")
    engine.save_fact(future_kind, "future-source", data, evidence=(proof,), expected_revision=0,
                     request_id="future-source-input")
    result = TaxFilingData(engine).query(2026, 1)
    current_tax = next(field for field in result["fields"]
                       if field["field_id"] == "vat_and_surtax.row_20_fen"
                       and field["period_basis"] == "current")
    assert current_tax["value_fen"] == 0 and current_tax["status"] == "ready"


def test_zero_price_sale_contract_does_not_crash_or_invent_a_taxable_amount(asset_engine):
    from ai_accounting.kernel.tax_filing_data import TaxFilingData

    engine, proof = asset_engine
    adopt_empty_quarter(engine, proof)
    seed_entities(engine, (("buyer", "organization", None),))
    contract_tax_basis(engine, "asset_disposal", "zero-sale", business_period="2026-02",
                       contract_period="2026-02", reviewed_through="2026-03")
    saved = engine.save_fact("asset_disposal", "zero-sale", {
        "period": "2026-02", "asset_id": "fixed-a", "disposal_date": "2026-02-28",
        "disposal_kind": "sale", "gross_proceeds_fen": 0, "buyer_id": "buyer",
        "vat_policy_id": "used-vat", "exemption_eligible": False,
    }, evidence=(proof,), expected_revision=0, request_id="zero-sale-input")
    with engine.store.connection(read_only=True) as connection:
        before = tuple(connection.iterdump())
    result = TaxFilingData(engine).query(2026, 1)
    current_tax = next(field for field in result["fields"]
                       if field["field_id"] == "vat_and_surtax.row_20_fen"
                       and field["period_basis"] == "current")
    assert current_tax["status"] == "needs_information" and current_tax["value_fen"] is None
    assert any(item.get("subject_id") == "zero-sale" and item.get("fact_id") == saved["fact_id"]
               for item in current_tax["fact_issues"])
    with engine.store.connection(read_only=True) as connection:
        assert tuple(connection.iterdump()) == before
