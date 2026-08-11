from __future__ import annotations

import asyncio
import json
import uuid
from datetime import date
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import select

from ai_accounting import mcp_server
from ai_accounting.coa import seed_organization
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.mcp_server import mcp
from ai_accounting.models import Evidence, FixedAsset
from ai_accounting.schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    ConfirmFixedAssetDepreciationRequest,
    DisposeFixedAssetRequest,
    FixedAssetResult,
    FixedAssetResultStatus,
    PreviewFixedAssetDepreciationRequest,
    RecordEventRequest,
    ReverseEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)

ASSET_TOOL_NAMES = {
    "finance_acquire_fixed_asset",
    "finance_activate_fixed_asset",
    "finance_preview_fixed_asset_depreciation",
    "finance_confirm_fixed_asset_depreciation",
    "finance_dispose_fixed_asset",
    "finance_get_fixed_asset",
}


def _tool_schema(tool_name: str) -> dict[str, Any]:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    return tools[tool_name].model_dump(by_alias=True)["inputSchema"]


def _id() -> str:
    return str(uuid.uuid4())


class _SessionContext:
    def __enter__(self) -> object:
        return object()

    def __exit__(self, *_: object) -> bool:
        return False


class _SessionFactory:
    def begin(self) -> _SessionContext:
        return _SessionContext()

    def __call__(self) -> _SessionContext:
        return _SessionContext()


def test_fixed_asset_tools_publish_strict_typed_contracts_without_free_entries() -> None:
    schemas = {tool_name: _tool_schema(tool_name) for tool_name in ASSET_TOOL_NAMES}

    assert all(schema["additionalProperties"] is False for schema in schemas.values())
    assert schemas["finance_get_fixed_asset"]["required"] == ["org_id", "asset_id"]
    assert "request" in schemas["finance_acquire_fixed_asset"]["required"]
    schema_text = json.dumps(schemas, ensure_ascii=False)
    assert "debit_fen" not in schema_text
    assert "credit_fen" not in schema_text
    assert "account_code" not in schema_text


def test_fixed_asset_capability_is_discoverable_and_generic_event_is_rejected() -> None:
    capability = mcp_server.finance_get_event_schema("fixed_asset")

    assert "fixed_asset" not in capability["disabled_event_types"]
    assert "fixed_asset" in capability["internal_event_types"]
    assert capability["module_capabilities"]["fixed_asset"] == {
        "status": "enabled",
        "entry_tools": [
            "finance_acquire_fixed_asset",
            "finance_activate_fixed_asset",
            "finance_preview_fixed_asset_depreciation",
            "finance_confirm_fixed_asset_depreciation",
            "finance_dispose_fixed_asset",
            "finance_get_fixed_asset",
        ],
        "generic_event_writer": "not_available",
        "accrual_entry": "finance_confirm_fixed_asset_depreciation",
    }
    request = RecordEventRequest.model_validate(
        {
            "org_id": _id(),
            "idempotency_key": "must-not-post-fixed-asset-directly",
            "event_type": "fixed_asset",
            "business_dates": {"business_date": "2026-01-01", "posting_date": "2026-01-01"},
            "amounts": {"amount_fen": 1},
        }
    )
    assert mcp_server.finance_record_event(request) == {
        "status": "rejected",
        "errors": ["FIXED_ASSET_REQUIRES_SPECIALIZED_WORKFLOW"],
    }


def test_fixed_asset_mcp_rejects_extra_fields_and_float_fen_without_echoing_input() -> None:
    invalid_requests = [
        {
            "request": {"org_id": _id(), "idempotency_key": "asset-extra"},
            "unrecognized": "sensitive-value-must-not-leak",
        },
        {
            "request": {
                "org_id": _id(),
                "idempotency_key": "asset-float",
                "cost_components": {"purchase_price_fen": 1.5},
            }
        },
    ]

    async def call(arguments: dict[str, Any]) -> None:
        await mcp.call_tool("finance_acquire_fixed_asset", arguments)

    for arguments in invalid_requests:
        with pytest.raises(ToolError) as error:
            asyncio.run(call(arguments))
        assert "VALIDATION_ERROR" in str(error.value)
        assert "sensitive-value-must-not-leak" not in str(error.value)


def test_fixed_asset_tools_delegate_to_specialized_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeFixedAssetService:
        def acquire_fixed_asset(self, request: AcquireFixedAssetRequest) -> FixedAssetResult:
            calls.append(("acquire", request))
            return FixedAssetResult(status=FixedAssetResultStatus.POSTED)

        def activate_fixed_asset(self, request: ActivateFixedAssetRequest) -> FixedAssetResult:
            calls.append(("activate", request))
            return FixedAssetResult(status=FixedAssetResultStatus.POSTED)

        def preview_fixed_asset_depreciation(
            self, request: PreviewFixedAssetDepreciationRequest
        ) -> FixedAssetResult:
            calls.append(("preview", request))
            return FixedAssetResult(
                status=FixedAssetResultStatus.CALCULATED,
                calculation_hash="a" * 64,
            )

        def confirm_fixed_asset_depreciation(
            self, request: ConfirmFixedAssetDepreciationRequest
        ) -> FixedAssetResult:
            calls.append(("confirm", request))
            return FixedAssetResult(status=FixedAssetResultStatus.POSTED)

        def dispose_fixed_asset(self, request: DisposeFixedAssetRequest) -> FixedAssetResult:
            calls.append(("dispose", request))
            return FixedAssetResult(status=FixedAssetResultStatus.POSTED)

        def get_fixed_asset(self, org_id: uuid.UUID, asset_id: uuid.UUID) -> FixedAssetResult:
            calls.append(("get", (org_id, asset_id)))
            return FixedAssetResult(status=FixedAssetResultStatus.POSTED, asset_id=asset_id)

        def reverse_event(self, request: object) -> FixedAssetResult:
            calls.append(("reverse", request))
            return FixedAssetResult(status=FixedAssetResultStatus.REVERSED)

    service = FakeFixedAssetService()
    monkeypatch.setattr(mcp_server, "SessionLocal", _SessionFactory())
    monkeypatch.setattr(mcp_server, "_fixed_asset_service", lambda _: service)

    org_id = uuid.uuid4()
    asset_id = uuid.uuid4()
    acquisition = AcquireFixedAssetRequest(org_id=org_id, idempotency_key="acquire")
    activation = ActivateFixedAssetRequest(org_id=org_id, idempotency_key="activate")
    preview = PreviewFixedAssetDepreciationRequest(org_id=org_id)
    confirm = ConfirmFixedAssetDepreciationRequest(org_id=org_id, idempotency_key="confirm")
    disposal = DisposeFixedAssetRequest(org_id=org_id, idempotency_key="dispose")
    reversal = ReverseEventRequest(
        org_id=org_id,
        event_id=asset_id,
        idempotency_key="reverse",
        reason="correct acquisition facts",
        posting_date=date(2026, 1, 1),
    )

    assert mcp_server.finance_acquire_fixed_asset(acquisition)["status"] == "posted"
    assert mcp_server.finance_activate_fixed_asset(activation)["status"] == "posted"
    assert mcp_server.finance_preview_fixed_asset_depreciation(preview)["status"] == "calculated"
    assert mcp_server.finance_confirm_fixed_asset_depreciation(confirm)["status"] == "posted"
    assert mcp_server.finance_dispose_fixed_asset(disposal)["status"] == "posted"
    assert mcp_server.finance_reverse_event(reversal)["status"] == "reversed"
    assert mcp_server.finance_get_fixed_asset(org_id, asset_id) == {
        "status": "posted",
        "asset_id": str(asset_id),
        "event_id": None,
        "voucher_id": None,
        "voucher_number": None,
        "calculation_hash": None,
        "missing_information": [],
        "errors": [],
        "trace": [],
        "data": {},
    }
    assert [name for name, _ in calls] == [
        "acquire",
        "activate",
        "preview",
        "confirm",
        "dispose",
        "reverse",
        "get",
    ]


def test_acquisition_mcp_handler_posts_to_an_isolated_sqlite_database(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory.begin() as session:
            organization = seed_organization(session, name="MCP fixed asset test")
            evidence = Evidence(
                org_id=organization.id,
                sha256="a" * 64,
                original_name="purchase.pdf",
                media_type="application/pdf",
                source="test",
                size_bytes=1,
                storage_path="test/purchase.pdf",
            )
            session.add(evidence)
            session.flush()
            org_id = organization.id
            evidence_id = evidence.id
        monkeypatch.setattr(mcp_server, "SessionLocal", factory)

        response = mcp_server.finance_acquire_fixed_asset(
            AcquireFixedAssetRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": "mcp-asset-acquisition",
                    "asset_code": "MCP-FA-001",
                    "asset_name": "MCP test device",
                    "category": "electronic",
                    "expected_use_over_one_year": True,
                    "purchase_date": "2026-01-01",
                    "posting_date": "2026-01-01",
                    "cost_components": {
                        "purchase_price_fen": 100_000,
                        "noncreditable_tax_fen": 3_000,
                        "transport_and_handling_fen": 0,
                        "installation_and_direct_cost_fen": 0,
                    },
                    "supplier": {"kind": "supplier", "name": "MCP supplier"},
                    "settlement_method": "payable",
                    "due_date": "2026-02-01",
                    "evidence_references": [evidence_id],
                    "claims_creditable_input_vat": False,
                }
            )
        )

        assert response["status"] == "posted"
        with factory() as session:
            asset = session.scalar(select(FixedAsset).where(FixedAsset.asset_code == "MCP-FA-001"))
            assert asset is not None
            assert asset.cost_fen == 103_000
    finally:
        engine.dispose()


def test_fixed_asset_sale_mcp_returns_tax_period_source_lock_from_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory.begin() as session:
            organization = seed_organization(session, name="MCP tax lock test")
            evidence = Evidence(
                org_id=organization.id,
                sha256="l" * 64,
                original_name="tax-lock.pdf",
                media_type="application/pdf",
                source="test",
                size_bytes=1,
                storage_path="test/tax-lock.pdf",
            )
            session.add(evidence)
            session.flush()
            org_id = organization.id
            evidence_id = evidence.id
        monkeypatch.setattr(mcp_server, "SessionLocal", factory)

        acquired = mcp_server.finance_acquire_fixed_asset(
            AcquireFixedAssetRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": "mcp-tax-lock-acquire",
                    "asset_code": "MCP-TAX-LOCK",
                    "asset_name": "MCP tax lock device",
                    "category": "electronic",
                    "expected_use_over_one_year": True,
                    "purchase_date": "2026-01-02",
                    "posting_date": "2026-01-02",
                    "cost_components": {
                        "purchase_price_fen": 100_000,
                        "noncreditable_tax_fen": 3_000,
                        "transport_and_handling_fen": 0,
                        "installation_and_direct_cost_fen": 0,
                    },
                    "supplier": {"kind": "supplier", "name": "MCP supplier"},
                    "settlement_method": "payable",
                    "due_date": "2026-02-02",
                    "evidence_references": [evidence_id],
                    "claims_creditable_input_vat": False,
                }
            )
        )
        assert acquired["status"] == "posted", acquired
        activated = mcp_server.finance_activate_fixed_asset(
            ActivateFixedAssetRequest.model_validate(
                {
                    "org_id": org_id,
                    "asset_id": acquired["asset_id"],
                    "idempotency_key": "mcp-tax-lock-activate",
                    "activation_date": "2026-01-10",
                    "posting_date": "2026-01-10",
                    "useful_life_months": 13,
                    "residual_value_fen": 0,
                    "benefit_area": "management",
                    "evidence_references": [evidence_id],
                }
            )
        )
        assert activated["status"] == "posted", activated
        source = mcp_server.finance_record_event(
            RecordEventRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": "mcp-tax-lock-source",
                    "event_type": "service_cash_sale",
                    "business_dates": {
                        "business_date": "2026-01-15",
                        "fulfillment_date": "2026-01-15",
                        "payment_date": "2026-01-15",
                        "tax_obligation_date": "2026-01-15",
                        "posting_date": "2026-01-15",
                    },
                    "amounts": {"gross_amount_fen": 101_000},
                    "tax_facts": {
                        "taxable": True,
                        "rate_percent": "1",
                        "invoice_type": "special",
                        "waive_exemption": False,
                        "tax_due_on_event": True,
                    },
                }
            )
        )
        assert source["status"] == "posted", source
        preview_request = TaxPeriodPreviewRequest(
            org_id=org_id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
        )
        preview = mcp_server.finance_calculate_tax_period(preview_request)
        assert preview["status"] == "calculated", preview
        confirmed = mcp_server.finance_confirm_tax_period(
            TaxPeriodConfirmRequest(
                **preview_request.model_dump(),
                calculation_hash=preview["calculation_hash"],
                idempotency_key="mcp-tax-lock-confirm",
            )
        )
        assert confirmed["status"] == "posted", confirmed

        blocked = mcp_server.finance_dispose_fixed_asset(
            DisposeFixedAssetRequest.model_validate(
                {
                    "org_id": org_id,
                    "asset_id": acquired["asset_id"],
                    "idempotency_key": "mcp-tax-lock-dispose",
                    "disposal_date": "2026-01-20",
                    "posting_date": "2026-01-20",
                    "disposal_kind": "sale",
                    "gross_proceeds_fen": 50_000,
                    "invoice_type": "ordinary",
                    "waive_exemption": False,
                    "settlement_method": "receivable",
                    "customer": {"kind": "customer", "name": "MCP customer"},
                    "tax_obligation_date": "2026-01-20",
                    "clearance_cost_fen": 0,
                    "evidence_references": [evidence_id],
                }
            )
        )
        assert blocked["status"] == "rejected"
        assert blocked["errors"] == ["TAX_PERIOD_SOURCE_LOCKED"]
    finally:
        engine.dispose()
