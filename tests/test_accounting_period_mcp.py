from __future__ import annotations

import asyncio
import json
import uuid
from datetime import UTC, date, datetime, timedelta
from typing import Any

import pytest
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import SecretStr

from ai_accounting import mcp_server
from ai_accounting.accounting_period_schemas import (
    AccountingPeriodResult,
    AccountingPeriodResultStatus,
    AccountingPeriodReviewFacts,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    GetAccountingPeriodsRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.bank_statement_schemas import (
    ConfirmBankReconciliationScopeRequest,
    PreviewBankReconciliationScopeRequest,
)
from ai_accounting.coa import seed_organization
from ai_accounting.credential_store import InMemoryCredentialStore
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.identity_schemas import OwnerLoginRequest, OwnerProvisionRequest
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import AccountingPeriodCloseApproval, Evidence, OwnerAccount
from ai_accounting.schemas import RecordEventRequest

PERIOD_TOOL_NAMES = {
    "finance_generate_accounting_period",
    "finance_preview_accounting_period_close",
    "finance_confirm_accounting_period_close",
    "finance_get_accounting_periods",
}


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


def _listed_tools() -> dict[str, Any]:
    return {tool.name: tool for tool in asyncio.run(mcp_server.mcp.list_tools())}


def _call_registered_tool(name: str, request: object) -> dict[str, Any]:
    tool = mcp_server.mcp._tool_manager.get_tool(name)
    assert tool is not None
    return tool.fn(request=request)


def test_accounting_period_tools_publish_strict_typed_contracts() -> None:
    tools = _listed_tools()
    assert PERIOD_TOOL_NAMES <= tools.keys()

    for name in PERIOD_TOOL_NAMES:
        assert tools[name].inputSchema["additionalProperties"] is False

    generate_schema = tools["finance_generate_accounting_period"].inputSchema
    generate_request = generate_schema["$defs"]["GenerateAccountingPeriodRequest"]
    assert generate_request["additionalProperties"] is False
    assert generate_request["required"] == ["org_id", "period_month"]

    confirm_schema = tools["finance_confirm_accounting_period_close"].inputSchema
    confirm_request = confirm_schema["$defs"]["ConfirmAccountingPeriodCloseRequest"]
    review_facts = confirm_schema["$defs"]["AccountingPeriodReviewFacts"]
    assert confirm_request["additionalProperties"] is False
    assert review_facts["additionalProperties"] is False
    assert set(confirm_request["required"]) == {"org_id", "period_id", "closing_date"}

    schema_text = json.dumps(
        {name: tools[name].inputSchema for name in PERIOD_TOOL_NAMES},
        ensure_ascii=False,
    )
    for forbidden in ("entries", "debit_fen", "credit_fen", "account_code"):
        assert forbidden not in schema_text

    assert tools["finance_generate_accounting_period"].annotations.readOnlyHint is False
    assert tools["finance_confirm_accounting_period_close"].annotations.readOnlyHint is False
    assert tools["finance_preview_accounting_period_close"].annotations.readOnlyHint is True
    assert tools["finance_get_accounting_periods"].annotations.readOnlyHint is True

    capability = mcp_server.finance_get_event_schema()["module_capabilities"][
        "accounting_period"
    ]
    assert set(capability["entry_tools"]) == PERIOD_TOOL_NAMES
    assert capability["reopen_entry"] == "not_available"


def test_accounting_period_tools_reject_extra_fields_without_echoing_values() -> None:
    async def call(name: str, arguments: dict[str, Any]) -> None:
        await mcp_server.mcp.call_tool(name, arguments)

    invalid_calls = [
        (
            "finance_generate_accounting_period",
            {
                "request": {"org_id": str(uuid.uuid4()), "period_month": "2026-08"},
                "secret": "must-not-echo-this-value",
            },
        ),
        (
            "finance_confirm_accounting_period_close",
            {
                "request": {
                    "org_id": str(uuid.uuid4()),
                    "period_id": str(uuid.uuid4()),
                    "closing_date": "2026-08-31",
                    "review_facts": {"unknown_review": True},
                }
            },
        ),
    ]
    for name, arguments in invalid_calls:
        with pytest.raises(ToolError) as error:
            asyncio.run(call(name, arguments))
        assert "VALIDATION_ERROR" in str(error.value)
        assert "must-not-echo-this-value" not in str(error.value)


def test_accounting_period_tools_delegate_to_period_service(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[str, object]] = []

    class FakeAccountingPeriodService:
        def generate_accounting_period(
            self, request: GenerateAccountingPeriodRequest
        ) -> AccountingPeriodResult:
            calls.append(("generate", request))
            return AccountingPeriodResult(status=AccountingPeriodResultStatus.POSTED)

        def preview_accounting_period_close(
            self, request: PreviewAccountingPeriodCloseRequest
        ) -> AccountingPeriodResult:
            calls.append(("preview", request))
            return AccountingPeriodResult(status=AccountingPeriodResultStatus.CALCULATED)

        def confirm_accounting_period_close(
            self, request: ConfirmAccountingPeriodCloseRequest
        ) -> AccountingPeriodResult:
            calls.append(("confirm", request))
            return AccountingPeriodResult(status=AccountingPeriodResultStatus.POSTED)

        def get_accounting_periods(
            self, request: GetAccountingPeriodsRequest
        ) -> AccountingPeriodResult:
            calls.append(("get", request))
            return AccountingPeriodResult(status=AccountingPeriodResultStatus.CALCULATED)

    service = FakeAccountingPeriodService()
    monkeypatch.setattr(mcp_server, "SessionLocal", _SessionFactory())
    monkeypatch.setattr(mcp_server, "_accounting_period_service", lambda _: service)
    org_id = uuid.uuid4()
    period_id = uuid.uuid4()

    assert mcp_server.finance_generate_accounting_period(
        GenerateAccountingPeriodRequest(org_id=org_id, period_month="2026-08")
    )["status"] == "posted"
    assert mcp_server.finance_preview_accounting_period_close(
        PreviewAccountingPeriodCloseRequest(
            org_id=org_id,
            period_id=period_id,
            closing_date=date(2026, 8, 31),
        )
    )["status"] == "calculated"
    assert mcp_server.finance_confirm_accounting_period_close(
        ConfirmAccountingPeriodCloseRequest(
            org_id=org_id,
            period_id=period_id,
            closing_date=date(2026, 8, 31),
        )
    )["status"] == "posted"
    assert mcp_server.finance_get_accounting_periods(
        GetAccountingPeriodsRequest(org_id=org_id, period_month="2026-08")
    )["status"] == "calculated"
    assert [name for name, _ in calls] == ["generate", "preview", "confirm", "get"]


def test_all_accounting_period_mcp_handlers_run_against_sqlite(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory.begin() as session:
            organization = seed_organization(
                session,
                taxpayer_identification_number="91330106MA1234567T",
                name="期间 MCP SQLite",
            )
            evidence = Evidence(
                org_id=organization.id,
                sha256="p" * 64,
                original_name="period-mcp.txt",
                media_type="text/plain",
                source="test",
                size_bytes=1,
                storage_path="test/period-mcp.txt",
            )
            session.add(evidence)
            session.flush()
            identity = IdentityService(session)
            password = SecretStr("Accounting-Period-MCP-2026!")
            identity.provision_owner(
                OwnerProvisionRequest(
                    org_id=organization.id,
                    login_name="period-mcp-owner",
                    password=password,
                )
            )
            login = identity.authenticate(
                OwnerLoginRequest(login_name="period-mcp-owner", password=password)
            )
            org_id = organization.id
            evidence_id = evidence.id
        monkeypatch.setattr(
            mcp_server,
            "SessionLocal",
            mcp_server._ContextAwareSessionFactory(factory),
        )
        credential_store = InMemoryCredentialStore()
        credential_store.save_session_token(login.session_token)
        mcp_server._set_mcp_credential_store_for_tests(credential_store)

        scope_request = PreviewBankReconciliationScopeRequest(
            org_id=org_id,
            action_type="initial_confirmation",
            accounts=[],
            confirm_zero_accounts=True,
            explanation="MCP 期间测试明确确认当前没有银行账户",
            evidence_references=[evidence_id],
        )
        scope_preview = _call_registered_tool(
            "finance_preview_bank_reconciliation_scope", scope_request
        )
        assert scope_preview["status"] == "calculated", scope_preview
        scope_confirm = _call_registered_tool(
            "finance_confirm_bank_reconciliation_scope",
            ConfirmBankReconciliationScopeRequest.model_validate(
                scope_request.model_dump()
                | {
                    "calculation_hash": scope_preview["calculation_hash"],
                    "idempotency_key": "mcp-period-zero-bank-scope",
                }
            )
        )
        assert scope_confirm["status"] == "posted", scope_confirm

        generated = _call_registered_tool(
            "finance_generate_accounting_period",
            GenerateAccountingPeriodRequest(
                org_id=org_id,
                period_month="2026-07",
                idempotency_key="mcp-generate-2026-07",
                confirmation_note="MCP 显式生成七月",
                evidence_references=[evidence_id],
            )
        )
        assert generated["status"] == "posted", generated
        periods = _call_registered_tool(
            "finance_get_accounting_periods",
            GetAccountingPeriodsRequest(org_id=org_id, period_month="2026-07")
        )
        assert periods["status"] == "calculated"
        assert periods["data"]["period_count"] == 1

        preview_request = PreviewAccountingPeriodCloseRequest(
            org_id=org_id,
            period_id=uuid.UUID(generated["period_id"]),
            closing_date=date(2026, 7, 31),
        )
        preview = _call_registered_tool(
            "finance_preview_accounting_period_close", preview_request
        )
        assert preview["status"] == "calculated", preview
        assert preview["data"]["calculation"]["voucher_sources"] == []
        missing_owner_approval = _call_registered_tool(
            "finance_confirm_accounting_period_close",
            ConfirmAccountingPeriodCloseRequest(
                **preview_request.model_dump(),
                calculation_hash=preview["calculation_hash"],
                idempotency_key="mcp-close-2026-07-without-owner-approval",
                review_facts=AccountingPeriodReviewFacts(
                    voucher_completeness_reviewed=True,
                    bank_reconciliation_reviewed=True,
                    open_items_reviewed=True,
                    payroll_and_statutory_items_reviewed=True,
                    tax_items_reviewed=True,
                    asset_and_borrowing_schedules_reviewed=True,
                ),
                confirmation_note="MCP不得自行确认七月关账",
                evidence_references=[evidence_id],
            ),
        )
        assert missing_owner_approval["status"] == "needs_information"
        assert missing_owner_approval["missing_information"][0]["code"] == (
            "ACCOUNTING_PERIOD_OWNER_APPROVAL_REQUIRED"
        )
        with factory.begin() as session:
            account = session.get(OwnerAccount, login.owner_account_id)
            assert account is not None
            now = datetime.now(UTC)
            approval = AccountingPeriodCloseApproval(
                org_id=org_id,
                period_id=preview_request.period_id,
                owner_account_id=account.id,
                owner_session_id=login.session_id,
                owner_credential_version=account.credential_version,
                calculation_hash=preview["calculation_hash"],
                confirmation_method="local_password_reauthentication",
                confirmed_at=now,
                expires_at=now + timedelta(minutes=30),
            )
            session.add(approval)
            session.flush()
            owner_approval_id = approval.id
        confirmed = _call_registered_tool(
            "finance_confirm_accounting_period_close",
            ConfirmAccountingPeriodCloseRequest(
                **preview_request.model_dump(),
                calculation_hash=preview["calculation_hash"],
                owner_approval_id=owner_approval_id,
                idempotency_key="mcp-close-2026-07",
                review_facts=AccountingPeriodReviewFacts(
                    voucher_completeness_reviewed=True,
                    bank_reconciliation_reviewed=True,
                    open_items_reviewed=True,
                    payroll_and_statutory_items_reviewed=True,
                    tax_items_reviewed=True,
                    asset_and_borrowing_schedules_reviewed=True,
                ),
                confirmation_note="MCP 确认七月关账",
                evidence_references=[evidence_id],
            )
        )
        assert confirmed["status"] == "posted", confirmed
        assert confirmed["data"]["calculation"]["voucher_sources"] == []
        closed = _call_registered_tool(
            "finance_get_accounting_periods",
            GetAccountingPeriodsRequest(org_id=org_id, period_month="2026-07")
        )
        assert closed["data"]["periods"][0]["status"] == "closed"
    finally:
        mcp_server._set_mcp_credential_store_for_tests(None)
        engine.dispose()


def test_mcp_posting_uses_china_current_date_boundary(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    today = date(2026, 8, 11)
    tomorrow = date(2026, 8, 12)
    monkeypatch.setattr("ai_accounting.ledger.china_current_date", lambda: today)
    monkeypatch.setattr(
        "ai_accounting.accounting_period_service.china_current_date", lambda: today
    )
    engine = make_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    try:
        with factory.begin() as session:
            organization = seed_organization(
                session,
                taxpayer_identification_number="91330106MA1234567T",
                name="MCP 中国日期边界",
            )
            evidence = Evidence(
                org_id=organization.id,
                sha256="t" * 64,
                original_name="china-date.txt",
                media_type="text/plain",
                source="test",
                size_bytes=1,
                storage_path="test/china-date.txt",
            )
            session.add(evidence)
            session.flush()
            identity = IdentityService(session)
            password = SecretStr("Accounting-Period-Date-2026!")
            identity.provision_owner(
                OwnerProvisionRequest(
                    org_id=organization.id,
                    login_name="period-date-owner",
                    password=password,
                )
            )
            login = identity.authenticate(
                OwnerLoginRequest(login_name="period-date-owner", password=password)
            )
            org_id = organization.id
            evidence_id = evidence.id
        monkeypatch.setattr(
            mcp_server,
            "SessionLocal",
            mcp_server._ContextAwareSessionFactory(factory),
        )
        credential_store = InMemoryCredentialStore()
        credential_store.save_session_token(login.session_token)
        mcp_server._set_mcp_credential_store_for_tests(credential_store)
        scope_request = PreviewBankReconciliationScopeRequest(
            org_id=org_id,
            action_type="initial_confirmation",
            accounts=[
                {
                    "bank_account_code": "1002",
                    "account_name": "银行存款",
                    "start_date": "2026-08-01",
                }
            ],
            explanation="MCP 日期边界测试明确确认银行账户",
            evidence_references=[evidence_id],
        )
        scope_preview = _call_registered_tool(
            "finance_preview_bank_reconciliation_scope", scope_request
        )
        assert scope_preview["status"] == "calculated", scope_preview
        scope_confirm = _call_registered_tool(
            "finance_confirm_bank_reconciliation_scope",
            ConfirmBankReconciliationScopeRequest.model_validate(
                scope_request.model_dump()
                | {
                    "calculation_hash": scope_preview["calculation_hash"],
                    "idempotency_key": "mcp-china-date-bank-scope",
                }
            )
        )
        assert scope_confirm["status"] == "posted", scope_confirm
        generated = _call_registered_tool(
            "finance_generate_accounting_period",
            GenerateAccountingPeriodRequest(
                org_id=org_id,
                period_month="2026-08",
                idempotency_key="mcp-china-date-generation",
                confirmation_note="验证中国日期",
                evidence_references=[evidence_id],
            )
        )
        assert generated["status"] == "posted"

        def sale_request(key: str, posting_date: date) -> RecordEventRequest:
            value = posting_date.isoformat()
            return RecordEventRequest.model_validate(
                {
                    "org_id": org_id,
                    "idempotency_key": key,
                    "event_type": "service_cash_sale",
                    "bank_account_code": "1002",
                    "business_dates": {
                        "business_date": value,
                        "posting_date": value,
                        "fulfillment_date": value,
                        "payment_date": value,
                        "tax_obligation_date": value,
                    },
                    "amounts": {"gross_amount_fen": 101_000},
                    "tax_facts": {
                        "taxable": True,
                        "rate_percent": "1",
                        "invoice_type": "ordinary",
                        "waive_exemption": False,
                        "tax_due_on_event": True,
                    },
                }
            )

        current = _call_registered_tool(
            "finance_record_event", sale_request("mcp-china-date-current", today)
        )
        future = _call_registered_tool(
            "finance_record_event", sale_request("mcp-china-date-future", tomorrow)
        )
        assert current["status"] == "posted"
        assert future["status"] == "rejected"
        assert future["errors"] == ["ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED"]
        assert future["voucher_id"] is None
    finally:
        mcp_server._set_mcp_credential_store_for_tests(None)
        engine.dispose()
