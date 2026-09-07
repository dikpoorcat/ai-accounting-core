from types import SimpleNamespace

from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.orm import sessionmaker
from test_enterprise_income_tax import change
from test_financial_statements import _evidence
from test_identity_mcp_integration import _provision

from ai_accounting import mcp_server
from ai_accounting.credential_store import InMemoryCredentialStore
from ai_accounting.enterprise_income_tax_schemas import (
    ConfirmEnterpriseIncomeTaxResultRequest,
    QueryEnterpriseIncomeTaxRequest,
)
from ai_accounting.execution_attribution import persist_execution_attribution
from ai_accounting.financial_statement_schemas import ConfirmEnterpriseIncomeTaxQuarterRequest
from ai_accounting.identity import ExecutorIdentity, ExecutorKind
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import EnterpriseIncomeTaxResult, ExecutionAttribution, Organization


def test_authenticated_cit_tools_and_attribution(session, monkeypatch):
    import uuid

    org_id, token = _provision(session)
    org = session.get(Organization, org_id)
    org.accounting_period_control_enabled = False
    context = IdentityService(session).authorize_execution(
        session_token=token,
        executor=ExecutorIdentity(
            kind=ExecutorKind.AI_AGENT, executor_name="cit-test", executor_version="1"
        ),
        request_correlation_id=uuid.uuid4(),
    )
    with persist_execution_attribution(
        session, context=context, tool_name="finance_register_evidence"
    ):
        evidence = _evidence(session, org, "cit-mcp.txt")
    session.commit()
    monkeypatch.setattr(
        mcp_server,
        "SessionLocal",
        mcp_server._ContextAwareSessionFactory(
            sessionmaker(bind=session.get_bind(), expire_on_commit=False)
        ),
    )
    monkeypatch.setattr(
        mcp_server, "get_settings", lambda: SimpleNamespace(finance_environment="production")
    )
    store = InMemoryCredentialStore()
    store.save_session_token(SecretStr(token))
    monkeypatch.setattr(mcp_server, "_MCP_CREDENTIAL_STORE", store)
    tools = {tool.name: tool for tool in mcp_server.mcp._tool_manager.list_tools()}
    query = tools["finance_query_enterprise_income_tax"]
    preview = tools["finance_preview_enterprise_income_tax_result"]
    write = tools["finance_confirm_enterprise_income_tax_result"]
    assert query.annotations.readOnlyHint and preview.annotations.readOnlyHint
    assert write.annotations.idempotentHint and not write.annotations.readOnlyHint
    assert write.parameters["additionalProperties"] is False
    first = tools["finance_confirm_enterprise_income_tax_quarter"].fn(
        request=ConfirmEnterpriseIncomeTaxQuarterRequest(
            org_id=org_id,
            year=2026,
            quarter=2,
            treatment="zero",
            amount_fen=0,
            idempotency_key="root",
            confirmation_note="原为零",
            evidence_references=[evidence.id],
        )
    )
    assert first["status"] == "posted", first
    request = change(org, evidence, first["enterprise_income_tax_confirmation_id"])
    calculated = preview.fn(request=request)
    assert calculated["status"] == "calculated", calculated
    command = ConfirmEnterpriseIncomeTaxResultRequest.model_validate(
        request.model_dump()
        | {"calculation_hash": calculated["calculation_hash"], "idempotency_key": "mcp-correct"}
    )
    posted = write.fn(request=command)
    assert posted["status"] == "posted", posted
    assert write.fn(request=command)["data"]["idempotent_replay"]
    sources = query.fn(request=QueryEnterpriseIncomeTaxRequest(org_id=org_id))["data"]["sources"]
    assert sources[0]["source_id"] == posted["result_id"]
    with sessionmaker(bind=session.get_bind())() as check:
        row = check.scalar(select(EnterpriseIncomeTaxResult))
        attribution = check.get(ExecutionAttribution, row.execution_attribution_id)
        assert attribution.tool_name == "finance_confirm_enterprise_income_tax_result"
