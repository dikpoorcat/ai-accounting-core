from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import select

from ai_accounting.accounting_period_schemas import (
    AccountingPeriodReviewFacts,
    ConfirmAccountingPeriodCloseRequest,
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.models import (
    AccountingPeriod,
    AccountingPeriodClose,
    BusinessEvent,
    Evidence,
    PayrollBatch,
    Voucher,
)
from ai_accounting.schemas import (
    RegisterEmployeePayrollProfileVersionRequest,
    RegisterEmployeeRequest,
    RegisterPayrollPolicyVersionRequest,
)
from ai_accounting.service import FinanceService

PERIOD_TOOLS = {
    "finance_generate_accounting_period",
    "finance_preview_accounting_period_close",
    "finance_confirm_accounting_period_close",
    "finance_get_accounting_periods",
}


def _payroll_parameters() -> dict[str, Any]:
    return {
        "contribution_rules": [
            {
                "code": "pension",
                "base_kind": "social_insurance",
                "employee_rate": "0.08",
                "employer_rate": "0.16",
                "minimum_base_fen": 0,
                "maximum_base_fen": 10_000_000,
                "rounding_rule": "half_up",
            },
            {
                "code": "housing_fund",
                "base_kind": "housing_fund",
                "employee_rate": "0.07",
                "employer_rate": "0.07",
                "minimum_base_fen": 0,
                "maximum_base_fen": 10_000_000,
                "rounding_rule": "half_up",
            },
        ],
        "income_tax": {
            "version": "period-stdio-income-tax-2026",
            "primary_source_url": "https://www.chinatax.gov.cn/",
            "legal_basis_source_url": "https://www.chinatax.gov.cn/",
            "effective_from": "2026-01-01",
            "effective_to": "2026-12-31",
            "monthly_standard_deduction_fen": 500_000,
            "brackets": [
                {
                    "upper_bound_fen": None,
                    "rate": "0.03",
                    "quick_deduction_fen": 0,
                }
            ],
        },
        "annual_bonus": {
            "version": "period-stdio-bonus-2026",
            "primary_source_url": "https://www.mof.gov.cn/",
            "effective_from": "2026-01-01",
            "effective_to": "2027-12-31",
            "brackets": [
                {
                    "upper_monthly_average_fen": None,
                    "rate": "0.03",
                    "quick_deduction_fen": 0,
                }
            ],
        },
        "payment_targets": {
            "social_insurance": {"agency_code": "SOCIAL-01", "agency_name": "社保局"},
            "housing_fund": {"agency_code": "HOUSING-01", "agency_name": "公积金中心"},
            "individual_income_tax": {"agency_code": "TAX-01", "agency_name": "税务局"},
        },
    }


def _environment(database_url: str, evidence_directory: Path) -> dict[str, str]:
    repository_root = Path(__file__).parents[1]
    site_packages = Path(sys.prefix) / "Lib" / "site-packages"
    environment = os.environ.copy()
    environment["DATABASE_URL"] = database_url
    environment["FINANCE_EVIDENCE_DIR"] = str(evidence_directory)
    environment["PYTHONPATH"] = os.pathsep.join(
        filter(
            None,
            [
                str(repository_root / "src"),
                str(site_packages),
                str(site_packages / "win32"),
                str(site_packages / "win32" / "lib"),
                str(site_packages / "pywin32_system32"),
                environment.get("PYTHONPATH"),
            ],
        )
    )
    return environment


def _object_schemas(schema: dict[str, Any]) -> list[dict[str, Any]]:
    objects: list[dict[str, Any]] = []

    def visit(value: Any) -> None:
        if isinstance(value, dict):
            if value.get("type") == "object":
                objects.append(value)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(schema)
    return objects


async def _call(
    client: ClientSession, name: str, request: dict[str, Any]
) -> dict[str, Any]:
    response = await client.call_tool(name, {"request": request})
    assert response.isError is False
    assert len(response.content) == 1
    return json.loads(response.content[0].text)


def test_accounting_period_real_stdio_closes_and_corrects_in_next_open_month(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "accounting-period-stdio.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    setup_engine = make_engine(database_url)
    Base.metadata.create_all(setup_engine)
    setup_factory = make_session_factory(setup_engine)
    with setup_factory.begin() as database_session:
        organization = seed_organization(database_session, name="期间 STDIO 验收企业")
        assert organization.accounting_period_control_enabled is True
        assert organization.accounting_period_control_start_date is None
        evidence = Evidence(
            org_id=organization.id,
            sha256="c" * 64,
            original_name="period-stdio.txt",
            media_type="text/plain",
            source="stdio-test",
            size_bytes=1,
            storage_path="stdio/period-stdio.txt",
        )
        database_session.add(evidence)
        database_session.flush()
        org_id = str(organization.id)
        evidence_id = str(evidence.id)
    setup_engine.dispose()

    parameters = StdioServerParameters(
        command=getattr(sys, "_base_executable", sys.executable),
        args=["-m", "ai_accounting.mcp_server"],
        cwd=Path(__file__).parents[1],
        env=_environment(database_url, tmp_path / "evidence"),
    )

    async def first_session() -> dict[str, Any]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert PERIOD_TOOLS <= tools.keys()
                schemas = {name: tools[name].inputSchema for name in PERIOD_TOOLS}
                assert all(
                    object_schema.get("additionalProperties") is False
                    for schema in schemas.values()
                    for object_schema in _object_schemas(schema)
                )
                schema_text = json.dumps(schemas, ensure_ascii=False)
                for forbidden in ("entries", "debit_fen", "credit_fen", "account_code"):
                    assert forbidden not in schema_text

                generation = {
                    "org_id": org_id,
                    "period_month": "2026-07",
                    "idempotency_key": "stdio-period-generate-july",
                    "confirmation_note": "STDIO 显式生成七月期间",
                    "evidence_references": [evidence_id],
                }
                generated = await _call(
                    client, "finance_generate_accounting_period", generation
                )
                assert generated["status"] == "posted", generated
                replay = await _call(
                    client, "finance_generate_accounting_period", generation
                )
                assert replay["period_id"] == generated["period_id"]
                assert replay["data"]["idempotent_replay"] is True

                before_control_start = await _call(
                    client,
                    "finance_record_event",
                    {
                        "org_id": org_id,
                        "idempotency_key": "stdio-period-before-control-start",
                        "event_type": "service_cash_sale",
                        "business_dates": {
                            "business_date": "2026-06-30",
                            "posting_date": "2026-06-30",
                            "fulfillment_date": "2026-06-30",
                            "payment_date": "2026-06-30",
                            "tax_obligation_date": "2026-06-30",
                        },
                        "amounts": {"gross_amount_fen": 101_000},
                        "tax_facts": {
                            "taxable": True,
                            "rate_percent": "1",
                            "invoice_type": "ordinary",
                            "waive_exemption": False,
                            "tax_due_on_event": True,
                        },
                    },
                )
                assert before_control_start["errors"] == [
                    "ACCOUNTING_PERIOD_NOT_GENERATED"
                ]

                sale_request = {
                    "org_id": org_id,
                    "idempotency_key": "stdio-period-july-sale",
                    "event_type": "service_cash_sale",
                    "business_dates": {
                        "business_date": "2026-07-15",
                        "posting_date": "2026-07-15",
                        "fulfillment_date": "2026-07-15",
                        "payment_date": "2026-07-15",
                        "tax_obligation_date": "2026-07-15",
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
                sale = await _call(client, "finance_record_event", sale_request)
                assert sale["status"] == "posted", sale
                fixed_asset_request = {
                    "org_id": org_id,
                    "idempotency_key": "stdio-period-july-fixed-asset",
                    "asset_code": "FA-PERIOD-STDIO",
                    "asset_name": "期间控制固定资产",
                    "category": "electronic",
                    "expected_use_over_one_year": True,
                    "purchase_date": "2026-07-10",
                    "posting_date": "2026-07-10",
                    "cost_components": {
                        "purchase_price_fen": 100_000,
                        "noncreditable_tax_fen": 3_000,
                        "transport_and_handling_fen": 0,
                        "installation_and_direct_cost_fen": 0,
                    },
                    "supplier": {"kind": "supplier", "name": "期间资产供应商"},
                    "settlement_method": "payable",
                    "due_date": "2026-08-10",
                    "evidence_references": [evidence_id],
                    "claims_creditable_input_vat": False,
                }
                fixed_asset = await _call(
                    client, "finance_acquire_fixed_asset", fixed_asset_request
                )
                assert fixed_asset["status"] == "posted", fixed_asset

                close_facts = {
                    "org_id": org_id,
                    "period_id": generated["period_id"],
                    "closing_date": "2026-07-31",
                }
                preview = await _call(
                    client, "finance_preview_accounting_period_close", close_facts
                )
                assert preview["status"] == "calculated", preview
                confirmed = await _call(
                    client,
                    "finance_confirm_accounting_period_close",
                    {
                        **close_facts,
                        "calculation_hash": preview["calculation_hash"],
                        "idempotency_key": "stdio-period-close-july",
                        "review_facts": {
                            "voucher_completeness_reviewed": True,
                            "bank_reconciliation_reviewed": True,
                            "open_items_reviewed": True,
                            "payroll_and_statutory_items_reviewed": True,
                            "tax_items_reviewed": True,
                            "asset_and_borrowing_schedules_reviewed": True,
                        },
                        "confirmation_note": "STDIO 完成七月非零凭证关账",
                        "evidence_references": [evidence_id],
                    },
                )
                assert confirmed["status"] == "posted", confirmed

                same_month = await _call(
                    client,
                    "finance_record_event",
                    {
                        **sale_request,
                        "idempotency_key": "stdio-period-after-close",
                        "business_dates": {
                            **sale_request["business_dates"],
                            "business_date": "2026-07-20",
                            "posting_date": "2026-07-20",
                            "fulfillment_date": "2026-07-20",
                            "payment_date": "2026-07-20",
                            "tax_obligation_date": "2026-07-20",
                        },
                    },
                )
                assert same_month["errors"] == ["ACCOUNTING_PERIOD_CLOSED"]
                specialized_write = await _call(
                    client,
                    "finance_acquire_fixed_asset",
                    {
                        **fixed_asset_request,
                        "idempotency_key": "stdio-period-fixed-after-close",
                        "asset_code": "FA-PERIOD-CLOSED",
                        "purchase_date": "2026-07-20",
                        "posting_date": "2026-07-20",
                    },
                )
                assert specialized_write["errors"] == ["ACCOUNTING_PERIOD_CLOSED"]
                specialized_reversal = await _call(
                    client,
                    "finance_reverse_event",
                    {
                        "org_id": org_id,
                        "event_id": fixed_asset["event_id"],
                        "idempotency_key": "stdio-period-fixed-reverse-closed",
                        "reason": "关闭月不得冲正固定资产",
                        "posting_date": "2026-07-31",
                    },
                )
                assert specialized_reversal["errors"] == ["ACCOUNTING_PERIOD_CLOSED"]
                july_state = await _call(
                    client,
                    "finance_get_accounting_periods",
                    {"org_id": org_id, "period_month": "2026-07"},
                )
                assert july_state["data"]["periods"][0]["status"] == "closed"

                august = await _call(
                    client,
                    "finance_generate_accounting_period",
                    {
                        **generation,
                        "period_month": "2026-08",
                        "idempotency_key": "stdio-period-generate-august",
                        "confirmation_note": "STDIO 连续生成八月期间",
                    },
                )
                assert august["status"] == "posted", august
                return {
                    "sale_event_id": sale["event_id"],
                    "fixed_asset_event_id": fixed_asset["event_id"],
                    "close_id": confirmed["close_id"],
                    "close_hash": preview["calculation_hash"],
                }

    async def second_session(result: dict[str, Any]) -> dict[str, Any]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                periods = await _call(
                    client, "finance_get_accounting_periods", {"org_id": org_id}
                )
                assert [row["status"] for row in periods["data"]["periods"]] == [
                    "closed",
                    "open",
                ]
                reversal = await _call(
                    client,
                    "finance_reverse_event",
                    {
                        "org_id": org_id,
                        "event_id": result["sale_event_id"],
                        "idempotency_key": "stdio-period-august-reversal",
                        "reason": "在后续开放月更正七月业务",
                        "posting_date": "2026-08-01",
                    },
                )
                assert reversal["status"] == "posted", reversal
                fixed_asset_reversal = await _call(
                    client,
                    "finance_reverse_event",
                    {
                        "org_id": org_id,
                        "event_id": result["fixed_asset_event_id"],
                        "idempotency_key": "stdio-period-fixed-reverse-august",
                        "reason": "后续开放月冲正固定资产",
                        "posting_date": "2026-08-02",
                    },
                )
                assert fixed_asset_reversal["status"] == "posted", fixed_asset_reversal
                return {
                    "generic": reversal,
                    "fixed_asset": fixed_asset_reversal,
                }

    result = asyncio.run(first_session())
    reversal_result = asyncio.run(second_session(result))

    verification_engine = make_engine(database_url)
    verification_factory = make_session_factory(verification_engine)
    try:
        with verification_factory() as database_session:
            periods = database_session.scalars(
                select(AccountingPeriod)
                .where(AccountingPeriod.org_id == uuid.UUID(org_id))
                .order_by(AccountingPeriod.start_date)
            ).all()
            assert [period.status for period in periods] == ["closed", "open"]
            close = database_session.get(
                AccountingPeriodClose, uuid.UUID(result["close_id"])
            )
            assert close.calculation_hash == result["close_hash"]
            assert close.calculation_payload
            source = database_session.get(
                BusinessEvent, uuid.UUID(result["sale_event_id"])
            )
            reversal = database_session.get(
                BusinessEvent, uuid.UUID(reversal_result["generic"]["event_id"])
            )
            voucher = database_session.get(
                Voucher, uuid.UUID(reversal_result["generic"]["voucher_id"])
            )
            fixed_asset_source = database_session.get(
                BusinessEvent, uuid.UUID(result["fixed_asset_event_id"])
            )
            fixed_asset_reversal = database_session.get(
                BusinessEvent, uuid.UUID(reversal_result["fixed_asset"]["event_id"])
            )
            fixed_asset_voucher = database_session.get(
                Voucher, uuid.UUID(reversal_result["fixed_asset"]["voucher_id"])
            )
            assert source.status == "reversed"
            assert reversal.status == "posted"
            assert voucher.posting_date.isoformat() == "2026-08-01"
            assert fixed_asset_source.status == "reversed"
            assert fixed_asset_reversal.status == "posted"
            assert fixed_asset_voucher.posting_date.isoformat() == "2026-08-02"
    finally:
        verification_engine.dispose()


def test_zero_voucher_month_closes_through_real_stdio(tmp_path: Path) -> None:
    database_path = tmp_path / "accounting-period-zero-stdio.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    setup_engine = make_engine(database_url)
    Base.metadata.create_all(setup_engine)
    setup_factory = make_session_factory(setup_engine)
    with setup_factory.begin() as database_session:
        organization = seed_organization(database_session, name="零凭证期间 STDIO 企业")
        evidence = Evidence(
            org_id=organization.id,
            sha256="z" * 64,
            original_name="zero-period-stdio.txt",
            media_type="text/plain",
            source="stdio-test",
            size_bytes=1,
            storage_path="stdio/zero-period-stdio.txt",
        )
        database_session.add(evidence)
        database_session.flush()
        org_id = str(organization.id)
        evidence_id = str(evidence.id)
    setup_engine.dispose()

    parameters = StdioServerParameters(
        command=getattr(sys, "_base_executable", sys.executable),
        args=["-m", "ai_accounting.mcp_server"],
        cwd=Path(__file__).parents[1],
        env=_environment(database_url, tmp_path / "zero-evidence"),
    )

    async def run() -> dict[str, Any]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                generated = await _call(
                    client,
                    "finance_generate_accounting_period",
                    {
                        "org_id": org_id,
                        "period_month": "2026-06",
                        "idempotency_key": "stdio-zero-generate-june",
                        "confirmation_note": "显式确认六月无业务",
                        "evidence_references": [evidence_id],
                    },
                )
                assert generated["status"] == "posted", generated
                close_facts = {
                    "org_id": org_id,
                    "period_id": generated["period_id"],
                    "closing_date": "2026-06-30",
                }
                preview = await _call(
                    client, "finance_preview_accounting_period_close", close_facts
                )
                assert preview["data"]["calculation"]["voucher_sources"] == []
                confirmed = await _call(
                    client,
                    "finance_confirm_accounting_period_close",
                    {
                        **close_facts,
                        "calculation_hash": preview["calculation_hash"],
                        "idempotency_key": "stdio-zero-close-june",
                        "review_facts": {
                            "voucher_completeness_reviewed": True,
                            "bank_reconciliation_reviewed": True,
                            "open_items_reviewed": True,
                            "payroll_and_statutory_items_reviewed": True,
                            "tax_items_reviewed": True,
                            "asset_and_borrowing_schedules_reviewed": True,
                        },
                        "confirmation_note": "确认零凭证月份完整复核",
                        "evidence_references": [evidence_id],
                    },
                )
                assert confirmed["status"] == "posted", confirmed
                assert confirmed["data"]["calculation"]["voucher_sources"] == []
                return confirmed

    confirmed = asyncio.run(run())
    verification_engine = make_engine(database_url)
    verification_factory = make_session_factory(verification_engine)
    try:
        with verification_factory() as database_session:
            close = database_session.get(
                AccountingPeriodClose, uuid.UUID(confirmed["close_id"])
            )
            assert (
                close.voucher_count,
                close.line_count,
                close.total_debit_fen,
                close.total_credit_fen,
            ) == (0, 0, 0, 0)
            period = database_session.get(
                AccountingPeriod, uuid.UUID(confirmed["period_id"])
            )
            assert period.status == "closed"
    finally:
        verification_engine.dispose()


def test_real_stdio_uses_china_current_date_for_posting_boundary(tmp_path: Path) -> None:
    china_today = datetime.now(ZoneInfo("Asia/Shanghai")).date()
    china_tomorrow = china_today + timedelta(days=1)
    database_path = tmp_path / "accounting-period-china-date-stdio.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    setup_engine = make_engine(database_url)
    Base.metadata.create_all(setup_engine)
    setup_factory = make_session_factory(setup_engine)
    with setup_factory.begin() as database_session:
        organization = seed_organization(database_session, name="中国日期 STDIO 企业")
        evidence = Evidence(
            org_id=organization.id,
            sha256="y" * 64,
            original_name="china-date-stdio.txt",
            media_type="text/plain",
            source="stdio-test",
            size_bytes=1,
            storage_path="stdio/china-date-stdio.txt",
        )
        database_session.add(evidence)
        database_session.flush()
        org_id = str(organization.id)
        evidence_id = str(evidence.id)
    setup_engine.dispose()

    parameters = StdioServerParameters(
        command=getattr(sys, "_base_executable", sys.executable),
        args=["-m", "ai_accounting.mcp_server"],
        cwd=Path(__file__).parents[1],
        env=_environment(database_url, tmp_path / "china-date-evidence"),
    )

    async def run() -> None:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                generated = await _call(
                    client,
                    "finance_generate_accounting_period",
                    {
                        "org_id": org_id,
                        "period_month": china_today.strftime("%Y-%m"),
                        "idempotency_key": "stdio-china-date-generation",
                        "confirmation_note": "按中国当前日期生成",
                        "evidence_references": [evidence_id],
                    },
                )
                assert generated["status"] == "posted", generated

                def sale(key: str, posting_date: str) -> dict[str, Any]:
                    return {
                        "org_id": org_id,
                        "idempotency_key": key,
                        "event_type": "service_cash_sale",
                        "business_dates": {
                            "business_date": posting_date,
                            "posting_date": posting_date,
                            "fulfillment_date": posting_date,
                            "payment_date": posting_date,
                            "tax_obligation_date": posting_date,
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

                current = await _call(
                    client,
                    "finance_record_event",
                    sale("stdio-china-date-current", china_today.isoformat()),
                )
                future = await _call(
                    client,
                    "finance_record_event",
                    sale("stdio-china-date-future", china_tomorrow.isoformat()),
                )
                assert current["status"] == "posted", current
                assert future["status"] == "rejected"
                assert future["errors"] == [
                    "ACCOUNTING_PERIOD_FUTURE_POSTING_NOT_ALLOWED"
                ]
                assert future["voucher_id"] is None

    asyncio.run(run())


def test_real_stdio_payroll_preview_rejects_closed_and_not_generated_without_batch(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "accounting-period-payroll-preview-stdio.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    setup_engine = make_engine(database_url)
    Base.metadata.create_all(setup_engine)
    setup_factory = make_session_factory(setup_engine)
    with setup_factory.begin() as database_session:
        closed_org = seed_organization(database_session, name="工资预览关闭月 STDIO")
        not_generated_org = seed_organization(
            database_session, name="工资预览未生成月 STDIO"
        )
        evidence = Evidence(
            org_id=closed_org.id,
            sha256="w" * 64,
            original_name="payroll-close-stdio.txt",
            media_type="text/plain",
            source="stdio-test",
            size_bytes=1,
            storage_path="stdio/payroll-close-stdio.txt",
        )
        database_session.add(evidence)
        database_session.flush()
        period_service = AccountingPeriodService(
            database_session, current_date=date(2026, 8, 11)
        )
        generated = period_service.generate_accounting_period(
            GenerateAccountingPeriodRequest(
                org_id=closed_org.id,
                period_month="2026-07",
                idempotency_key="payroll-stdio-generate-july",
                confirmation_note="生成工资测试七月",
                evidence_references=[evidence.id],
            )
        )
        close_facts = PreviewAccountingPeriodCloseRequest(
            org_id=closed_org.id,
            period_id=generated.period_id,
            closing_date=date(2026, 7, 31),
        )
        close_preview = period_service.preview_accounting_period_close(close_facts)
        close = period_service.confirm_accounting_period_close(
            ConfirmAccountingPeriodCloseRequest(
                **close_facts.model_dump(),
                calculation_hash=close_preview.calculation_hash,
                idempotency_key="payroll-stdio-close-july",
                review_facts=AccountingPeriodReviewFacts(
                    voucher_completeness_reviewed=True,
                    bank_reconciliation_reviewed=True,
                    open_items_reviewed=True,
                    payroll_and_statutory_items_reviewed=True,
                    tax_items_reviewed=True,
                    asset_and_borrowing_schedules_reviewed=True,
                ),
                confirmation_note="关闭工资测试七月",
                evidence_references=[evidence.id],
            )
        )
        assert close.status == "posted"

        def register_payroll_facts(org_id: uuid.UUID, suffix: str) -> str:
            service = FinanceService(database_session)
            employee = service.register_employee(
                RegisterEmployeeRequest(
                    org_id=org_id,
                    employee_code=f"E-{suffix}",
                    name=f"工资员工 {suffix}",
                    employment_start_date=date(2026, 7, 1),
                    status="active",
                )
            )
            employee_id = uuid.UUID(employee["employee_id"])
            profile = service.register_employee_payroll_profile_version(
                RegisterEmployeePayrollProfileVersionRequest(
                    org_id=org_id,
                    employee_id=employee_id,
                    effective_from=date(2026, 7, 1),
                    expense_role="payroll_management_expense",
                    social_insurance_base_fen=1_000_000,
                    housing_fund_base_fen=1_000_000,
                    resident_employee=True,
                )
            )
            assert profile["status"] == "registered"
            policy = service.register_payroll_policy_version(
                RegisterPayrollPolicyVersionRequest(
                    org_id=org_id,
                    region=f"STDIO-{suffix}",
                    effective_from=date(2026, 1, 1),
                    effective_to=date(2026, 12, 31),
                    version=f"stdio-{suffix}-2026",
                    source_url="https://www.chinatax.gov.cn/",
                    parameters=_payroll_parameters(),
                )
            )
            assert policy["status"] == "registered"
            return str(employee_id)

        closed_employee_id = register_payroll_facts(closed_org.id, "CLOSED")
        not_generated_employee_id = register_payroll_facts(
            not_generated_org.id, "NOT-GENERATED"
        )
        closed_org_id = str(closed_org.id)
        not_generated_org_id = str(not_generated_org.id)
    setup_engine.dispose()

    parameters = StdioServerParameters(
        command=getattr(sys, "_base_executable", sys.executable),
        args=["-m", "ai_accounting.mcp_server"],
        cwd=Path(__file__).parents[1],
        env=_environment(database_url, tmp_path / "payroll-preview-evidence"),
    )

    async def run() -> None:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()

                def request(org_id: str, employee_id: str, suffix: str) -> dict[str, Any]:
                    return {
                        "org_id": org_id,
                        "idempotency_key": f"stdio-payroll-preview-{suffix}",
                        "batch_kind": "regular",
                        "payroll_period": "2026-07",
                        "posting_date": "2026-07-05",
                        "payment_date": "2026-07-05",
                        "employee_items": [
                            {
                                "employee_id": employee_id,
                                "base_salary_fen": 1_000_000,
                                "performance_pay_fen": 0,
                                "taxable_allowance_fen": 0,
                                "tax_exempt_income_fen": 0,
                                "attendance_deduction_fen": 0,
                                "special_additional_deduction_fen": 0,
                                "other_legal_deduction_fen": 0,
                            }
                        ],
                    }

                closed = await _call(
                    client,
                    "finance_preview_payroll",
                    request(closed_org_id, closed_employee_id, "closed"),
                )
                not_generated = await _call(
                    client,
                    "finance_preview_payroll",
                    request(
                        not_generated_org_id,
                        not_generated_employee_id,
                        "not-generated",
                    ),
                )
                assert closed["status"] == "rejected"
                assert closed["errors"] == ["ACCOUNTING_PERIOD_CLOSED"]
                assert closed["batch_id"] is None
                assert not_generated["status"] == "rejected"
                assert not_generated["errors"] == ["ACCOUNTING_PERIOD_NOT_GENERATED"]
                assert not_generated["batch_id"] is None

    asyncio.run(run())
    verification_engine = make_engine(database_url)
    verification_factory = make_session_factory(verification_engine)
    try:
        with verification_factory() as database_session:
            assert database_session.scalar(select(PayrollBatch)) is None
    finally:
        verification_engine.dispose()
