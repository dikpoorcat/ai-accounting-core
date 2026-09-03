from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import select

from ai_accounting.coa import seed_organization
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.models import BusinessEvent, TaxPeriod, Voucher, VoucherLine

TAX_TOOLS = {"finance_calculate_tax_period", "finance_confirm_tax_period"}


def _stdio_environment(database_url: str, evidence_directory: Path) -> dict[str, str]:
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


def _request_schema(schema: dict[str, Any]) -> dict[str, Any]:
    request = schema["properties"]["request"]
    if "$ref" in request:
        return schema["$defs"][request["$ref"].rsplit("/", 1)[-1]]
    if "allOf" in request:
        ref = request["allOf"][0]["$ref"]
        return schema["$defs"][ref.rsplit("/", 1)[-1]]
    return request


def test_tax_stdio_schema_and_persisted_snapshot_chain_uses_new_client_session(
    tmp_path: Path,
) -> None:
    """Exercise the public FastMCP contract, then inspect it from a new client session."""

    database_path = tmp_path / "tax-determinism-stdio.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    setup_engine = make_engine(database_url)
    Base.metadata.create_all(setup_engine)
    setup_factory = make_session_factory(setup_engine)
    with setup_factory.begin() as database_session:
        organization = seed_organization(
            database_session,
            taxpayer_identification_number="91330106MA1234567T",
            name="税务 STDIO 验收企业",
        )
        organization.accounting_period_control_enabled = False
        database_session.flush()
        org_id = str(organization.id)
    setup_engine.dispose()

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "ai_accounting.mcp_server"],
        cwd=Path(__file__).parents[1],
        env=_stdio_environment(database_url, tmp_path / "evidence"),
    )

    async def first_client_session() -> dict[str, Any]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert TAX_TOOLS <= set(tools)
                schemas = {name: tools[name].inputSchema for name in TAX_TOOLS}
                assert all(
                    object_schema.get("additionalProperties") is False
                    for schema in schemas.values()
                    for object_schema in _object_schemas(schema)
                )
                preview_schema = _request_schema(schemas["finance_calculate_tax_period"])
                confirm_schema = _request_schema(schemas["finance_confirm_tax_period"])
                assert set(preview_schema["required"]) == {
                    "org_id",
                    "start_date",
                    "end_date",
                    "adjustment_posting_date",
                }
                assert set(confirm_schema["required"]) == {
                    "org_id",
                    "start_date",
                    "end_date",
                    "adjustment_posting_date",
                    "calculation_hash",
                    "idempotency_key",
                }
                assert "post_adjustment" not in preview_schema.get("properties", {})
                assert "calculation_hash" not in preview_schema.get("properties", {})
                assert "post_adjustment" not in confirm_schema.get("properties", {})

                async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    response = await client.call_tool(name, arguments)
                    assert response.isError is False
                    assert len(response.content) == 1
                    return json.loads(response.content[0].text)

                source = await call(
                    "finance_record_event",
                    {
                        "request": {
                            "org_id": org_id,
                            "idempotency_key": "stdio-tax-source",
                            "event_type": "service_credit_sale",
                            "counterparty": {"kind": "customer", "name": "税务 STDIO 客户"},
                            "business_dates": {
                                "business_date": "2026-01-15",
                                "fulfillment_date": "2026-01-15",
                                "payment_date": "2026-01-15",
                                "tax_obligation_date": "2026-01-15",
                                "posting_date": "2026-01-15",
                            },
                            "amounts": {"gross_amount_fen": 10100},
                            "tax_facts": {
                                "taxable": True,
                                "rate_percent": "1",
                                "invoice_type": "special",
                                "waive_exemption": False,
                                "tax_due_on_event": True,
                            },
                        }
                    },
                )
                assert source["status"] == "posted", source
                preview_request = {
                    "org_id": org_id,
                    "start_date": "2026-01-01",
                    "end_date": "2026-03-31",
                    "adjustment_posting_date": "2026-03-31",
                }
                preview = await call("finance_calculate_tax_period", {"request": preview_request})
                assert preview["status"] == "calculated", preview
                assert preview["calculation_hash"]
                assert preview["source_events"] == [source["event_id"]]
                confirmed = await call(
                    "finance_confirm_tax_period",
                    {
                        "request": {
                            **preview_request,
                            "calculation_hash": preview["calculation_hash"],
                            "idempotency_key": "stdio-tax-confirm",
                        }
                    },
                )
                assert confirmed["status"] == "posted", confirmed
                assert confirmed["data"]["tax_period_id"]
                assert confirmed["data"]["calculation_hash"] == preview["calculation_hash"]
                return {
                    "source": source,
                    "preview": preview,
                    "confirmed": confirmed,
                    "schemas": schemas,
                }

    async def second_client_session(result: dict[str, Any]) -> dict[str, Any]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()

                async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    response = await client.call_tool(name, arguments)
                    assert response.isError is False
                    return json.loads(response.content[0].text)

                source_projection = await call(
                    "finance_get_event",
                    {"org_id": org_id, "event_id": result["source"]["event_id"]},
                )
                confirmed_projection = await call(
                    "finance_get_event",
                    {"org_id": org_id, "event_id": result["confirmed"]["event_id"]},
                )
                assert source_projection["event"]["event_status"] == "posted"
                assert confirmed_projection["event"]["event_status"] == "posted"
                assert confirmed_projection["vouchers"][0]["lines"]
                snapshot_text = json.dumps(confirmed_projection, ensure_ascii=False)
                assert result["source"]["event_id"] in snapshot_text
                assert "source_url" in snapshot_text

                reversal = await call(
                    "finance_reverse_event",
                    {
                        "request": {
                            "org_id": org_id,
                            "event_id": result["confirmed"]["event_id"],
                            "idempotency_key": "stdio-reverse-tax-period",
                            "reason": "STDIO 税期快照冲正",
                            "posting_date": "2026-04-01",
                        }
                    },
                )
                assert reversal["status"] == "posted", reversal
                original_after_reversal = await call(
                    "finance_get_event",
                    {"org_id": org_id, "event_id": result["confirmed"]["event_id"]},
                )
                reversal_projection = await call(
                    "finance_get_event", {"org_id": org_id, "event_id": reversal["event_id"]}
                )
                assert original_after_reversal["event"]["event_status"] == "reversed"
                assert reversal_projection["event"]["event_status"] == "posted"
                return {"reversal": reversal}

    result = asyncio.run(first_client_session())
    verification = asyncio.run(second_client_session(result))

    verification_engine = make_engine(database_url)
    verification_factory = make_session_factory(verification_engine)
    try:
        with verification_factory() as database_session:
            source = database_session.get(BusinessEvent, uuid.UUID(result["source"]["event_id"]))
            confirmation = database_session.get(
                BusinessEvent, uuid.UUID(result["confirmed"]["event_id"])
            )
            reversal = database_session.get(
                BusinessEvent, uuid.UUID(verification["reversal"]["event_id"])
            )
            assert source is not None and source.status == "posted"
            assert confirmation is not None and confirmation.status == "reversed"
            assert reversal is not None and reversal.status == "posted"
            period = database_session.scalar(
                select(TaxPeriod).where(TaxPeriod.adjustment_event_id == confirmation.id)
            )
            assert period is not None
            assert period.status == "reversed"
            calculation_text = json.dumps(period.calculation, ensure_ascii=False)
            assert str(source.id) in calculation_text
            assert result["preview"]["calculation_hash"] in calculation_text
            vouchers = database_session.scalars(
                select(Voucher).where(Voucher.event_id.in_([confirmation.id, reversal.id]))
            ).all()
            assert len(vouchers) == 2
            by_event = {voucher.event_id: voucher for voucher in vouchers}
            assert by_event[reversal.id].reversal_of_voucher_id == by_event[confirmation.id].id
            for voucher in vouchers:
                lines = database_session.scalars(
                    select(VoucherLine).where(VoucherLine.voucher_id == voucher.id)
                ).all()
                assert sum(line.debit_fen for line in lines) == sum(
                    line.credit_fen for line in lines
                )
    finally:
        verification_engine.dispose()
