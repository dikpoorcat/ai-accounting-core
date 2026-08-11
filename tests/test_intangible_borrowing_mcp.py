from __future__ import annotations

import asyncio
import uuid

from ai_accounting import mcp_server as mcp_server
from ai_accounting.schemas import RecordEventRequest

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
    "finance_pay_borrowing_interest",
    "finance_repay_borrowing_principal",
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
    intangible = mcp_server.finance_get_event_schema("intangible_asset")
    borrowing = mcp_server.finance_get_event_schema("loan_interest")

    assert "intangible_asset" not in intangible["disabled_event_types"]
    assert "loan_interest" not in borrowing["disabled_event_types"]
    assert "intangible_asset" in intangible["internal_event_types"]
    assert "loan_interest" in borrowing["internal_event_types"]
    assert set(intangible["module_capabilities"]["intangible_asset"]["entry_tools"]) == (
        INTANGIBLE_TOOLS
    )
    assert set(borrowing["module_capabilities"]["borrowing"]["entry_tools"]) == BORROWING_TOOLS
    assert intangible["event_requirements"]["workflow"].startswith("specialized")
    assert borrowing["event_requirements"]["workflow"].startswith("specialized")


def test_generic_event_writer_returns_specialized_workflow_errors() -> None:
    common = {
        "org_id": uuid.uuid4(),
        "business_dates": {
            "business_date": "2026-08-10",
            "posting_date": "2026-08-10",
        },
        "amounts": {"amount_fen": 1},
    }
    intangible = mcp_server.finance_record_event(
        RecordEventRequest.model_validate(
            {
                **common,
                "idempotency_key": "generic-intangible",
                "event_type": "intangible_asset",
            }
        )
    )
    borrowing = mcp_server.finance_record_event(
        RecordEventRequest.model_validate(
            {
                **common,
                "idempotency_key": "generic-borrowing",
                "event_type": "loan_interest",
            }
        )
    )
    assert intangible == {
        "status": "rejected",
        "errors": ["INTANGIBLE_ASSET_REQUIRES_SPECIALIZED_WORKFLOW"],
    }
    assert borrowing == {
        "status": "rejected",
        "errors": ["BORROWING_REQUIRES_SPECIALIZED_WORKFLOW"],
    }
