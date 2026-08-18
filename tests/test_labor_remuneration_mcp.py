from __future__ import annotations

import asyncio

from ai_accounting import mcp_server
from ai_accounting.labor_remuneration_schemas import (
    ConfirmLaborRemunerationBatchRequest,
    ConfirmUnifiedPayoutRunRequest,
    EndLaborServicePersonRequest,
    PayLaborWithholdingTaxRequest,
    PreviewLaborRemunerationBatchRequest,
    PreviewUnifiedPayoutRunRequest,
    RegisterLaborServicePersonRequest,
)
from ai_accounting.mcp_server import mcp

LABOR_TOOLS = {
    "finance_register_labor_service_person",
    "finance_end_labor_service_person",
    "finance_preview_labor_remuneration_batch",
    "finance_confirm_labor_remuneration_batch",
    "finance_get_labor_remuneration",
    "finance_preview_unified_payout_run",
    "finance_confirm_unified_payout_run",
    "finance_pay_labor_withholding_tax",
    "finance_confirm_labor_external_declaration",
}


def test_labor_mcp_contract_is_typed_and_has_no_freeform_journal_fields() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    assert LABOR_TOOLS <= tools.keys()

    schema_text = str(
        {
            "person": RegisterLaborServicePersonRequest.model_json_schema(),
            "person_end": EndLaborServicePersonRequest.model_json_schema(),
            "preview": PreviewLaborRemunerationBatchRequest.model_json_schema(),
            "confirm": ConfirmLaborRemunerationBatchRequest.model_json_schema(),
            "payout_preview": PreviewUnifiedPayoutRunRequest.model_json_schema(),
            "payout_confirm": ConfirmUnifiedPayoutRunRequest.model_json_schema(),
            "tax_payment": PayLaborWithholdingTaxRequest.model_json_schema(),
        }
    )
    assert "fixed_fee_fen" in schema_text
    assert "commission_fen" in schema_text
    assert "tax_identity" in schema_text
    assert "income_grouping" in schema_text
    assert "calculation_hash" in schema_text
    assert "debit_fen" not in schema_text
    assert "credit_fen" not in schema_text
    assert "'account_code':" not in schema_text
    assert "withholding_rate" not in schema_text
    assert "quick_deduction_fen" not in schema_text


def test_event_schema_advertises_labor_workflow_separately_from_payroll() -> None:
    capability = mcp_server.finance_get_event_schema()["module_capabilities"]

    assert set(capability["personal_labor_remuneration"]["entry_tools"]) == LABOR_TOOLS
    assert capability["personal_labor_remuneration"]["generic_event_writer"] == ("not_available")
    assert capability["payroll"]["generic_event_writer"] == "not_available"
