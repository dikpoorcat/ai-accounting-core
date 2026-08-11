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
    Account,
    BankTransaction,
    BankTransactionMatch,
    Borrowing,
    BorrowingInterestAccrual,
    BorrowingPayment,
    BusinessEvent,
    Evidence,
    IntangibleAsset,
    IntangibleAssetAmortization,
    IntangibleAssetRetirement,
    Voucher,
    VoucherLine,
    event_evidence,
)

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
SPECIALIZED_TOOLS = INTANGIBLE_TOOLS | BORROWING_TOOLS


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


def test_intangible_and_borrowing_stdio_full_lifecycles_use_isolated_database(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "stdio-intangible-borrowing.sqlite3"
    database_url = f"sqlite+pysqlite:///{database_path.as_posix()}"
    setup_engine = make_engine(database_url)
    Base.metadata.create_all(setup_engine)
    setup_factory = make_session_factory(setup_engine)
    with setup_factory.begin() as database_session:
        organization = seed_organization(database_session, name="无形资产与借款 STDIO 验收企业")
        organization.accounting_period_control_enabled = False
        database_session.flush()
        org_id = organization.id
        intangible_acquisition_evidence = _evidence(org_id, "ia-acquire")
        intangible_retirement_evidence = _evidence(org_id, "ia-retire")
        borrowing_contract_evidence = _evidence(org_id, "loan-contract")
        first_interest_evidence = _evidence(org_id, "loan-interest-one")
        second_interest_evidence = _evidence(org_id, "loan-interest-two")
        principal_evidence = _evidence(org_id, "loan-principal")
        intangible_bank = _bank_transaction(
            org_id, amount_fen=-12_000, booking_date=date(2026, 1, 2), seed="ia-bank"
        )
        drawdown_bank = _bank_transaction(
            org_id, amount_fen=1_000_000, booking_date=date(2025, 1, 1), seed="loan-draw"
        )
        first_interest_bank = _bank_transaction(
            org_id, amount_fen=-18_100, booking_date=date(2025, 7, 1), seed="loan-pay-one"
        )
        second_interest_bank = _bank_transaction(
            org_id, amount_fen=-18_400, booking_date=date(2026, 1, 1), seed="loan-pay-two"
        )
        principal_bank = _bank_transaction(
            org_id, amount_fen=-1_000_000, booking_date=date(2026, 1, 1), seed="loan-principal"
        )
        database_session.add_all(
            [
                intangible_acquisition_evidence,
                intangible_retirement_evidence,
                borrowing_contract_evidence,
                first_interest_evidence,
                second_interest_evidence,
                principal_evidence,
                intangible_bank,
                drawdown_bank,
                first_interest_bank,
                second_interest_bank,
                principal_bank,
            ]
        )
        database_session.flush()
        ids = {
            "intangible_acquisition_evidence": intangible_acquisition_evidence.id,
            "intangible_retirement_evidence": intangible_retirement_evidence.id,
            "borrowing_contract_evidence": borrowing_contract_evidence.id,
            "first_interest_evidence": first_interest_evidence.id,
            "second_interest_evidence": second_interest_evidence.id,
            "principal_evidence": principal_evidence.id,
            "intangible_bank": intangible_bank.id,
            "drawdown_bank": drawdown_bank.id,
            "first_interest_bank": first_interest_bank.id,
            "second_interest_bank": second_interest_bank.id,
            "principal_bank": principal_bank.id,
        }
    setup_engine.dispose()

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

    async def call(client: ClientSession, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        response = await client.call_tool(name, arguments)
        assert response.isError is False
        assert len(response.content) == 1
        return json.loads(response.content[0].text)

    async def run_lifecycles() -> dict[str, Any]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                tools = {tool.name: tool for tool in (await client.list_tools()).tools}
                assert SPECIALIZED_TOOLS <= set(tools)
                schemas = {name: tools[name].inputSchema for name in sorted(SPECIALIZED_TOOLS)}
                schema_text = json.dumps(schemas, ensure_ascii=False).lower()
                for forbidden in (
                    '"debit"',
                    '"debit_fen"',
                    '"credit"',
                    '"credit_fen"',
                    '"account_code"',
                    '"role"',
                ):
                    assert forbidden not in schema_text
                assert all(
                    object_schema.get("additionalProperties") is False
                    for schema in schemas.values()
                    for object_schema in _object_schemas(schema)
                )

                generic_facts = {
                    "org_id": str(org_id),
                    "business_dates": {
                        "business_date": "2026-01-01",
                        "posting_date": "2026-01-01",
                    },
                    "amounts": {"amount_fen": 1},
                }
                generic_intangible = await call(
                    client,
                    "finance_record_event",
                    {
                        "request": {
                            **generic_facts,
                            "idempotency_key": "stdio-generic-intangible",
                            "event_type": "intangible_asset",
                        }
                    },
                )
                generic_borrowing = await call(
                    client,
                    "finance_record_event",
                    {
                        "request": {
                            **generic_facts,
                            "idempotency_key": "stdio-generic-borrowing",
                            "event_type": "loan_interest",
                        }
                    },
                )
                assert generic_intangible == {
                    "status": "rejected",
                    "errors": ["INTANGIBLE_ASSET_REQUIRES_SPECIALIZED_WORKFLOW"],
                }
                assert generic_borrowing == {
                    "status": "rejected",
                    "errors": ["BORROWING_REQUIRES_SPECIALIZED_WORKFLOW"],
                }

                intangible_request = {
                    "org_id": str(org_id),
                    "idempotency_key": "stdio-intangible-acquire",
                    "asset_code": "IA-STDIO-001",
                    "asset_name": "STDIO 外购软件许可",
                    "category": "software",
                    "rights_description": "合同约定可单独识别的软件使用权",
                    "supplier": {"kind": "supplier", "name": "STDIO 软件供应商"},
                    "acquisition_date": "2026-01-02",
                    "available_for_use_date": "2026-01-02",
                    "posting_date": "2026-01-02",
                    "cost_components": {
                        "purchase_price_fen": 11_000,
                        "noncreditable_tax_fen": 500,
                        "directly_attributable_cost_fen": 500,
                    },
                    "settlement_method": "bank",
                    "payment_date": "2026-01-02",
                    "benefit_area": "management",
                    "life_basis": "legal_or_contractual",
                    "useful_life_months": 12,
                    "life_basis_explanation": "合同明确约定十二个月许可期",
                    "is_available_for_use": True,
                    "claims_creditable_input_vat": False,
                    "evidence_references": [str(ids["intangible_acquisition_evidence"])],
                    "bank_transaction_references": [{"id": str(ids["intangible_bank"])}],
                }
                acquired = await call(
                    client, "finance_acquire_intangible_asset", {"request": intangible_request}
                )
                assert acquired["status"] == "posted", acquired
                assert acquired["data"]["cost_fen"] == 12_000
                replayed_acquisition = await call(
                    client, "finance_acquire_intangible_asset", {"request": intangible_request}
                )
                assert replayed_acquisition["event_id"] == acquired["event_id"]
                assert replayed_acquisition["data"]["idempotent_replay"] is True
                mismatched_acquisition = await call(
                    client,
                    "finance_acquire_intangible_asset",
                    {"request": {**intangible_request, "asset_name": "篡改的名称"}},
                )
                assert mismatched_acquisition["errors"] == [
                    "INTANGIBLE_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"
                ]

                amortization_facts = {
                    "org_id": str(org_id),
                    "asset_id": acquired["asset_id"],
                    "amortization_period": "2026-01",
                    "posting_date": "2026-01-31",
                }
                amortization_preview = await call(
                    client,
                    "finance_preview_intangible_asset_amortization",
                    {"request": amortization_facts},
                )
                assert amortization_preview["status"] == "calculated", amortization_preview
                assert amortization_preview["data"]["amortization_fen"] == 1_000
                amortized = await call(
                    client,
                    "finance_confirm_intangible_asset_amortization",
                    {
                        "request": {
                            **amortization_facts,
                            "idempotency_key": "stdio-intangible-amortize",
                            "calculation_hash": amortization_preview["calculation_hash"],
                        }
                    },
                )
                assert amortized["status"] == "posted", amortized
                retired = await call(
                    client,
                    "finance_retire_intangible_asset",
                    {
                        "request": {
                            "org_id": str(org_id),
                            "asset_id": acquired["asset_id"],
                            "idempotency_key": "stdio-intangible-retire",
                            "retirement_date": "2026-01-31",
                            "posting_date": "2026-01-31",
                            "gross_proceeds_fen": 0,
                            "compensation_fen": 0,
                            "taxes_and_fees_fen": 0,
                            "residual_proceeds_fen": 0,
                            "evidence_references": [str(ids["intangible_retirement_evidence"])],
                        }
                    },
                )
                assert retired["status"] == "posted", retired
                assert retired["data"]["book_value_fen"] == 11_000
                retired_asset = await call(
                    client,
                    "finance_get_intangible_asset",
                    {"org_id": str(org_id), "asset_id": acquired["asset_id"]},
                )
                assert retired_asset["data"]["retired"] is True

                drawing_request = {
                    "org_id": str(org_id),
                    "idempotency_key": "stdio-borrowing-draw",
                    "borrowing_code": "LOAN-STDIO-001",
                    "contract_name": "STDIO 经营周转借款",
                    "lender": {"name": "STDIO 持牌银行"},
                    "lender_is_licensed_financial_institution": True,
                    "currency": "CNY",
                    "principal_fen": 1_000_000,
                    "drawdown_date": "2025-01-01",
                    "due_date": "2026-01-01",
                    "posting_date": "2025-01-01",
                    "annual_rate_percent": "3.65",
                    "day_count_basis": "actual_365",
                    "interest_due_dates": ["2025-07-01", "2026-01-01"],
                    "capitalization_applicable": False,
                    "purpose_description": "仅用于日常经营周转",
                    "term_facts": {
                        "single_drawdown": True,
                        "fixed_rate": True,
                        "simple_interest": True,
                        "bullet_principal_at_maturity": True,
                        "allows_prepayment": False,
                        "allows_extension": False,
                        "has_penalty_interest": False,
                        "has_financing_fees": False,
                    },
                    "bank_transaction_references": [{"id": str(ids["drawdown_bank"])}],
                    "evidence_references": [str(ids["borrowing_contract_evidence"])],
                }
                drawn = await call(client, "finance_draw_borrowing", {"request": drawing_request})
                assert drawn["status"] == "posted", drawn
                replayed_draw = await call(
                    client, "finance_draw_borrowing", {"request": drawing_request}
                )
                assert replayed_draw["event_id"] == drawn["event_id"]
                assert replayed_draw["data"]["idempotent_replay"] is True
                mismatched_draw = await call(
                    client,
                    "finance_draw_borrowing",
                    {"request": {**drawing_request, "contract_name": "篡改的借款合同"}},
                )
                assert mismatched_draw["errors"] == ["BORROWING_IDEMPOTENCY_PAYLOAD_MISMATCH"]

                first_period = {
                    "org_id": str(org_id),
                    "borrowing_id": drawn["borrowing_id"],
                    "period_start": "2025-01-01",
                    "period_end": "2025-07-01",
                }
                first_preview = await call(
                    client, "finance_preview_borrowing_interest", {"request": first_period}
                )
                assert first_preview["status"] == "calculated", first_preview
                assert first_preview["data"]["interest_fen"] == 18_100
                first_accrual = await call(
                    client,
                    "finance_confirm_borrowing_interest",
                    {
                        "request": {
                            **first_period,
                            "idempotency_key": "stdio-borrowing-accrual-one",
                            "calculation_hash": first_preview["calculation_hash"],
                        }
                    },
                )
                assert first_accrual["status"] == "posted", first_accrual
                first_payment = await call(
                    client,
                    "finance_pay_borrowing_interest",
                    {
                        "request": {
                            "org_id": str(org_id),
                            "borrowing_id": drawn["borrowing_id"],
                            "accrual_event_id": first_accrual["event_id"],
                            "idempotency_key": "stdio-borrowing-pay-one",
                            "payment_date": "2025-07-01",
                            "posting_date": "2025-07-01",
                            "bank_transaction_references": [
                                {"id": str(ids["first_interest_bank"])}
                            ],
                            "evidence_references": [str(ids["first_interest_evidence"])],
                        }
                    },
                )
                assert first_payment["status"] == "posted", first_payment

                second_period = {
                    "org_id": str(org_id),
                    "borrowing_id": drawn["borrowing_id"],
                    "period_start": "2025-07-01",
                    "period_end": "2026-01-01",
                }
                second_preview = await call(
                    client, "finance_preview_borrowing_interest", {"request": second_period}
                )
                assert second_preview["status"] == "calculated", second_preview
                assert second_preview["data"]["interest_fen"] == 18_400
                second_accrual = await call(
                    client,
                    "finance_confirm_borrowing_interest",
                    {
                        "request": {
                            **second_period,
                            "idempotency_key": "stdio-borrowing-accrual-two",
                            "calculation_hash": second_preview["calculation_hash"],
                        }
                    },
                )
                assert second_accrual["status"] == "posted", second_accrual
                second_payment = await call(
                    client,
                    "finance_pay_borrowing_interest",
                    {
                        "request": {
                            "org_id": str(org_id),
                            "borrowing_id": drawn["borrowing_id"],
                            "accrual_event_id": second_accrual["event_id"],
                            "idempotency_key": "stdio-borrowing-pay-two",
                            "payment_date": "2026-01-01",
                            "posting_date": "2026-01-01",
                            "bank_transaction_references": [
                                {"id": str(ids["second_interest_bank"])}
                            ],
                            "evidence_references": [str(ids["second_interest_evidence"])],
                        }
                    },
                )
                assert second_payment["status"] == "posted", second_payment
                repaid = await call(
                    client,
                    "finance_repay_borrowing_principal",
                    {
                        "request": {
                            "org_id": str(org_id),
                            "borrowing_id": drawn["borrowing_id"],
                            "idempotency_key": "stdio-borrowing-repay",
                            "repayment_date": "2026-01-01",
                            "posting_date": "2026-01-01",
                            "bank_transaction_references": [{"id": str(ids["principal_bank"])}],
                            "evidence_references": [str(ids["principal_evidence"])],
                        }
                    },
                )
                assert repaid["status"] == "posted", repaid
                repaid_borrowing = await call(
                    client,
                    "finance_get_borrowing",
                    {"org_id": str(org_id), "borrowing_id": drawn["borrowing_id"]},
                )
                assert repaid_borrowing["data"]["state"] == "repaid"
                assert repaid_borrowing["data"]["unpaid_interest_fen"] == 0

                reversal_events: dict[str, dict[str, Any]] = {}
                for label, event_id in (
                    ("intangible_retirement", retired["event_id"]),
                    ("intangible_amortization", amortized["event_id"]),
                    ("intangible_acquisition", acquired["event_id"]),
                    ("borrowing_principal", repaid["event_id"]),
                    ("borrowing_payment_two", second_payment["event_id"]),
                    ("borrowing_accrual_two", second_accrual["event_id"]),
                    ("borrowing_payment_one", first_payment["event_id"]),
                    ("borrowing_accrual_one", first_accrual["event_id"]),
                    ("borrowing_draw", drawn["event_id"]),
                ):
                    reversed_event = await call(
                        client,
                        "finance_reverse_event",
                        {
                            "request": {
                                "org_id": str(org_id),
                                "event_id": event_id,
                                "idempotency_key": f"stdio-reverse-{label}",
                                "reason": "STDIO 验收按依赖逆序冲正",
                                "posting_date": "2026-01-02",
                            }
                        },
                    )
                    assert reversed_event["status"] == "posted", reversed_event
                    reversal_events[label] = reversed_event

                return {
                    "schemas": schemas,
                    "acquired": acquired,
                    "amortization_preview": amortization_preview,
                    "amortized": amortized,
                    "retired": retired,
                    "drawn": drawn,
                    "first_preview": first_preview,
                    "first_accrual": first_accrual,
                    "first_payment": first_payment,
                    "second_preview": second_preview,
                    "second_accrual": second_accrual,
                    "second_payment": second_payment,
                    "repaid": repaid,
                    "reversal_events": reversal_events,
                }

    async def read_in_new_client(result: dict[str, Any]) -> dict[str, Any]:
        async with stdio_client(parameters) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as client:
                await client.initialize()
                intangible = await call(
                    client,
                    "finance_get_intangible_asset",
                    {"org_id": str(org_id), "asset_id": result["acquired"]["asset_id"]},
                )
                borrowing = await call(
                    client,
                    "finance_get_borrowing",
                    {"org_id": str(org_id), "borrowing_id": result["drawn"]["borrowing_id"]},
                )
                return {"intangible": intangible, "borrowing": borrowing}

    result = asyncio.run(run_lifecycles())
    second_client = asyncio.run(read_in_new_client(result))
    assert second_client["intangible"]["status"] == "reversed"
    assert second_client["intangible"]["data"]["on_book"] is False
    assert second_client["intangible"]["data"]["book_value_fen"] == 0
    assert second_client["borrowing"]["status"] == "reversed"
    assert second_client["borrowing"]["data"]["state"] == "reversed"
    assert second_client["borrowing"]["data"]["outstanding_principal_fen"] == 0

    verification_engine = make_engine(database_url)
    verification_factory = make_session_factory(verification_engine)
    try:
        with verification_factory() as database_session:
            intangible_asset_id = uuid.UUID(result["acquired"]["asset_id"])
            borrowing_id = uuid.UUID(result["drawn"]["borrowing_id"])
            intangible_event_ids = {
                name: uuid.UUID(result[name]["event_id"])
                for name in ("acquired", "amortized", "retired")
            }
            borrowing_event_ids = {
                name: uuid.UUID(result[name]["event_id"])
                for name in (
                    "drawn",
                    "first_accrual",
                    "first_payment",
                    "second_accrual",
                    "second_payment",
                    "repaid",
                )
            }
            reversal_event_ids = {
                name: uuid.UUID(value["event_id"])
                for name, value in result["reversal_events"].items()
            }
            asset = database_session.get(IntangibleAsset, intangible_asset_id)
            borrowing = database_session.get(Borrowing, borrowing_id)
            assert asset is not None
            assert borrowing is not None
            assert asset.cost_fen == 12_000
            assert asset.acquisition_event_id == intangible_event_ids["acquired"]
            assert borrowing.principal_fen == 1_000_000
            assert str(borrowing.annual_rate_percent) == "3.650000"

            amortization = database_session.scalar(
                select(IntangibleAssetAmortization).where(
                    IntangibleAssetAmortization.asset_id == intangible_asset_id
                )
            )
            retirement = database_session.scalar(
                select(IntangibleAssetRetirement).where(
                    IntangibleAssetRetirement.asset_id == intangible_asset_id
                )
            )
            accruals = database_session.scalars(
                select(BorrowingInterestAccrual)
                .where(BorrowingInterestAccrual.borrowing_id == borrowing_id)
                .order_by(BorrowingInterestAccrual.sequence_no)
            ).all()
            payments = database_session.scalars(
                select(BorrowingPayment)
                .where(BorrowingPayment.borrowing_id == borrowing_id)
                .order_by(BorrowingPayment.payment_date, BorrowingPayment.payment_kind)
            ).all()
            assert amortization is not None
            assert retirement is not None
            assert amortization.amount_fen == 1_000
            assert amortization.calculation_hash == result["amortization_preview"][
                "calculation_hash"
            ]
            assert retirement.book_value_fen == 11_000
            assert [(row.actual_days, row.amount_fen) for row in accruals] == [
                (181, 18_100),
                (184, 18_400),
            ]
            assert [row.payment_kind for row in payments] == ["interest", "interest", "principal"]
            assert [row.amount_fen for row in payments] == [18_100, 18_400, 1_000_000]

            original_event_ids = set(intangible_event_ids.values()) | set(
                borrowing_event_ids.values()
            )
            events = {
                event.id: event
                for event in database_session.scalars(
                    select(BusinessEvent).where(BusinessEvent.org_id == org_id)
                ).all()
            }
            assert all(events[event_id].status == "reversed" for event_id in original_event_ids)
            assert all(
                events[event_id].status == "posted"
                for event_id in reversal_event_ids.values()
            )
            assert all(events[event_id].rule_version for event_id in original_event_ids)
            assert all(
                any(
                    item.get("source_url", "").startswith("https://")
                    for item in events[event_id].rule_trace
                )
                for event_id in original_event_ids
            )
            assert any(
                item.get("calculation_hash") == result["amortization_preview"]["calculation_hash"]
                for item in events[intangible_event_ids["amortized"]].rule_trace
            )
            assert any(
                item.get("calculation_hash") == result["first_preview"]["calculation_hash"]
                for item in events[borrowing_event_ids["first_accrual"]].rule_trace
            )

            vouchers = database_session.scalars(
                select(Voucher).where(
                    Voucher.event_id.in_(original_event_ids | set(reversal_event_ids.values()))
                )
            ).all()
            assert len(vouchers) == len(original_event_ids) + len(reversal_event_ids)
            voucher_by_event = {voucher.event_id: voucher for voucher in vouchers}
            for voucher in vouchers:
                lines = database_session.scalars(
                    select(VoucherLine).where(VoucherLine.voucher_id == voucher.id)
                ).all()
                assert sum(line.debit_fen for line in lines) == sum(
                    line.credit_fen for line in lines
                )
                assert sum(line.debit_fen for line in lines) > 0
            for label, original_event_id in (
                ("intangible_retirement", intangible_event_ids["retired"]),
                ("intangible_amortization", intangible_event_ids["amortized"]),
                ("intangible_acquisition", intangible_event_ids["acquired"]),
                ("borrowing_principal", borrowing_event_ids["repaid"]),
                ("borrowing_payment_two", borrowing_event_ids["second_payment"]),
                ("borrowing_accrual_two", borrowing_event_ids["second_accrual"]),
                ("borrowing_payment_one", borrowing_event_ids["first_payment"]),
                ("borrowing_accrual_one", borrowing_event_ids["first_accrual"]),
                ("borrowing_draw", borrowing_event_ids["drawn"]),
            ):
                assert (
                    voucher_by_event[reversal_event_ids[label]].reversal_of_voucher_id
                    == voucher_by_event[original_event_id].id
                )

            role_lines = database_session.execute(
                select(Account.system_role, VoucherLine.debit_fen, VoucherLine.credit_fen)
                .join(VoucherLine, VoucherLine.account_id == Account.id)
                .where(
                    VoucherLine.voucher_id
                    == voucher_by_event[intangible_event_ids["acquired"]].id
                )
            ).all()
            assert [(row.system_role, row.debit_fen, row.credit_fen) for row in role_lines] == [
                ("intangible_asset_cost", 12_000, 0),
                ("bank", 0, 12_000),
            ]
            interest_roles = database_session.execute(
                select(Account.system_role, VoucherLine.debit_fen, VoucherLine.credit_fen)
                .join(VoucherLine, VoucherLine.account_id == Account.id)
                .where(
                    VoucherLine.voucher_id
                    == voucher_by_event[borrowing_event_ids["first_accrual"]].id
                )
                .order_by(VoucherLine.line_number)
            ).all()
            assert [(row.system_role, row.debit_fen, row.credit_fen) for row in interest_roles] == [
                ("borrowing_interest_expense", 18_100, 0),
                ("interest_payable", 0, 18_100),
            ]

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
                intangible_event_ids["acquired"],
                ids["intangible_acquisition_evidence"],
                "supporting",
            ) in evidence_edges
            assert (
                borrowing_event_ids["drawn"], ids["borrowing_contract_evidence"], "supporting"
            ) in evidence_edges
            assert (
                reversal_event_ids["borrowing_draw"],
                ids["borrowing_contract_evidence"],
                "inherited",
            ) in evidence_edges

            matched_bank_ids = {
                ids["intangible_bank"],
                ids["drawdown_bank"],
                ids["first_interest_bank"],
                ids["second_interest_bank"],
                ids["principal_bank"],
            }
            matches = database_session.scalars(
                select(BankTransactionMatch).where(
                    BankTransactionMatch.bank_transaction_id.in_(matched_bank_ids)
                )
            ).all()
            assert len(matches) == 5
            assert all(match.invalidated_by_event_id is not None for match in matches)
            assert all(match.invalidated_at is not None for match in matches)
            banks = database_session.scalars(
                select(BankTransaction).where(BankTransaction.id.in_(matched_bank_ids))
            ).all()
            assert all(bank.matched_event_id is None for bank in banks)
    finally:
        verification_engine.dispose()
