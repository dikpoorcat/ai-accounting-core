"""A frozen asset card is accepted only through its actual voucher-owning batch."""

import json

import pytest
from test_asset_batch_reads import prepare_batch_assets
from test_integrity_content import damage, verify
from test_payroll_corrections import Company

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.types import canonical, digest


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


@pytest.mark.parametrize("changed", ["asset", "digest", "owner"])
def test_full_integrity_rejects_forged_card_adoption(closed_company, changed):
    engine = closed_company.engine
    with engine.store.connection(read_only=True) as connection:
        manifest = json.loads(connection.execute("SELECT manifest FROM period_close").fetchone()[0])
    adoption = manifest["asset_card_adoptions"][0]
    if changed == "asset":
        adoption["asset_id"] = "unrelated-asset"
    elif changed == "digest":
        adoption["result_digest"] = "0" * 64
    else:
        # A no-voucher card is a manifest member, but cannot stand in for its owner.
        adoption["acceptance_calculation_id"] = adoption["calculation_id"]
        adoption["acceptance_result_digest"] = adoption["result_digest"]
    damage(
        engine,
        "period_close",
        "UPDATE period_close SET manifest=?,digest=?",
        (canonical(manifest), digest(manifest)),
    )
    with pytest.raises(KernelError) as failure:
        verify(engine, include_indexes=False)
    assert failure.value.code == "content_integrity_failed"
    assert failure.value.details["reason"] == "asset_card_adoption_mismatch"
