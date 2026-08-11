from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import select

from ai_accounting.coa import seed_organization
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.models import (
    BankTransaction,
    BankTransactionMatch,
    BusinessEvent,
    Evidence,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDisposal,
    Voucher,
    VoucherLine,
    event_evidence,
)

FIXED_ASSET_TOOLS = {
    "finance_acquire_fixed_asset",
    "finance_activate_fixed_asset",
    "finance_preview_fixed_asset_depreciation",
    "finance_confirm_fixed_asset_depreciation",
    "finance_dispose_fixed_asset",
    "finance_get_fixed_asset",
}


def _evidence(organization_id: uuid.UUID, seed: str) -> Evidence:
    return Evidence(
        org_id=organization_id,
        sha256=(seed * 64)[:64],
        original_name=f"stdio-{seed}.pdf",
        media_type="application/pdf",
        source="stdio-test",
        size_bytes=1,
        storage_path=f"stdio/{seed}",
    )


def _bank_transaction(
    organization_id: uuid.UUID,
    *,
    amount_fen: int,
    booking_date: date,
    seed: str,
) -> BankTransaction:
    return BankTransaction(
        org_id=organization_id,
        bank_account_code="1002",
        fingerprint=(seed * 64)[:64],
        booking_date=booking_date,
        amount_fen=amount_fen,
        currency="CNY",
        memo=f"stdio-{seed}",
        source_sha256=(("s" + seed) * 64)[:64],
    )


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


def test_fixed_asset_stdio_full_lifecycle_uses_isolated_database(tmp_path: Path) -> None:
    database_path = tmp_path / "stdio-fixed-assets.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    setup_engine = make_engine(database_url)
    Base.metadata.create_all(setup_engine)
    setup_factory = make_session_factory(setup_engine)
    with setup_factory.begin() as database_session:
        organization = seed_organization(database_session, name="固定资产 STDIO 回归企业")
        organization.accounting_period_control_enabled = False
        database_session.flush()
        org_id = organization.id
        acquisition_evidence = _evidence(org_id, "a")
        activation_evidence = _evidence(org_id, "b")
        disposal_evidence = _evidence(org_id, "c")
        acquisition_bank = _bank_transaction(
            org_id,
            amount_fen=-1_050_000,
            booking_date=date(2026, 1, 2),
            seed="acquire",
        )
        disposal_bank = _bank_transaction(
            org_id,
            amount_fen=500_000,
            booking_date=date(2026, 2, 28),
            seed="dispose",
        )
        database_session.add_all(
            [
                acquisition_evidence,
                activation_evidence,
                disposal_evidence,
                acquisition_bank,
                disposal_bank,
            ]
        )
        database_session.flush()
        acquisition_evidence_id = acquisition_evidence.id
        activation_evidence_id = activation_evidence.id
        disposal_evidence_id = disposal_evidence.id
        acquisition_bank_id = acquisition_bank.id
        disposal_bank_id = disposal_bank.id
    setup_engine.dispose()

    async def run_lifecycle() -> dict[str, Any]:
        environment = os.environ.copy()
        source_directory = str(Path(__file__).parents[1] / "src")
        virtualenv_site_packages = Path(sys.prefix) / "Lib" / "site-packages"
        environment["DATABASE_URL"] = database_url
        environment["FINANCE_EVIDENCE_DIR"] = str(tmp_path / "evidence")
        environment["PYTHONPATH"] = os.pathsep.join(
            filter(
                None,
                [
                    source_directory,
                    str(virtualenv_site_packages),
                    str(virtualenv_site_packages / "win32"),
                    str(virtualenv_site_packages / "win32" / "lib"),
                    str(virtualenv_site_packages / "pywin32_system32"),
                    environment.get("PYTHONPATH"),
                ],
            )
        )
        parameters = StdioServerParameters(
            command=getattr(sys, "_base_executable", sys.executable),
            args=["-m", "ai_accounting.mcp_server"],
            cwd=Path(__file__).parents[1],
            env=environment,
        )

        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert FIXED_ASSET_TOOLS <= set(tools)
                schemas = {name: tools[name].inputSchema for name in sorted(FIXED_ASSET_TOOLS)}
                schema_text = json.dumps(schemas, ensure_ascii=False)
                assert "debit_fen" not in schema_text
                assert "credit_fen" not in schema_text
                assert "account_code" not in schema_text
                assert all(
                    object_schema.get("additionalProperties") is False
                    for schema in schemas.values()
                    for object_schema in _object_schemas(schema)
                )

                async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    response = await client.call_tool(name, arguments)
                    assert response.isError is False
                    assert len(response.content) == 1
                    return json.loads(response.content[0].text)

                acquired = await call(
                    "finance_acquire_fixed_asset",
                    {
                        "request": {
                            "org_id": str(org_id),
                            "idempotency_key": "stdio-fixed-asset-acquire",
                            "asset_code": "FA-STDIO-001",
                            "asset_name": "STDIO 测试设备",
                            "category": "electronic",
                            "expected_use_over_one_year": True,
                            "purchase_date": "2026-01-02",
                            "posting_date": "2026-01-02",
                            "cost_components": {
                                "purchase_price_fen": 1_000_000,
                                "noncreditable_tax_fen": 30_000,
                                "transport_and_handling_fen": 10_000,
                                "installation_and_direct_cost_fen": 10_000,
                            },
                            "supplier": {"kind": "supplier", "name": "STDIO 供应商"},
                            "settlement_method": "bank",
                            "payment_date": "2026-01-02",
                            "evidence_references": [str(acquisition_evidence_id)],
                            "bank_transaction_references": [{"id": str(acquisition_bank_id)}],
                            "claims_creditable_input_vat": False,
                        }
                    },
                )
                assert acquired["status"] == "posted", acquired
                asset_id = acquired["asset_id"]

                activated = await call(
                    "finance_activate_fixed_asset",
                    {
                        "request": {
                            "org_id": str(org_id),
                            "asset_id": asset_id,
                            "idempotency_key": "stdio-fixed-asset-activate",
                            "activation_date": "2026-01-10",
                            "posting_date": "2026-01-10",
                            "useful_life_months": 13,
                            "residual_value_fen": 10_000,
                            "benefit_area": "management",
                            "evidence_references": [str(activation_evidence_id)],
                        }
                    },
                )
                assert activated["status"] == "posted", activated

                depreciation_facts = {
                    "org_id": str(org_id),
                    "asset_id": asset_id,
                    "depreciation_period": "2026-02",
                    "posting_date": "2026-02-28",
                }
                preview = await call(
                    "finance_preview_fixed_asset_depreciation",
                    {"request": depreciation_facts},
                )
                assert preview["status"] == "calculated", preview
                assert preview["data"]["depreciation_fen"] == 80_000

                bad_hash = "f" * 64
                secret_note = "TOP_SECRET_CONFIRMATION_NOTE"
                stale = await call(
                    "finance_confirm_fixed_asset_depreciation",
                    {
                        "request": {
                            **depreciation_facts,
                            "idempotency_key": "stdio-fixed-asset-confirm-stale",
                            "calculation_hash": bad_hash,
                            "confirmed_by": "stdio-test",
                            "confirmation_note": secret_note,
                        }
                    },
                )
                assert stale["status"] == "rejected"
                assert stale["errors"] == ["FIXED_ASSET_CALCULATION_STALE"]
                stale_text = json.dumps(stale, ensure_ascii=False)
                assert bad_hash not in stale_text
                assert secret_note not in stale_text

                confirmed = await call(
                    "finance_confirm_fixed_asset_depreciation",
                    {
                        "request": {
                            **depreciation_facts,
                            "idempotency_key": "stdio-fixed-asset-confirm",
                            "calculation_hash": preview["calculation_hash"],
                            "confirmed_by": "stdio-test",
                        }
                    },
                )
                assert confirmed["status"] == "posted", confirmed

                active_asset = await call(
                    "finance_get_fixed_asset",
                    {"org_id": str(org_id), "asset_id": asset_id},
                )
                assert active_asset["status"] == "posted"
                assert active_asset["data"]["state"] == "active"

                disposed = await call(
                    "finance_dispose_fixed_asset",
                    {
                        "request": {
                            "org_id": str(org_id),
                            "asset_id": asset_id,
                            "idempotency_key": "stdio-fixed-asset-dispose",
                            "disposal_date": "2026-02-28",
                            "posting_date": "2026-02-28",
                            "disposal_kind": "sale",
                            "gross_proceeds_fen": 500_000,
                            "invoice_type": "ordinary",
                            "waive_exemption": False,
                            "settlement_method": "bank",
                            "customer": {"kind": "customer", "name": "STDIO 资产客户"},
                            "tax_obligation_date": "2026-02-28",
                            "clearance_cost_fen": 0,
                            "evidence_references": [str(disposal_evidence_id)],
                            "bank_transaction_references": [{"id": str(disposal_bank_id)}],
                        }
                    },
                )
                assert disposed["status"] == "posted", disposed
                assert disposed["data"]["vat_tax_sales_fen"] == 485_437
                assert disposed["data"]["vat_fen"] == 9_709

                event_projection = await call(
                    "finance_get_event",
                    {"org_id": str(org_id), "event_id": disposed["event_id"]},
                )
                assert event_projection["status"] == "ok"
                assert event_projection["event"]["event_status"] == "posted"

                reversal = await call(
                    "finance_reverse_event",
                    {
                        "request": {
                            "org_id": str(org_id),
                            "event_id": disposed["event_id"],
                            "idempotency_key": "stdio-fixed-asset-reverse-disposal",
                            "reason": "STDIO 生命周期验收冲正",
                            "posting_date": "2026-03-01",
                        }
                    },
                )
                assert reversal["status"] == "posted", reversal

                after_reversal = await call(
                    "finance_get_fixed_asset",
                    {"org_id": str(org_id), "asset_id": asset_id},
                )
                assert after_reversal["data"]["state"] == "active"

                return {
                    "schemas": schemas,
                    "acquired": acquired,
                    "activated": activated,
                    "preview": preview,
                    "stale": stale,
                    "confirmed": confirmed,
                    "active_asset": active_asset,
                    "disposed": disposed,
                    "event_projection": event_projection,
                    "reversal": reversal,
                    "after_reversal": after_reversal,
                }

    result = asyncio.run(run_lifecycle())

    verification_engine = make_engine(database_url)
    verification_factory = make_session_factory(verification_engine)
    try:
        with verification_factory() as database_session:
            asset_id = uuid.UUID(result["acquired"]["asset_id"])
            event_ids = {
                name: uuid.UUID(result[name]["event_id"])
                for name in ("acquired", "activated", "confirmed", "disposed", "reversal")
            }
            asset = database_session.get(FixedAsset, asset_id)
            assert asset is not None
            assert asset.cost_fen == 1_050_000
            assert asset.acquisition_event_id == event_ids["acquired"]

            activation = database_session.scalar(
                select(FixedAssetActivation).where(FixedAssetActivation.asset_id == asset_id)
            )
            depreciation = database_session.scalar(
                select(FixedAssetDepreciation).where(FixedAssetDepreciation.asset_id == asset_id)
            )
            disposal = database_session.scalar(
                select(FixedAssetDisposal).where(FixedAssetDisposal.asset_id == asset_id)
            )
            assert activation is not None
            assert depreciation is not None
            assert disposal is not None
            assert activation.event_id == event_ids["activated"]
            assert activation.useful_life_months == 13
            assert depreciation.event_id == event_ids["confirmed"]
            assert depreciation.amount_fen == 80_000
            assert depreciation.calculation_hash == result["preview"]["calculation_hash"]
            assert disposal.event_id == event_ids["disposed"]
            assert disposal.activation_id == activation.id
            assert disposal.vat_tax_sales_fen == 485_437
            assert disposal.vat_fen == 9_709
            assert disposal.book_value_fen == 970_000

            events = {
                event.id: event
                for event in database_session.scalars(
                    select(BusinessEvent).where(BusinessEvent.org_id == org_id)
                ).all()
            }
            assert events[event_ids["acquired"]].status == "posted"
            assert events[event_ids["activated"]].status == "posted"
            assert events[event_ids["confirmed"]].status == "posted"
            assert events[event_ids["disposed"]].status == "reversed"
            assert events[event_ids["disposed"]].reversed_by_event_id == event_ids["reversal"]
            assert events[event_ids["reversal"]].status == "posted"

            stale_event = database_session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.org_id == org_id,
                    BusinessEvent.idempotency_key == "stdio-fixed-asset-confirm-stale",
                )
            )
            assert stale_event is not None
            assert stale_event.status == "rejected"
            assert stale_event.facts["_decision"]["errors"] == ["FIXED_ASSET_CALCULATION_STALE"]
            assert (
                database_session.scalar(select(Voucher).where(Voucher.event_id == stale_event.id))
                is None
            )

            vouchers = database_session.scalars(
                select(Voucher).where(Voucher.event_id.in_(event_ids.values()))
            ).all()
            assert len(vouchers) == 5
            voucher_by_event = {voucher.event_id: voucher for voucher in vouchers}
            for voucher in vouchers:
                lines = database_session.scalars(
                    select(VoucherLine).where(VoucherLine.voucher_id == voucher.id)
                ).all()
                debit = sum(line.debit_fen for line in lines)
                credit = sum(line.credit_fen for line in lines)
                assert debit == credit
                assert debit > 0
            assert (
                voucher_by_event[event_ids["reversal"]].reversal_of_voucher_id
                == voucher_by_event[event_ids["disposed"]].id
            )

            trace_stages = {
                name: {item["stage"] for item in events[event_id].rule_trace}
                for name, event_id in event_ids.items()
            }
            assert {"facts_validated", "entries_created", "normalized_fact_created"} <= (
                trace_stages["acquired"]
            )
            assert {"facts_validated", "entries_created", "normalized_fact_created"} <= (
                trace_stages["activated"]
            )
            assert {"depreciation_calculated", "entries_created"} <= (trace_stages["confirmed"])
            assert {"tax_rule_selected", "entries_created", "normalized_fact_created"} <= (
                trace_stages["disposed"]
            )
            depreciation_trace = next(
                item
                for item in events[event_ids["confirmed"]].rule_trace
                if item["stage"] == "depreciation_calculated"
            )
            assert depreciation_trace["calculation_hash"] == result["preview"]["calculation_hash"]
            tax_trace = next(
                item
                for item in events[event_ids["disposed"]].rule_trace
                if item["stage"] == "tax_rule_selected"
            )
            assert tax_trace["source_url"].startswith("https://")

            evidence_edges = {
                (row.event_id, row.evidence_id, row.relation_kind)
                for row in database_session.execute(
                    select(
                        event_evidence.c.event_id,
                        event_evidence.c.evidence_id,
                        event_evidence.c.relation_kind,
                    ).where(event_evidence.c.org_id == org_id)
                ).all()
            }
            assert (
                event_ids["acquired"],
                acquisition_evidence_id,
                "supporting",
            ) in evidence_edges
            assert (
                event_ids["activated"],
                activation_evidence_id,
                "supporting",
            ) in evidence_edges
            assert (
                event_ids["confirmed"],
                activation_evidence_id,
                "inherited",
            ) in evidence_edges
            assert (
                event_ids["disposed"],
                disposal_evidence_id,
                "supporting",
            ) in evidence_edges
            assert (
                event_ids["reversal"],
                disposal_evidence_id,
                "inherited",
            ) in evidence_edges

            acquisition_bank = database_session.get(BankTransaction, acquisition_bank_id)
            disposal_bank = database_session.get(BankTransaction, disposal_bank_id)
            assert acquisition_bank is not None
            assert disposal_bank is not None
            assert acquisition_bank.matched_event_id == event_ids["acquired"]
            assert disposal_bank.matched_event_id is None
            acquisition_match = database_session.scalar(
                select(BankTransactionMatch).where(
                    BankTransactionMatch.bank_transaction_id == acquisition_bank_id
                )
            )
            disposal_match = database_session.scalar(
                select(BankTransactionMatch).where(
                    BankTransactionMatch.bank_transaction_id == disposal_bank_id
                )
            )
            assert acquisition_match is not None
            assert acquisition_match.invalidated_by_event_id is None
            assert disposal_match is not None
            assert disposal_match.event_id == event_ids["disposed"]
            assert disposal_match.invalidated_by_event_id == event_ids["reversal"]
            assert disposal_match.invalidated_at is not None

            projection = result["event_projection"]
            assert projection["event"]["event_type"] == "fixed_asset_disposal"
            assert projection["event"]["trace"]
            assert projection["vouchers"][0]["lines"]
            assert projection["evidence"][0]["id"] == str(disposal_evidence_id)
            assert (
                result["active_asset"]["data"]["depreciations"][0]["calculation_hash"]
                == result["preview"]["calculation_hash"]
            )
            assert result["after_reversal"]["data"]["state"] == "active"
            assert result["after_reversal"]["data"]["disposal"] is None
            assert (
                result["after_reversal"]["data"]["disposal_history"][0]["event"]["status"]
                == "reversed"
            )
    finally:
        verification_engine.dispose()
