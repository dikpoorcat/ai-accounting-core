from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import uuid
from copy import deepcopy
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from sqlalchemy import func, select

from ai_accounting.coa import seed_organization
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.models import BusinessEvent, Evidence, FixedAssetDisposal, TaxPeriod, Voucher


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


def _canonical_hash(payload: dict[str, Any]) -> str:
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def test_hardening_codes_and_fixed_asset_source_lock_over_real_stdio(tmp_path: Path) -> None:
    database_path = tmp_path / "tax-hardening-stdio.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    setup_engine = make_engine(database_url)
    Base.metadata.create_all(setup_engine)
    setup_factory = make_session_factory(setup_engine)
    with setup_factory.begin() as database_session:
        organization = seed_organization(database_session, name="税务硬化 STDIO 验收企业")
        organization.accounting_period_control_enabled = False
        database_session.flush()
        evidence = Evidence(
            org_id=organization.id,
            sha256="h" * 64,
            original_name="tax-hardening-stdio.pdf",
            media_type="application/pdf",
            source="hardening-stdio",
            size_bytes=1,
            storage_path="hardening/stdio",
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
        env=_stdio_environment(database_url, tmp_path / "evidence"),
    )

    async def run() -> dict[str, Any]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()

                async def call(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
                    response = await client.call_tool(name, arguments)
                    assert response.isError is False
                    assert len(response.content) == 1
                    return json.loads(response.content[0].text)

                async def active_asset(asset_code: str) -> str:
                    acquired = await call(
                        "finance_acquire_fixed_asset",
                        {
                            "request": {
                                "org_id": org_id,
                                "idempotency_key": f"stdio-hardening-acquire-{asset_code}",
                                "asset_code": asset_code,
                                "asset_name": f"STDIO 硬化资产 {asset_code}",
                                "category": "production_equipment",
                                "expected_use_over_one_year": True,
                                "purchase_date": "2026-01-02",
                                "posting_date": "2026-01-02",
                                "cost_components": {
                                    "purchase_price_fen": 1_000_000,
                                    "noncreditable_tax_fen": 0,
                                    "transport_and_handling_fen": 0,
                                    "installation_and_direct_cost_fen": 0,
                                },
                                "supplier": {
                                    "kind": "supplier",
                                    "name": f"STDIO 供应商 {asset_code}",
                                },
                                "settlement_method": "payable",
                                "due_date": "2026-02-02",
                                "evidence_references": [evidence_id],
                                "claims_creditable_input_vat": False,
                            }
                        },
                    )
                    assert acquired["status"] == "posted", acquired
                    activated = await call(
                        "finance_activate_fixed_asset",
                        {
                            "request": {
                                "org_id": org_id,
                                "asset_id": acquired["asset_id"],
                                "idempotency_key": f"stdio-hardening-activate-{asset_code}",
                                "activation_date": "2026-01-10",
                                "posting_date": "2026-01-10",
                                "useful_life_months": 13,
                                "residual_value_fen": 10_000,
                                "benefit_area": "management",
                                "evidence_references": [evidence_id],
                            }
                        },
                    )
                    assert activated["status"] == "posted", activated
                    return acquired["asset_id"]

                sale_asset_id = await active_asset("FA-STDIO-HARDENING-SALE")
                retirement_asset_id = await active_asset("FA-STDIO-HARDENING-RETIRE")
                source = await call(
                    "finance_record_event",
                    {
                        "request": {
                            "org_id": org_id,
                            "idempotency_key": "stdio-hardening-tax-source",
                            "event_type": "service_credit_sale",
                            "counterparty": {
                                "kind": "customer",
                                "name": "税务硬化 STDIO 客户",
                            },
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
                q1_request = {
                    "org_id": org_id,
                    "start_date": "2026-01-01",
                    "end_date": "2026-03-31",
                    "adjustment_posting_date": "2026-03-31",
                }
                preview = await call(
                    "finance_calculate_tax_period", {"request": q1_request}
                )
                assert preview["status"] == "calculated", preview
                payload = json.loads(preview["calculation_hash_payload"])
                assert _canonical_hash(payload) == preview["calculation_hash"]
                tampered_payload = deepcopy(payload)
                tampered_payload["organization"]["urban_maintenance_rate"] = "0.05000"
                stale = await call(
                    "finance_confirm_tax_period",
                    {
                        "request": {
                            **q1_request,
                            "calculation_hash": _canonical_hash(tampered_payload),
                            "idempotency_key": "stdio-hardening-stale",
                        }
                    },
                )
                assert stale["status"] == "rejected"
                assert stale["errors"] == ["TAX_PERIOD_CALCULATION_STALE"]
                confirmed = await call(
                    "finance_confirm_tax_period",
                    {
                        "request": {
                            **q1_request,
                            "calculation_hash": preview["calculation_hash"],
                            "idempotency_key": "stdio-hardening-confirm",
                        }
                    },
                )
                assert confirmed["status"] == "posted", confirmed

                blocked_sale = await call(
                    "finance_dispose_fixed_asset",
                    {
                        "request": {
                            "org_id": org_id,
                            "asset_id": sale_asset_id,
                            "idempotency_key": "stdio-hardening-sale-locked",
                            "disposal_date": "2026-01-20",
                            "posting_date": "2026-01-20",
                            "disposal_kind": "sale",
                            "gross_proceeds_fen": 500_000,
                            "invoice_type": "ordinary",
                            "waive_exemption": False,
                            "settlement_method": "receivable",
                            "customer": {"kind": "customer", "name": "STDIO 硬化资产客户"},
                            "tax_obligation_date": "2026-01-20",
                            "clearance_cost_fen": 0,
                            "evidence_references": [evidence_id],
                        }
                    },
                )
                assert blocked_sale["status"] == "rejected"
                assert blocked_sale["errors"] == ["TAX_PERIOD_SOURCE_LOCKED"]
                retired = await call(
                    "finance_dispose_fixed_asset",
                    {
                        "request": {
                            "org_id": org_id,
                            "asset_id": retirement_asset_id,
                            "idempotency_key": "stdio-hardening-retirement",
                            "disposal_date": "2026-01-20",
                            "posting_date": "2026-01-20",
                            "disposal_kind": "retirement",
                            "settlement_method": "none",
                            "clearance_cost_fen": 0,
                            "evidence_references": [evidence_id],
                        }
                    },
                )
                assert retired["status"] == "posted", retired

                q2_request = {
                    "org_id": org_id,
                    "start_date": "2026-04-01",
                    "end_date": "2026-06-30",
                    "adjustment_posting_date": "2026-06-30",
                }
                empty_preview = await call(
                    "finance_calculate_tax_period", {"request": q2_request}
                )
                assert empty_preview["source_events"] == []
                no_adjustment = await call(
                    "finance_confirm_tax_period",
                    {
                        "request": {
                            **q2_request,
                            "calculation_hash": empty_preview["calculation_hash"],
                            "idempotency_key": "stdio-hardening-no-adjustment",
                        }
                    },
                )
                assert no_adjustment["status"] == "rejected"
                assert no_adjustment["errors"] == ["TAX_PERIOD_NO_ADJUSTMENT"]
                return {
                    "source": source,
                    "confirmed": confirmed,
                    "blocked_sale": blocked_sale,
                    "retired": retired,
                    "no_adjustment": no_adjustment,
                }

    result = asyncio.run(run())

    verification_engine = make_engine(database_url)
    verification_factory = make_session_factory(verification_engine)
    try:
        with verification_factory() as database_session:
            source_id = uuid.UUID(result["source"]["event_id"])
            confirmation_id = uuid.UUID(result["confirmed"]["event_id"])
            retirement_id = uuid.UUID(result["retired"]["event_id"])
            assert database_session.get(BusinessEvent, source_id).status == "posted"
            assert database_session.get(BusinessEvent, confirmation_id).status == "posted"
            assert database_session.get(BusinessEvent, retirement_id).status == "posted"
            assert database_session.scalar(
                select(func.count()).select_from(TaxPeriod)
            ) == 1
            assert database_session.scalar(select(func.count()).select_from(Voucher)) == 7
            disposals = database_session.scalars(select(FixedAssetDisposal)).all()
            assert len(disposals) == 1
            assert disposals[0].event_id == retirement_id
            assert disposals[0].disposal_kind == "retirement"
            rejected_sale = database_session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.idempotency_key == "stdio-hardening-sale-locked"
                )
            )
            assert rejected_sale is not None
            assert rejected_sale.status == "rejected"
            assert rejected_sale.facts["_decision"]["errors"] == [
                "TAX_PERIOD_SOURCE_LOCKED"
            ]
            assert not rejected_sale.vouchers
            no_adjustment_event = database_session.scalar(
                select(BusinessEvent).where(
                    BusinessEvent.idempotency_key == "stdio-hardening-no-adjustment"
                )
            )
            assert no_adjustment_event is None
    finally:
        verification_engine.dispose()
