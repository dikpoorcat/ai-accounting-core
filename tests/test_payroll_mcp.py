from __future__ import annotations

import asyncio
from datetime import date

from sqlalchemy.orm import Session

from ai_accounting import mcp_server
from ai_accounting.mcp_server import mcp
from ai_accounting.models import BusinessEvent, Counterparty, OpenItem, Organization
from ai_accounting.schemas import (
    ConfirmPayrollRequest,
    PreviewPayrollRequest,
    RecordEventRequest,
    RecordPayrollContributionSupplementRequest,
    RegisterPayrollContributionActualRequest,
)


def test_payroll_mcp_contract_exposes_only_structured_business_facts() -> None:
    tools = {tool.name: tool for tool in asyncio.run(mcp.list_tools())}
    payroll_tools = {
        "finance_register_employee",
        "finance_register_employee_profile_version",
        "finance_register_payroll_policy_version",
        "finance_register_payroll_opening_state",
        "finance_register_payroll_contribution_actual",
        "finance_record_payroll_contribution_supplement",
        "finance_preview_payroll",
        "finance_confirm_payroll",
        "finance_get_payroll_batch",
    }
    assert payroll_tools <= tools.keys()
    preview_schema = PreviewPayrollRequest.model_json_schema()
    confirm_schema = ConfirmPayrollRequest.model_json_schema()
    record_schema = RecordEventRequest.model_json_schema()
    actual_schema = RegisterPayrollContributionActualRequest.model_json_schema()
    supplement_schema = RecordPayrollContributionSupplementRequest.model_json_schema()
    assert "calculation_hash" in confirm_schema["properties"]
    assert "employee_items" in preview_schema["properties"]
    schema_text = str(
        {
            "preview": preview_schema,
            "confirm": confirm_schema,
            "record": record_schema,
            "contribution_actual": actual_schema,
            "contribution_supplement": supplement_schema,
        }
    )
    assert actual_schema["properties"]["evidence_references"]["minItems"] == 1
    assert supplement_schema["properties"]["evidence_references"]["minItems"] == 1
    assert "debit_fen" not in schema_text
    assert "credit_fen" not in schema_text
    assert "'account_code'" not in schema_text


def test_query_context_exposes_payroll_payable_target_metadata(
    session: Session, organization: Organization, monkeypatch: object
) -> None:
    counterparty = Counterparty(org_id=organization.id, kind="other", name="社保局")
    session.add(counterparty)
    session.flush()
    event = BusinessEvent(
        org_id=organization.id,
        idempotency_key="payroll-open-item-context",
        event_type="payroll_accrual",
        status="posted",
        facts={},
        business_date=date(2026, 8, 31),
        posting_date=date(2026, 8, 31),
    )
    session.add(event)
    session.flush()
    session.add(
        OpenItem(
            org_id=organization.id,
            counterparty_id=counterparty.id,
            source_event_id=event.id,
            item_type="payable",
            original_amount_fen=10_000,
            payable_category="employer_social",
            payable_agency_code="SOCIAL-01",
            insurance_kind="pension",
        )
    )
    session.flush()

    class SessionContext:
        def __enter__(self) -> Session:
            return session

        def __exit__(self, *_: object) -> bool:
            return False

    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: SessionContext())
    result = mcp_server.finance_query_context(
        str(organization.id), include_recent_events=False, include_unmatched_bank=False
    )
    item = result["open_items"][0]
    assert item["payable_category"] == "employer_social"
    assert item["payable_agency_code"] == "SOCIAL-01"
    assert item["insurance_kind"] == "pension"
