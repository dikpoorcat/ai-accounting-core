import asyncio
from datetime import date

import pytest
from sqlalchemy import select
from test_enterprise_income_tax import change, confirm, root
from test_financial_statements import _evidence

from ai_accounting import mcp_server
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.enterprise_income_tax import EnterpriseIncomeTaxService
from ai_accounting.enterprise_income_tax_schemas import ConfirmEnterpriseIncomeTaxResultRequest
from ai_accounting.models import BusinessEvent, EnterpriseIncomeTaxResult, Voucher


def test_external_filing_date_does_not_supply_or_limit_recognition(session, organization):
    evidence = _evidence(session, organization, "result-recognition.txt")
    source = root(session, organization, evidence)
    service = EnterpriseIncomeTaxService(session)
    request = change(
        organization,
        evidence,
        source,
        business_date="2026-08-26",
        posting_date="2026-08-26",
        declaration_date=None,
        declared_tax_fen=32704,
    )
    preview = service.preview(request)
    assert preview["status"] == "calculated"
    for filing_date in (date(2026, 5, 1), date(2026, 9, 30)):
        decorated = service.preview(request.model_copy(update={"declaration_date": filing_date}))
        assert decorated["calculation_hash"] == preview["calculation_hash"]
        assert decorated["data"] == preview["data"]
    result, command = confirm(service, request)
    assert result["data"]["expense_adjustment_fen"] == 22704
    assert (
        service.confirm(command.model_copy(update={"declaration_date": date(2026, 9, 30)}))[
            "result_id"
        ]
        == result["result_id"]
    )


@pytest.mark.parametrize(
    "business_date,period,code,component_code",
    [
        (None, None, "CIT_RESULT_RECOGNITION_REQUIRED", "ACCOUNTING_RECOGNITION_REQUIRED"),
        (
            "2026-05-01",
            None,
            "CIT_RESULT_RECOGNITION_BEFORE_TAX_PERIOD_END",
            "CIT_RESULT_RECOGNITION_BEFORE_TAX_PERIOD_END",
        ),
        (
            "2026-08-27",
            None,
            "CIT_RESULT_RECOGNITION_AFTER_POSTING_DATE",
            "CIT_RESULT_RECOGNITION_AFTER_POSTING_DATE",
        ),
        (
            None,
            "2026-08",
            "CIT_RESULT_RECOGNITION_AFTER_POSTING_DATE",
            "RECOGNITION_PERIOD_IN_FUTURE",
        ),
    ],
)
def test_recognition_errors_keep_context_through_all_submission_paths(
    session, organization, business_date, period, code, component_code
):
    evidence = _evidence(session, organization, "date-conflict.txt")
    source = root(session, organization, evidence)
    request = change(
        organization,
        evidence,
        source,
        business_date=business_date,
        recognition_period=period,
        declaration_date="2026-08-26",
        posting_date="2026-08-26",
    )
    command = ConfirmEnterpriseIncomeTaxResultRequest.model_validate(
        request.model_dump() | {"calculation_hash": "0" * 64, "idempotency_key": "bad-date"}
    )
    service = EnterpriseIncomeTaxService(session)
    before = {
        model: list(session.scalars(select(model.id)))
        for model in (BusinessEvent, Voucher, EnterpriseIncomeTaxResult)
    }
    for result in (service.preview(request), service.confirm(command)):
        issue = result["data"]["fact_issues"][0]
        assert issue["code"] == code
        assert issue["expected"] == {
            "recognition_not_before": "2026-06-30",
            "recognition_not_after": "2026-08-26",
        }
        assert issue["alternatives"] == ["business_date", "recognition_period"]
        assert "declaration_date" not in issue["fields"]
        if business_date is None and period is None:
            assert result["missing_information"] == ["business_date_or_recognition_period"]
        else:
            assert result["errors"] == [code]
    component = request.model_dump(
        exclude={"org_id", "posting_date", "declaration_reference", "confirmation_note"}
    ) | {"key": "result", "kind": "enterprise_income_tax_result", "calculation_hash": "0" * 64}
    composed = RecordEventRequest(
        org_id=organization.id,
        posting_date=request.posting_date,
        idempotency_key="component-bad-date",
        evidence_references=[evidence.id],
        components=[component],
    )
    for result in (
        ComponentService(session).preview(composed),
        ComponentService(session).record(composed),
    ):
        issue = result.data["fact_issues"][0]
        assert issue["code"] == component_code
        assert issue["context"]["component_key"] == "result"
        assert "declaration_date" not in issue["fields"]
    for model, ids in before.items():
        assert list(session.scalars(select(model.id))) == ids


@pytest.mark.parametrize("kind", ["expense", "project_cost"])
def test_other_monthly_components_explain_cutoff_without_demanding_exact_day(
    session, organization, kind
):
    evidence = _evidence(session, organization, "monthly-other.txt")
    facts = {"key": "cost", "kind": kind, "recognition_period": "2026-06", "amount_fen": 100}
    if kind == "expense":
        facts |= {"expense_class": "general_expense", "payment_basis": "supplier_credit"}
    else:
        facts |= {
            "project_nature": "purchased_intangible",
            "rights_controlled": True,
            "cost_element": "purchase_price",
        }
    result = ComponentService(session).record(
        RecordEventRequest(
            org_id=organization.id,
            posting_date="2026-06-15",
            idempotency_key="cutoff",
            evidence_references=[evidence.id],
            components=[facts],
        )
    )
    assert result.errors == ["RECOGNITION_PERIOD_IN_FUTURE"]
    issue = result.data["fact_issues"][0]
    assert issue["fields"] == ["components.cost.recognition_period", "posting_date"]
    assert issue["alternatives"] == [
        "components.cost.business_date",
        "components.cost.recognition_period",
    ]
    assert not list(session.scalars(select(BusinessEvent)))


def test_discovery_publishes_field_meaning_and_fact_resolution_contract():
    discovery = mcp_server.finance_get_event_schema(component_type="enterprise_income_tax_result")
    definitions = discovery["component_schema"]["$defs"]
    component = definitions["EnterpriseIncomeTaxResultComponent"]
    assert component["x-recognition-precision"] == ["day", "month"]
    assert definitions["PayrollAccrualComponent"]["x-recognition-precision"] == ["day"]
    assert definitions["BusinessMetadata"]["x-accounting-fact"]["role"] == "management"
    assert (
        definitions["FundsSettlement"]["properties"]["payment_date"]["x-accounting-fact"]["meaning"]
        == "cash_movement"
    )
    tools = {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}
    for tool_name, model_name in (
        ("finance_preview_enterprise_income_tax_result", "PreviewEnterpriseIncomeTaxResultRequest"),
        ("finance_confirm_enterprise_income_tax_result", "ConfirmEnterpriseIncomeTaxResultRequest"),
        (
            "finance_register_payroll_first_wage_tax_treatment",
            "RegisterPayrollFirstWageTaxTreatmentRequest",
        ),
        (
            "finance_register_payroll_contribution_actual",
            "RegisterPayrollContributionActualRequest",
        ),
    ):
        schema = tools[tool_name].inputSchema["$defs"][model_name]
        declaration = schema["properties"]["declaration_date"]
        assert declaration["x-accounting-fact"]["role"] == "management"
        assert "declaration_date" not in schema["required"]
    assert component["properties"]["declaration_date"]["x-accounting-fact"]["role"] == "management"
    contract = discovery["agent_operating_protocol"]["fact_resolution"]
    assert contract["error_code_is_question"] is False
    assert contract["management_missing_blocks_posting"] is False
    assert contract["instruction"] in mcp_server.mcp.instructions
    assert (
        discovery["agent_operating_protocol"]["question_policy"]["fact_resolution_contract"]
        == "agent_operating_protocol.fact_resolution"
    )
    assert "fields" in discovery["fact_issue_schema"]["required"]


def test_actual_funds_date_is_still_required_and_cannot_be_replaced_by_management(
    session, organization
):
    evidence = _evidence(session, organization, "cash-dates.txt")
    request = RecordEventRequest(
        org_id=organization.id,
        posting_date="2026-08-26",
        idempotency_key="cash-date",
        evidence_references=[evidence.id],
        components=[
            {
                "key": "receipt",
                "kind": "pass_through",
                "amount_fen": 100,
                "metadata": {"declaration_date": "2026-08-25"},
            }
        ],
        funds=[
            {
                "key": "cash",
                "account_code": "1001",
                "direction": "receipt",
                "payment_date": "2026-08-27",
                "amount_fen": 100,
                "allocations": [{"component_key": "receipt", "amount_fen": 100}],
            }
        ],
    )
    result = ComponentService(session).record(request)
    assert result.errors == ["FUNDS_PAYMENT_DATE_IN_FUTURE"]
    issue = result.data["fact_issues"][0]
    assert issue["fields"] == ["funds.payment_date", "posting_date"]
    assert issue["actual_values"]["payment_dates"] == ["2026-08-27"]
    assert not list(session.scalars(select(BusinessEvent)))
