"""A frozen asset card is accepted only through its actual voucher-owning batch."""

import json

import pytest
from close_storage_fixture import replace_stored_manifest, stored_manifest
from test_asset_batch_reads import prepare_batch_assets
from test_integrity_content import verify
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import KernelError


@pytest.fixture
def closed_company(tmp_path):
    company = Company(tmp_path / "closed-assets.sqlite")
    prepare_batch_assets(company)
    company.close("2026-02")
    return company


def test_full_integrity_accepts_frozen_batch_and_card_relationships(closed_company):
    result = verify(closed_company.engine)
    assert result["status"] == "verified"
    assert result["counts"]["closes"] == 1


def test_v1_asset_membership_uses_released_proof_after_current_rules_change(
    closed_company, tmp_path, monkeypatch
):
    from ai_accounting.kernel import asset_batch_models, asset_batches, content_v1

    engine = closed_company.engine
    contract_dir = tmp_path / "schema_contracts"
    contract_dir.mkdir()
    (contract_dir / "content-v1.json").write_text(
        json.dumps(content_v1.content_contract(engine.store.registry), ensure_ascii=False),
        encoding="utf-8",
    )
    monkeypatch.setattr(content_v1, "__file__", str(tmp_path / "content_v1.py"))

    def current_rule_must_not_run(*_args):
        raise AssertionError("v1 asset membership called a current rule")

    monkeypatch.setattr(asset_batches, "frozen_members", current_rule_must_not_run)
    monkeypatch.setattr(asset_batches, "digest", current_rule_must_not_run)
    monkeypatch.setattr(asset_batch_models, "OWNER_KINDS", frozenset())
    content_v1.v1_registry.cache_clear()
    try:
        with engine.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            result = content_v1.verify_v1_company(connection, engine.store.bundle)
        assert result["status"] == "verified"
        assert result["counts"]["closes"] == 1
    finally:
        content_v1.v1_registry.cache_clear()


@pytest.mark.parametrize("changed", ["asset", "digest", "owner"])
def test_full_integrity_rejects_forged_card_adoption(closed_company, changed):
    engine = closed_company.engine
    with engine.store.connection(read_only=True) as connection:
        manifest = stored_manifest(connection)
    adoption = manifest["asset_card_adoptions"][0]
    if changed == "asset":
        adoption["asset_id"] = "unrelated-asset"
    elif changed == "digest":
        adoption["result_digest"] = "0" * 64
    else:
        # A no-voucher card is a manifest member, but cannot stand in for its owner.
        adoption["acceptance_calculation_id"] = adoption["calculation_id"]
        adoption["acceptance_result_digest"] = adoption["result_digest"]
    replace_stored_manifest(engine, manifest)
    with pytest.raises(KernelError) as failure:
        verify(engine, include_indexes=False)
    assert failure.value.code == "content_integrity_failed"
    assert failure.value.details["reason"] == "asset_card_adoption_mismatch"
