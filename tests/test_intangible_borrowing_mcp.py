from __future__ import annotations

import asyncio
import uuid

import pytest
from pydantic import ValidationError

from ai_accounting import mcp_server as mcp_server
from ai_accounting.component_schemas import RecordEventRequest

INTANGIBLE_TOOLS = {
    "finance_acquire_intangible_asset",
    "finance_preview_intangible_asset_amortization",
    "finance_confirm_intangible_asset_amortization",
    "finance_retire_intangible_asset",
    "finance_get_intangible_asset",
}

BORROWING_TOOLS = {
    "finance_draw_borrowing",
    "finance_preview_borrowing_interest",
    "finance_confirm_borrowing_interest",
    "finance_get_borrowing",
}


def test_specialized_tools_publish_strict_typed_contracts() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    assert INTANGIBLE_TOOLS | BORROWING_TOOLS <= tools.keys()

    for name in INTANGIBLE_TOOLS | BORROWING_TOOLS:
        assert tools[name].inputSchema["additionalProperties"] is False

    draw_schema = tools["finance_draw_borrowing"].inputSchema
    request_schema = draw_schema["$defs"]["DrawBorrowingRequest"]
    term_schema = draw_schema["$defs"]["BorrowingTermFacts"]
    assert request_schema["additionalProperties"] is False
    assert term_schema["additionalProperties"] is False
    assert "entries" not in request_schema["properties"]
    assert "account_code" not in request_schema["properties"]

    acquire_schema = tools["finance_acquire_intangible_asset"].inputSchema
    supplier_schema = acquire_schema["$defs"]["IntangibleAssetSupplierReference"]
    assert supplier_schema["additionalProperties"] is False
    assert supplier_schema["properties"]["name"]["anyOf"][0]["maxLength"] == 200
    assert supplier_schema["properties"]["external_ref"]["anyOf"][0]["maxLength"] == 100


def test_specialized_capabilities_replace_legacy_disabled_sentinels() -> None:
    schema = mcp_server.finance_get_event_schema()
    assert {
        "intangible_asset_acquisition",
        "intangible_asset_amortization",
        "intangible_asset_retirement",
        "borrowing_drawdown",
        "borrowing_interest_accrual",
        "borrowing_interest_payment",
        "borrowing_principal_repayment",
    } <= set(schema["component_types"])


def test_generic_event_writer_returns_specialized_workflow_errors() -> None:
    for kind in ("intangible_asset", "loan_interest"):
        with pytest.raises(ValidationError):
            RecordEventRequest.model_validate(
                {
                    "org_id": uuid.uuid4(),
                    "idempotency_key": kind,
                    "posting_date": "2026-08-10",
                    "components": [
                        {
                            "key": "domain",
                            "kind": kind,
                            "business_date": "2026-08-10",
                            "amount_fen": 1,
                        }
                    ],
                }
            )
