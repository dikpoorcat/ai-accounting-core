from dataclasses import replace
from datetime import date

import pytest
from _postgres_helpers import authenticated_business_database
from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import AmendEventRequest, DeleteEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.fixed_asset_service import FixedAssetService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventComponent,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDepreciationBatch,
    OpenItem,
    Voucher,
)
from ai_accounting.schemas import (
    AcquireFixedAssetRequest,
    ConfirmFixedAssetDepreciationRequest,
    PreviewFixedAssetDepreciationRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


def _acquisition_facts(*, ready: bool) -> dict:
    facts = {
        "category": "electronic",
        "expected_use_over_one_year": True,
        "cost_fen": 130_000,
        "settlement_method": "payable",
        "claims_creditable_input_vat": False,
    }
    if ready:
        facts["ready_for_use"] = {
            "in_service_date": "2026-01-15",
            "useful_life_months": 13,
            "residual_value_fen": 0,
            "benefit_area": "management",
        }
    return facts


def _acquisition_metadata() -> dict:
    return {
        "asset_code": "FA-LOCAL-001",
        "asset_name": "延迟登记设备",
        "counterparty": {"kind": "supplier", "name": "延迟登记设备供应商"},
        "due_date": "2026-03-31",
    }


def _ready_acquisition_with_depreciation(org_id, evidence_id, *, key):
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "posting_date": "2026-02-28",
            "description": "延迟登记已达到可使用状态的设备及首月折旧",
            "components": [
                {
                    "key": "depreciation",
                    "kind": "fixed_asset_depreciation",
                    "business_date": "2026-02-28",
                    "activation_component_key": "acquisition",
                    "facts": {
                        "asset_id": None,
                        "depreciation_period": "2026-02",
                        "calculation_hash": None,
                    },
                    "metadata": {"confirmation_note": "确认首个应计折旧月"},
                    "evidence_references": [evidence_id],
                },
                {
                    "key": "acquisition",
                    "kind": "fixed_asset_acquisition",
                    "business_date": "2026-01-15",
                    "facts": _acquisition_facts(ready=True),
                    "metadata": _acquisition_metadata(),
                    "evidence_references": [evidence_id],
                },
            ],
        }
    )


def _activation_with_depreciation_batch(org_id, evidence_id, asset_id, *, key):
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "posting_date": "2026-02-28",
            "description": "延迟登记设备启用及单资产首月批量折旧",
            "components": [
                {
                    "key": "depreciation-batch",
                    "kind": "fixed_asset_depreciation_batch",
                    "business_date": "2026-02-28",
                    "activation_component_keys": ["activation"],
                    "facts": {
                        "depreciation_period": "2026-02",
                        "calculation_hash": None,
                    },
                    "metadata": {"confirmation_note": "确认单项资产首月批量折旧"},
                    "evidence_references": [evidence_id],
                },
                {
                    "key": "activation",
                    "kind": "fixed_asset_activation",
                    "business_date": "2026-01-15",
                    "facts": {
                        "asset_id": asset_id,
                        "useful_life_months": 13,
                        "residual_value_fen": 0,
                        "benefit_area": "management",
                    },
                    "evidence_references": [evidence_id],
                },
            ],
        }
    )


def _preview_reviewed(session, authority, request):
    with authority.attributed_call(session, tool_name="finance_preview_event"):
        preview = ComponentService(session).preview(request)
    assert preview.status == "calculated", preview
    return RecordEventRequest.model_validate(preview.data["reviewed_request"]), preview


def _component_ids(session, event_id):
    return {
        component.key: component.id
        for component in session.scalars(
            select(BusinessEventComponent).where(BusinessEventComponent.event_id == event_id)
        )
    }


def _source_graph_ids(session, event_id):
    asset = session.scalar(select(FixedAsset).where(FixedAsset.acquisition_event_id == event_id))
    activation = session.scalar(
        select(FixedAssetActivation).where(FixedAssetActivation.event_id == event_id)
    )
    depreciation = session.scalar(
        select(FixedAssetDepreciation).where(FixedAssetDepreciation.event_id == event_id)
    )
    voucher = session.scalar(select(Voucher).where(Voucher.event_id == event_id))
    assert asset is not None
    assert activation is not None
    assert depreciation is not None
    assert voucher is not None
    return asset.id, activation.id, depreciation.id, voucher.id, _component_ids(session, event_id)


def test_ready_acquisition_and_first_depreciation_amend_then_delete_atomically():
    with authenticated_business_database("local_ready_asset_lifecycle") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            request = _ready_acquisition_with_depreciation(
                org_id, evidence_id, key="local-ready-asset"
            )
            reviewed, preview = _preview_reviewed(session, authority, request)
            assert [item["key"] for item in preview.data["components"]] == [
                "acquisition",
                "depreciation",
            ]
            assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 0
            assert session.scalar(select(func.count()).select_from(FixedAsset)) == 0
            assert session.scalar(select(func.count()).select_from(FixedAssetActivation)) == 0
            assert session.scalar(select(func.count()).select_from(FixedAssetDepreciation)) == 0

            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = ComponentService(session).record(reviewed)
            assert posted.status == "posted", posted
            session.commit()

            original_ids = _source_graph_ids(session, posted.event_id)
            source_component = session.scalar(
                select(BusinessEventComponent).where(
                    BusinessEventComponent.event_id == posted.event_id,
                    BusinessEventComponent.key == "acquisition",
                )
            )
            depreciation_component = session.scalar(
                select(BusinessEventComponent).where(
                    BusinessEventComponent.event_id == posted.event_id,
                    BusinessEventComponent.key == "depreciation",
                )
            )
            assert depreciation_component.derived["local_activation_proofs"] == [
                {
                    "component_key": "acquisition",
                    "source_kind": "fixed_asset_acquisition",
                    "activation_projection_hash": source_component.derived[
                        "activation_projection_hash"
                    ],
                    "asset_id": source_component.derived["asset_id"],
                    "activation_id": source_component.derived["activation_id"],
                }
            ]

            replacement_payload = reviewed.model_dump(mode="json")
            replacement_payload["idempotency_key"] = "local-ready-asset-replacement"
            replacement_payload["description"] = "调整设备成本并重算首月折旧"
            acquisition_payload = next(
                item for item in replacement_payload["components"] if item["key"] == "acquisition"
            )
            acquisition_payload["facts"]["cost_fen"] = 143_000
            replacement = RecordEventRequest.model_validate(replacement_payload)
            with authority.attributed_call(session, tool_name="finance_amend_event"):
                amended = EventAmendmentService(session).amend(
                    AmendEventRequest(
                        org_id=org_id,
                        event_id=posted.event_id,
                        idempotency_key="amend-local-ready-asset",
                        expected_facts_hash=posted.data["facts_hash"],
                        reason="补充成本依据后调整设备成本",
                        replacement=replacement,
                    )
                )
            assert amended["status"] == "posted", amended
            session.commit()
            assert _source_graph_ids(session, posted.event_id) == original_ids
            assert session.get(FixedAsset, original_ids[0]).cost_fen == 143_000
            assert session.get(FixedAssetDepreciation, original_ids[2]).amount_fen == 11_000

            with authority.attributed_call(session, tool_name="finance_delete_event"):
                deleted = EventAmendmentService(session).amend(
                    DeleteEventRequest(
                        org_id=org_id,
                        event_id=posted.event_id,
                        idempotency_key="delete-local-ready-asset",
                        expected_facts_hash=amended["facts_hash"],
                        reason="整笔延迟登记误录",
                    )
                )
            assert deleted["status"] == "deleted", deleted
            session.commit()

            assert session.get(BusinessEvent, posted.event_id).status == "deleted"
            for model, predicate in (
                (BusinessEventComponent, BusinessEventComponent.event_id == posted.event_id),
                (FixedAsset, FixedAsset.acquisition_event_id == posted.event_id),
                (FixedAssetActivation, FixedAssetActivation.event_id == posted.event_id),
                (FixedAssetDepreciation, FixedAssetDepreciation.event_id == posted.event_id),
                (OpenItem, OpenItem.source_event_id == posted.event_id),
                (Voucher, Voucher.event_id == posted.event_id),
            ):
                assert session.scalar(select(func.count()).select_from(model).where(predicate)) == 0


def test_local_activation_single_asset_batch_reverse_waits_for_external_depreciation():
    with authenticated_business_database("local_activation_batch_lifecycle") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            acquisition = AcquireFixedAssetRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": "acquire-before-local-activation",
                    "purchase_date": "2026-01-15",
                    "posting_date": "2026-01-15",
                    "evidence_references": [evidence_id],
                    **_acquisition_facts(ready=False),
                }
            )
            with authority.attributed_call(session, tool_name="finance_acquire_fixed_asset"):
                acquired = FixedAssetService(session).acquire_fixed_asset(acquisition)
            assert acquired.status == "posted", acquired
            session.commit()

            request = _activation_with_depreciation_batch(
                org_id,
                evidence_id,
                acquired.asset_id,
                key="local-activation-one-asset-batch",
            )
            reviewed, preview = _preview_reviewed(session, authority, request)
            batch_preview = next(
                item for item in preview.data["components"] if item["key"] == "depreciation-batch"
            )
            assert batch_preview["derived"]["asset_count"] == 1
            assert session.scalar(select(func.count()).select_from(FixedAssetActivation)) == 0
            assert (
                session.scalar(select(func.count()).select_from(FixedAssetDepreciationBatch)) == 0
            )

            with authority.attributed_call(session, tool_name="finance_record_event"):
                posted = ComponentService(session).record(reviewed)
            assert posted.status == "posted", posted
            session.commit()
            source_activation = session.scalar(
                select(FixedAssetActivation).where(FixedAssetActivation.event_id == posted.event_id)
            )
            source_batch = session.scalar(
                select(FixedAssetDepreciationBatch).where(
                    FixedAssetDepreciationBatch.event_id == posted.event_id
                )
            )
            source_depreciation = session.scalar(
                select(FixedAssetDepreciation).where(
                    FixedAssetDepreciation.event_id == posted.event_id
                )
            )
            assert source_activation.asset_id == acquired.asset_id
            assert source_batch.asset_count == 1
            assert source_depreciation.batch_id == source_batch.id

            service = FixedAssetService(session)
            later_request = PreviewFixedAssetDepreciationRequest(
                org_id=org_id,
                asset_id=acquired.asset_id,
                depreciation_period="2026-03",
                posting_date=date(2026, 3, 31),
            )
            later_preview = service.preview_fixed_asset_depreciation(later_request)
            assert later_preview.status == "calculated", later_preview
            with authority.attributed_call(
                session, tool_name="finance_confirm_fixed_asset_depreciation"
            ):
                later = service.confirm_fixed_asset_depreciation(
                    ConfirmFixedAssetDepreciationRequest(
                        **later_request.model_dump(),
                        idempotency_key="external-march-depreciation",
                        calculation_hash=later_preview.calculation_hash,
                    )
                )
            assert later.status == "posted", later
            session.commit()

            with authority.attributed_call(session, tool_name="finance_reverse_event"):
                blocked = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=posted.event_id,
                        idempotency_key="reverse-local-activation-before-external",
                        posting_date=date(2026, 3, 31),
                        reason="验证必须先处理外部后续折旧",
                    )
                )
                assert blocked.status == "rejected", blocked
                assert blocked.errors == ["FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"]
                reversed_later = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=later.event_id,
                        idempotency_key="reverse-external-depreciation",
                        posting_date=date(2026, 3, 31),
                        reason="先冲正外部后续折旧",
                    )
                )
                assert reversed_later.status == "posted", reversed_later
                reversed_source = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=posted.event_id,
                        idempotency_key="reverse-local-activation-and-batch",
                        posting_date=date(2026, 3, 31),
                        reason="整体冲正启用与首月折旧",
                    )
                )
                assert reversed_source.status == "posted", reversed_source
            session.commit()

            assert session.get(BusinessEvent, acquired.event_id).status == "posted"
            assert session.get(BusinessEvent, posted.event_id).status == "reversed"
            assert len(_component_ids(session, reversed_source.event_id)) == 2


def test_postgres_rejects_forged_local_activation_proof_atomically(monkeypatch):
    with authenticated_business_database("local_asset_forged_proof") as (
        engine,
        org_id,
        evidence_id,
        authority,
    ):
        with Session(engine) as session:
            request = _ready_acquisition_with_depreciation(
                org_id, evidence_id, key="forged-local-asset-proof"
            )
            reviewed, _ = _preview_reviewed(session, authority, request)
            compile_original = FixedAssetService.compile_depreciation

            def forged_compile(service, depreciation_request, **kwargs):
                plan = compile_original(service, depreciation_request, **kwargs)
                proofs = [dict(item) for item in plan.derived["local_activation_proofs"]]
                assert len(proofs) == 1
                proofs[0]["activation_projection_hash"] = "0" * 64
                return replace(
                    plan,
                    derived=plan.derived | {"local_activation_proofs": proofs},
                )

            monkeypatch.setattr(
                FixedAssetService,
                "compile_depreciation",
                forged_compile,
            )
            with authority.attributed_call(session, tool_name="finance_record_event"):
                forged = ComponentService(session).record(reviewed)
                assert forged.status == "posted", forged
                with pytest.raises(
                    DBAPIError,
                    match="FIXED_ASSET_LOCAL_ACTIVATION_PROOF_MISMATCH",
                ):
                    session.commit()
                session.rollback()

            assert (
                session.scalar(
                    select(BusinessEvent.id).where(
                        BusinessEvent.org_id == org_id,
                        BusinessEvent.idempotency_key == "forged-local-asset-proof",
                    )
                )
                is None
            )
            for model in (
                BusinessEventComponent,
                FixedAsset,
                FixedAssetActivation,
                FixedAssetDepreciation,
                OpenItem,
                Voucher,
            ):
                assert session.scalar(select(func.count()).select_from(model)) == 0
