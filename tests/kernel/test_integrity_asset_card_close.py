"""A frozen asset card is accepted only through its actual voucher-owning batch."""

import pytest
from close_storage_fixture import replace_stored_manifest, stored_manifest
from test_asset_batch_reads import prepare_batch_assets
from test_integrity_content import verify
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import KernelError


@pytest.fixture
def closed_company(tmp_path):
    company = Company(tmp_path / "closed-assets.sqlite", opening_period="2026-02")
    prepare_batch_assets(company)
    company.close("2026-02")
    return company


def test_full_integrity_accepts_frozen_batch_and_card_relationships(closed_company):
    result = verify(closed_company.engine)
    assert result["status"] == "verified"
    assert result["counts"]["closes"] == 1


def test_v1_asset_membership_uses_released_proof_after_current_rules_change(
    closed_company, monkeypatch
):
    from ai_accounting.kernel import asset_batch_models, asset_batches, asset_membership_v1

    engine = closed_company.engine
    assert verify(engine)["counts"]["closes"] == 1
    with engine.store.connection(read_only=True) as connection:
        owners = [row[0] for row in connection.execute(
            "SELECT id FROM calculation WHERE kind IN "
            "('asset_activation_batch','asset_consumption_month') ORDER BY id"
        )]
        expected = {owner: asset_batches.frozen_members(connection, owner) for owner in owners}
    assert owners and all(expected.values())

    def current_rule_must_not_run(*_args):
        raise AssertionError("v1 asset membership called a current rule")

    monkeypatch.setattr(asset_batches, "frozen_members", current_rule_must_not_run)
    monkeypatch.setattr(asset_batches, "digest", current_rule_must_not_run)
    monkeypatch.setattr(asset_batch_models, "OWNER_KINDS", frozenset())
    with engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        assert {
            owner: asset_membership_v1.frozen_members(connection, owner) for owner in owners
        } == expected


@pytest.mark.parametrize("changed", ["asset", "digest", "owner"])
def test_full_integrity_rejects_forged_card_adoption(closed_company, changed):
    engine = closed_company.engine
    with engine.store.connection(read_only=True) as connection:
        manifest = stored_manifest(connection)
    replace_stored_manifest(engine, manifest, preserve_journal_reference=True)
    assert verify(engine)["status"] == "verified"
    adoption = manifest["asset_card_adoptions"][0]
    if changed == "asset":
        adoption["asset_id"] = "unrelated-asset"
    elif changed == "digest":
        adoption["result_digest"] = "0" * 64
    else:
        # A no-voucher card is a manifest member, but cannot stand in for its owner.
        adoption["acceptance_calculation_id"] = adoption["calculation_id"]
        adoption["acceptance_result_digest"] = adoption["result_digest"]
    replace_stored_manifest(engine, manifest, preserve_journal_reference=True)
    with pytest.raises(KernelError) as failure:
        verify(engine, include_indexes=False)
    assert failure.value.code == "content_integrity_failed"
    assert failure.value.details["reason"] == "asset_card_adoption_mismatch"
