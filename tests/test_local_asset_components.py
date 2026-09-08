from __future__ import annotations

from sqlalchemy import func, select

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    Evidence,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDepreciationBatch,
)


def _evidence(session, organization, key: str) -> Evidence:
    evidence = Evidence(
        org_id=organization.id,
        sha256=(key.encode().hex() + "0" * 64)[:64],
        original_name=f"{key}.pdf",
        media_type="application/pdf",
        source="test",
        size_bytes=1,
        storage_path=f"test/{key}.pdf",
    )
    session.add(evidence)
    session.flush()
    return evidence


def _acquisition_component(*, ready: bool, asset_code: str = "LOCAL-FA-001") -> dict:
    facts = {
        "asset_code": asset_code,
        "asset_name": "本期补录设备",
        "category": "electronic",
        "expected_use_over_one_year": True,
        "cost_components": {
            "purchase_price_fen": 120_000,
            "noncreditable_tax_fen": 0,
            "transport_and_handling_fen": 0,
            "installation_and_direct_cost_fen": 0,
        },
        "supplier": {"kind": "supplier", "name": "本地资产供应商"},
        "settlement_method": "payable",
        "due_date": "2026-03-31",
        "claims_creditable_input_vat": False,
    }
    if ready:
        facts["ready_for_use"] = {
            "in_service_date": "2026-01-10",
            "useful_life_months": 60,
            "residual_value_fen": 0,
            "benefit_area": "management",
        }
    return {
        "key": "asset",
        "kind": "fixed_asset_acquisition",
        "business_date": "2026-01-10",
        "facts": facts,
    }


def _ready_asset_depreciation_request(organization, evidence) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "local-ready-asset-depreciation",
            "posting_date": "2026-02-28",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "depreciation",
                    "kind": "fixed_asset_depreciation",
                    "business_date": "2026-02-01",
                    "activation_component_key": "asset",
                    "facts": {
                        "depreciation_period": "2026-02",
                    },
                },
                _acquisition_component(ready=True),
            ],
        }
    )


def test_ready_acquisition_and_first_depreciation_post_from_one_preview(session, organization):
    evidence = _evidence(session, organization, "local-ready-asset")
    request = _ready_asset_depreciation_request(organization, evidence)

    preview = ComponentService(session).preview(request)
    assert preview.status == "calculated", preview
    reviewed = preview.data["reviewed_request"]
    depreciation = next(c for c in reviewed["components"] if c["key"] == "depreciation")
    assert len(depreciation["facts"]["calculation_hash"]) == 64
    assert depreciation["facts"].get("asset_id") is None
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0
    assert session.scalar(select(func.count()).select_from(FixedAsset)) == 0

    posted = ComponentService(session).record(RecordEventRequest.model_validate(reviewed))
    assert posted.status == "posted", posted
    asset = session.scalar(select(FixedAsset))
    activation = session.scalar(select(FixedAssetActivation))
    depreciation_row = session.scalar(select(FixedAssetDepreciation))
    assert asset.acquisition_event_id == posted.event_id
    assert activation.event_id == depreciation_row.event_id == posted.event_id
    assert depreciation_row.asset_id == asset.id
    assert depreciation_row.activation_id == activation.id
    components = {
        component.key: component
        for component in session.scalars(
            select(BusinessEventComponent).where(BusinessEventComponent.event_id == posted.event_id)
        )
    }
    assert components["asset"].facts["asset_code"] == "LOCAL-FA-001"
    proof = components["depreciation"].derived["local_activation_proofs"][0]
    assert proof == {
        "component_key": "asset",
        "source_kind": "fixed_asset_acquisition",
        "activation_projection_hash": components["asset"].derived["activation_projection_hash"],
        "asset_id": str(asset.id),
        "activation_id": str(activation.id),
    }


def test_local_activation_can_feed_monthly_depreciation_batch(session, organization):
    evidence = _evidence(session, organization, "local-activation-batch")
    acquisition = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "pending-asset-acquisition",
            "posting_date": "2026-01-10",
            "evidence_references": [evidence.id],
            "components": [_acquisition_component(ready=False, asset_code="LOCAL-FA-002")],
        }
    )
    acquired = ComponentService(session).record(acquisition)
    assert acquired.status == "posted", acquired
    asset = session.scalar(select(FixedAsset).where(FixedAsset.asset_code == "LOCAL-FA-002"))
    request = RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "local-activation-depreciation-batch",
            "posting_date": "2026-02-28",
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "batch",
                    "kind": "fixed_asset_depreciation_batch",
                    "business_date": "2026-02-01",
                    "activation_component_keys": ["activation"],
                    "facts": {"depreciation_period": "2026-02"},
                },
                {
                    "key": "activation",
                    "kind": "fixed_asset_activation",
                    "business_date": "2026-01-10",
                    "facts": {
                        "asset_id": asset.id,
                        "useful_life_months": 60,
                        "residual_value_fen": 0,
                        "benefit_area": "management",
                    },
                },
            ],
        }
    )
    preview = ComponentService(session).preview(request)
    assert preview.status == "calculated", preview
    reviewed = preview.data["reviewed_request"]
    batch_facts = next(c for c in reviewed["components"] if c["key"] == "batch")["facts"]
    assert len(batch_facts["calculation_hash"]) == 64
    posted = ComponentService(session).record(RecordEventRequest.model_validate(reviewed))
    assert posted.status == "posted", posted
    activation = session.scalar(
        select(FixedAssetActivation).where(FixedAssetActivation.event_id == posted.event_id)
    )
    depreciation = session.scalar(
        select(FixedAssetDepreciation).where(FixedAssetDepreciation.event_id == posted.event_id)
    )
    batch = session.scalar(
        select(FixedAssetDepreciationBatch).where(
            FixedAssetDepreciationBatch.event_id == posted.event_id
        )
    )
    assert depreciation.activation_id == activation.id
    assert depreciation.batch_id == batch.id
    batch_component = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == posted.event_id,
            BusinessEventComponent.key == "batch",
        )
    )
    assert batch_component.derived["local_activation_proofs"][0]["component_key"] == ("activation")


def test_changed_local_activation_facts_reject_stale_review_without_rows(session, organization):
    evidence = _evidence(session, organization, "stale-local-asset")
    request = _ready_asset_depreciation_request(organization, evidence)
    preview = ComponentService(session).preview(request)
    assert preview.status == "calculated", preview
    reviewed = preview.data["reviewed_request"]
    asset = next(c for c in reviewed["components"] if c["key"] == "asset")
    asset["facts"]["cost_components"]["purchase_price_fen"] += 1

    rejected = ComponentService(session).record(RecordEventRequest.model_validate(reviewed))
    assert rejected.status == "rejected", rejected
    assert "FIXED_ASSET_CALCULATION_STALE" in rejected.errors
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0
    assert session.scalar(select(func.count()).select_from(FixedAsset)) == 0
    assert session.scalar(select(func.count()).select_from(FixedAssetActivation)) == 0
    assert session.scalar(select(func.count()).select_from(FixedAssetDepreciation)) == 0
