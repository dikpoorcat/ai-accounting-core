from __future__ import annotations

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import ExitStack
from datetime import date
from hashlib import sha256
from threading import Barrier

import pytest
import sqlalchemy as sa
from _postgres_helpers import authenticated_business_database, catalog_owner_authority
from alembic.config import Config
from component_posting_helpers import create_component_voucher as create_voucher
from conftest import AuthenticatedOwnerAuthority
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import RecordEventRequest
from ai_accounting.fixed_asset_service import FixedAssetService
from ai_accounting.ledger import Entry
from ai_accounting.models import (
    Account,
    BusinessEvent,
    Counterparty,
    Evidence,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDepreciationBatch,
    FixedAssetDisposal,
    OpenItem,
    TaxRule,
    VoucherLine,
    event_evidence,
)
from ai_accounting.schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    ConfirmFixedAssetDepreciationBatchRequest,
    DisposeFixedAssetRequest,
    PreviewFixedAssetDepreciationBatchRequest,
    PreviewFixedAssetDepreciationRequest,
    ReverseEventRequest,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _evidence(session: Session, org_id: uuid.UUID, seed: str) -> Evidence:
    row = Evidence(
        org_id=org_id,
        sha256=sha256(seed.encode()).hexdigest(),
        original_name=f"{seed}.pdf",
        media_type="application/pdf",
        source="test",
        size_bytes=1,
        storage_path=f"test/{seed}",
    )
    session.add(row)
    session.flush()
    return row


def _acquire_payable(
    session: Session,
    org_id: uuid.UUID,
    evidence_id: uuid.UUID,
    authority: AuthenticatedOwnerAuthority,
    key: str,
) -> tuple[FixedAsset, BusinessEvent]:
    with authority.attributed_call(session, tool_name="finance_acquire_fixed_asset"):
        result = FixedAssetService(session).acquire_fixed_asset(
            AcquireFixedAssetRequest.model_validate(
            {
                "org_id": org_id,
                "idempotency_key": f"{key}-acquire",
                "asset_code": f"FA-{key}",
                "asset_name": "生产设备",
                "category": "production_equipment",
                "expected_use_over_one_year": True,
                "purchase_date": "2026-01-02",
                "posting_date": "2026-01-02",
                "cost_components": {
                    "purchase_price_fen": 1_000_000,
                    "noncreditable_tax_fen": 30_000,
                    "transport_and_handling_fen": 10_000,
                    "installation_and_direct_cost_fen": 10_000,
                },
                "supplier": {"kind": "supplier", "name": f"供应商-{key}"},
                "settlement_method": "payable",
                "due_date": "2026-02-02",
                "evidence_references": [evidence_id],
                "claims_creditable_input_vat": False,
            }
            )
        )
    assert result.status == "posted", result.errors
    session.commit()
    return session.get(FixedAsset, result.asset_id), session.get(BusinessEvent, result.event_id)


def _activate(
    session: Session,
    asset: FixedAsset,
    evidence_id: uuid.UUID,
    authority: AuthenticatedOwnerAuthority,
    key: str,
) -> FixedAssetActivation:
    with authority.attributed_call(session, tool_name="finance_activate_fixed_asset"):
        result = FixedAssetService(session).activate_fixed_asset(
            ActivateFixedAssetRequest.model_validate(
            {
                "org_id": asset.org_id,
                "asset_id": asset.id,
                "idempotency_key": f"{key}-activate",
                "activation_date": "2026-01-10",
                "posting_date": "2026-01-10",
                "useful_life_months": 13,
                "residual_value_fen": 10_000,
                "benefit_area": "management",
                "evidence_references": [evidence_id],
            }
            )
        )
    assert result.status == "posted", result.errors
    session.commit()
    return session.scalar(
        sa.select(FixedAssetActivation).where(FixedAssetActivation.event_id == result.event_id)
    )


def _draft_asset_event(
    session: Session,
    *,
    asset: FixedAsset,
    event_type: str,
    key: str,
    posting_date: date,
) -> BusinessEvent:
    event = BusinessEvent(
        org_id=asset.org_id,
        idempotency_key=key,
        request_payload_hash=sha256(key.encode()).hexdigest(),
        event_type=event_type,
        status="draft",
        description=key,
        facts={"asset_id": str(asset.id), "test": key},
        business_date=posting_date,
        posting_date=posting_date,
        rule_trace=[{"stage": "test", "rule": "closed_template"}],
        rule_version="small_enterprise_fixed_asset_straight_line_2013.1",
    )
    session.add(event)
    session.flush()
    return event


def _add_depreciation_attempt(
    session: Session,
    *,
    asset: FixedAsset,
    activation: FixedAssetActivation,
    key: str,
    period_start: date,
    posting_date: date,
    sequence_no: int,
    amount_fen: int,
    accumulated_after_fen: int,
) -> None:
    event = _draft_asset_event(
        session,
        asset=asset,
        event_type="fixed_asset_depreciation",
        key=key,
        posting_date=posting_date,
    )
    session.add(
        FixedAssetDepreciation(
            org_id=asset.org_id,
            asset_id=asset.id,
            activation_id=activation.id,
            event_id=event.id,
            period_start=period_start,
            posting_date=posting_date,
            sequence_no=sequence_no,
            amount_fen=amount_fen,
            accumulated_after_fen=accumulated_after_fen,
            calculation_hash=sha256(key.encode()).hexdigest(),
            accounting_rule_version="small_enterprise_fixed_asset_straight_line_2013.1",
            accounting_rule_source_url=(
                "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
            ),
        )
    )
    voucher = create_voucher(
        session,
        event=event,
        posting_date=posting_date,
        description=key,
        entries=[
            Entry(account_role="management_depreciation_expense", debit_fen=amount_fen),
            Entry(account_role="accumulated_depreciation", credit_fen=amount_fen),
        ],
    )
    event.status = "posted"
    session.flush()
    assert voucher.status == "posted"


def _add_duplicate_activation_attempt(
    session: Session,
    *,
    asset: FixedAsset,
    key: str,
) -> None:
    event = _draft_asset_event(
        session,
        asset=asset,
        event_type="fixed_asset_activation",
        key=key,
        posting_date=date(2026, 1, 11),
    )
    evidence = _evidence(session, asset.org_id, key)
    session.execute(
        event_evidence.insert().values(
            org_id=asset.org_id,
            event_id=event.id,
            evidence_id=evidence.id,
            relation_kind="supporting",
        )
    )
    session.add(
        FixedAssetActivation(
            org_id=asset.org_id,
            asset_id=asset.id,
            event_id=event.id,
            in_service_date=date(2026, 1, 11),
            posting_date=date(2026, 1, 11),
            depreciation_method="straight_line",
            useful_life_months=13,
            residual_value_fen=10_000,
            benefit_area="management",
            accounting_rule_version="small_enterprise_fixed_asset_straight_line_2013.1",
            accounting_rule_source_url=(
                "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
            ),
        )
    )
    create_voucher(
        session,
        event=event,
        posting_date=date(2026, 1, 11),
        description=key,
        entries=[
            Entry(account_role="fixed_asset_cost", debit_fen=asset.cost_fen),
            Entry(account_role="fixed_asset_pending", credit_fen=asset.cost_fen),
        ],
    )
    event.status = "posted"
    session.flush()


def _add_duplicate_disposal_attempt(
    session: Session,
    *,
    asset: FixedAsset,
    activation: FixedAssetActivation,
    key: str,
) -> None:
    event = _draft_asset_event(
        session,
        asset=asset,
        event_type="fixed_asset_disposal",
        key=key,
        posting_date=date(2026, 1, 21),
    )
    evidence = _evidence(session, asset.org_id, key)
    session.execute(
        event_evidence.insert().values(
            org_id=asset.org_id,
            event_id=event.id,
            evidence_id=evidence.id,
            relation_kind="supporting",
        )
    )
    session.add(
        FixedAssetDisposal(
            org_id=asset.org_id,
            asset_id=asset.id,
            activation_id=activation.id,
            event_id=event.id,
            disposal_date=date(2026, 1, 21),
            posting_date=date(2026, 1, 21),
            disposal_kind="retirement",
            settlement_method="none",
            customer_id=None,
            gross_proceeds_fen=0,
            invoice_type="none",
            waive_threshold_exemption=False,
            vat_tax_sales_fen=0,
            vat_fen=0,
            clearance_cost_fen=0,
            accumulated_depreciation_fen=0,
            book_value_fen=asset.cost_fen,
            gain_fen=0,
            loss_fen=asset.cost_fen,
            tax_rule_id=None,
            accounting_rule_version="small_enterprise_fixed_asset_straight_line_2013.1",
            accounting_rule_source_url=(
                "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
            ),
        )
    )
    create_voucher(
        session,
        event=event,
        posting_date=date(2026, 1, 21),
        description=key,
        entries=[
            Entry(account_role="fixed_asset_clearance", debit_fen=asset.cost_fen),
            Entry(account_role="fixed_asset_cost", credit_fen=asset.cost_fen),
            Entry(account_role="fixed_asset_disposal_loss", debit_fen=asset.cost_fen),
            Entry(account_role="fixed_asset_clearance", credit_fen=asset.cost_fen),
        ],
    )
    event.status = "posted"
    session.flush()


def test_postgres_monthly_depreciation_batch_is_one_final_voucher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.setenv("DATABASE_URL", url)
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = url
        command.upgrade(config, "head")
        engine = sa.create_engine(url)
        authority_stack = ExitStack()
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                    name="PG 固定资产批量折旧",
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(session, organization)
                )
                with authority.attributed_call(session, tool_name="finance_register_evidence"):
                    evidence = _evidence(session, organization.id, "batch-pg")
                session.commit()
                first, _ = _acquire_payable(
                    session, organization.id, evidence.id, authority, "batch-pg"
                )
                _activate(session, first, evidence.id, authority, "batch-pg")
                with authority.attributed_call(session, tool_name="finance_acquire_fixed_asset"):
                    second = FixedAssetService(session).acquire_fixed_asset(
                        AcquireFixedAssetRequest.model_validate(
                        {
                            "org_id": first.org_id,
                            "idempotency_key": "batch-pg-second-acquire",
                            "asset_code": "FA-BATCH-PG-002",
                            "asset_name": "第二项设备",
                            "category": "electronic",
                            "expected_use_over_one_year": True,
                            "purchase_date": "2026-01-02",
                            "posting_date": "2026-01-02",
                            "cost_components": {
                                "purchase_price_fen": 100_006,
                                "noncreditable_tax_fen": 0,
                                "transport_and_handling_fen": 0,
                                "installation_and_direct_cost_fen": 0,
                            },
                            "supplier": {"kind": "supplier", "name": "第二供应商"},
                            "settlement_method": "payable",
                            "due_date": "2026-02-02",
                            "evidence_references": [evidence.id],
                            "claims_creditable_input_vat": False,
                            "ready_for_use": {
                                "in_service_date": "2026-01-02",
                                "useful_life_months": 13,
                                "residual_value_fen": 0,
                                "benefit_area": "management",
                            },
                            }
                        )
                    )
                assert second.status == "posted", second.errors
                preview_request = PreviewFixedAssetDepreciationBatchRequest(
                    org_id=first.org_id,
                    depreciation_period="2026-02",
                    posting_date=date(2026, 2, 28),
                )
                service = FixedAssetService(session)
                preview = service.preview_fixed_asset_depreciation_batch(preview_request)
                assert preview.data["total_amount_fen"] == 87_693
                with authority.attributed_call(
                    session, tool_name="finance_confirm_fixed_asset_depreciation_batch"
                ):
                    confirmed = service.confirm_fixed_asset_depreciation_batch(
                        ConfirmFixedAssetDepreciationBatchRequest(
                            **preview_request.model_dump(),
                            idempotency_key="batch-pg-2026-02",
                            calculation_hash=preview.calculation_hash,
                        )
                    )
                assert confirmed.status == "posted", confirmed.errors
                session.commit()

                batch = session.scalar(
                    sa.select(FixedAssetDepreciationBatch).where(
                        FixedAssetDepreciationBatch.event_id == confirmed.event_id
                    )
                )
                details = session.scalars(
                    sa.select(FixedAssetDepreciation).where(
                        FixedAssetDepreciation.event_id == confirmed.event_id
                    )
                ).all()
                assert batch.asset_count == len(details) == 2
                assert batch.total_amount_fen == 87_693
                assert (
                    len(
                        session.scalars(
                            sa.select(VoucherLine).where(
                                VoucherLine.voucher_id == confirmed.voucher_id
                            )
                        ).all()
                    )
                    == 2
                )
                with pytest.raises(DBAPIError, match="final fixed-asset facts are immutable"):
                    batch.total_amount_fen += 1
                    session.commit()
        finally:
            authority_stack.close()
            engine.dispose()


def test_postgres_fixed_asset_reverse_edges_and_normal_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.setenv("DATABASE_URL", url)
        config = Config("alembic.ini")
        config.attributes["database_url_override"] = url
        command.upgrade(config, "head")
        engine = sa.create_engine(url)
        authority_stack = ExitStack()
        try:
            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                    name="PG 固定资产冲正与结算",
                )
                session.commit()
                authority = authority_stack.enter_context(
                    catalog_owner_authority(session, organization)
                )
                with authority.attributed_call(session, tool_name="finance_register_evidence"):
                    evidence = _evidence(session, organization.id, "reverse-edges")
                session.commit()
                asset, event = _acquire_payable(
                    session, organization.id, evidence.id, authority, "reverse-edges"
                )
                with authority.attributed_call(session, tool_name="finance_acquire_fixed_asset"):
                    direct = FixedAssetService(session).acquire_fixed_asset(
                        AcquireFixedAssetRequest.model_validate(
                        {
                            "org_id": asset.org_id,
                            "idempotency_key": "direct-ready-acquisition",
                            "asset_code": "FA-direct-ready",
                            "asset_name": "已交付设备",
                            "category": "electronic",
                            "expected_use_over_one_year": True,
                            "purchase_date": "2026-01-02",
                            "posting_date": "2026-01-02",
                            "cost_components": {
                                "purchase_price_fen": 120_000,
                                "noncreditable_tax_fen": 0,
                                "transport_and_handling_fen": 0,
                                "installation_and_direct_cost_fen": 0,
                            },
                            "supplier": {"kind": "supplier", "name": "直接交付供应商"},
                            "settlement_method": "payable",
                            "due_date": "2026-02-28",
                            "evidence_references": [evidence.id],
                            "claims_creditable_input_vat": False,
                            "ready_for_use": {
                                "in_service_date": "2026-01-02",
                                "useful_life_months": 13,
                                "residual_value_fen": 10_000,
                                "benefit_area": "management",
                            },
                            }
                        )
                    )
                assert direct.status == "posted", direct.errors
                session.commit()
                direct_asset = session.get(FixedAsset, direct.asset_id)
                direct_activation = session.scalar(
                    sa.select(FixedAssetActivation).where(
                        FixedAssetActivation.asset_id == direct.asset_id
                    )
                )
                assert direct_activation.event_id == direct.event_id
                assert (
                    session.scalar(
                        sa.select(Account.system_role)
                        .join(VoucherLine, VoucherLine.account_id == Account.id)
                        .where(
                            VoucherLine.voucher_id == direct.voucher_id,
                            VoucherLine.debit_fen == 120_000,
                        )
                    )
                    == "fixed_asset_cost"
                )
                direct_preview = FixedAssetService(session).preview_fixed_asset_depreciation(
                    PreviewFixedAssetDepreciationRequest(
                        org_id=asset.org_id,
                        asset_id=direct.asset_id,
                        depreciation_period="2026-02",
                        posting_date=date(2026, 2, 28),
                    )
                )
                assert direct_preview.status == "calculated"
                assert direct_preview.data["amount_fen"] == 8_462
                with authority.attributed_call(session, tool_name="finance_reverse_event"):
                    reversed_direct = FixedAssetService(session).reverse_event(
                        ReverseEventRequest(
                            org_id=asset.org_id,
                            event_id=direct.event_id,
                            idempotency_key="reverse-direct-ready-acquisition",
                            reason="验证合并购置启用事件可整体冲正",
                            posting_date=date(2026, 2, 1),
                        )
                    )
                assert reversed_direct.status == "posted", reversed_direct.errors
                session.commit()
                assert session.get(BusinessEvent, direct.event_id).status == "reversed"
                item = session.scalar(
                    sa.select(OpenItem).where(OpenItem.source_event_id == event.id)
                )
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    payment = FinanceService(session).record_event(
                        RecordEventRequest.model_validate(
                            {
                                "org_id": asset.org_id,
                                "idempotency_key": "asset-payable-settlement",
                                "posting_date": "2026-02-02",
                                "evidence_references": [event.evidence[0].id],
                                "components": [
                                    {
                                        "key": "settlement",
                                        "kind": "payable_settlement",
                                        "business_date": "2026-02-02",
                                        "payment_date": "2026-02-02",
                                        "counterparty": {"id": item.counterparty_id},
                                        "allocations": [
                                            {
                                                "open_item_id": item.id,
                                                "amount_fen": asset.cost_fen,
                                            }
                                        ],
                                    }
                                ],
                                "funds": [
                                    {
                                        "key": "payment",
                                        "account_code": "1001",
                                        "direction": "payment",
                                        "payment_date": "2026-02-02",
                                        "amount_fen": asset.cost_fen,
                                        "allocations": [
                                            {
                                                "component_key": "settlement",
                                                "amount_fen": asset.cost_fen,
                                            }
                                        ],
                                    }
                                ],
                            }
                        )
                    )
                assert payment.status == "posted"
                with authority.attributed_call(session, tool_name="finance_acquire_fixed_asset"):
                    employee_evidence = _evidence(session, asset.org_id, "employee-advanced")
                    employee_asset = FixedAssetService(session).acquire_fixed_asset(
                        AcquireFixedAssetRequest.model_validate(
                            {
                                "org_id": asset.org_id,
                                "idempotency_key": "employee-advanced-acquisition",
                                "asset_code": "FA-employee-advanced",
                                "asset_name": "员工垫付设备",
                                "category": "tools_furniture",
                                "expected_use_over_one_year": True,
                                "purchase_date": "2026-02-02",
                                "posting_date": "2026-02-02",
                                "cost_components": {
                                    "purchase_price_fen": 50_000,
                                    "noncreditable_tax_fen": 0,
                                    "transport_and_handling_fen": 0,
                                    "installation_and_direct_cost_fen": 0,
                                },
                                "supplier": {
                                    "kind": "supplier",
                                    "name": "家具供应商",
                                },
                                "reimbursing_employee": {
                                    "kind": "employee",
                                    "name": "测试员工乙",
                                },
                                "settlement_method": "employee_payable",
                                "due_date": "2026-02-03",
                                "evidence_references": [employee_evidence.id],
                                "claims_creditable_input_vat": False,
                            }
                        )
                    )
                assert employee_asset.status == "posted", employee_asset.errors
                employee_item = session.scalar(
                    sa.select(OpenItem).where(OpenItem.source_event_id == employee_asset.event_id)
                )
                employee_counterparty = session.get(
                    Counterparty,
                    session.get(FixedAsset, employee_asset.asset_id).reimbursing_employee_id,
                )
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    employee_payment = FinanceService(session).record_event(
                        RecordEventRequest.model_validate(
                            {
                                "org_id": asset.org_id,
                                "idempotency_key": "employee-advanced-payment",
                                "posting_date": "2026-02-03",
                                "evidence_references": [
                                    session.get(BusinessEvent, employee_asset.event_id)
                                    .evidence[0]
                                    .id
                                ],
                                "components": [
                                    {
                                        "key": "settlement",
                                        "kind": "payable_settlement",
                                        "business_date": "2026-02-03",
                                        "payment_date": "2026-02-03",
                                        "counterparty": {"id": employee_counterparty.id},
                                        "allocations": [
                                            {"open_item_id": employee_item.id, "amount_fen": 50_000}
                                        ],
                                    }
                                ],
                                "funds": [
                                    {
                                        "key": "payment",
                                        "account_code": "1001",
                                        "direction": "payment",
                                        "payment_date": "2026-02-03",
                                        "amount_fen": 50_000,
                                        "allocations": [
                                            {
                                                "component_key": "settlement",
                                                "amount_fen": 50_000,
                                            }
                                        ],
                                    }
                                ],
                            }
                        )
                    )
                assert employee_payment.status == "posted", employee_payment.errors
                session.commit()
                session.refresh(item)
                assert item.status == "settled"
                assert item.settled_amount_fen == asset.cost_fen
                session.refresh(employee_item)
                assert employee_item.status == "settled"

            with Session(engine) as session:
                asset = session.scalar(
                    sa.select(FixedAsset).where(FixedAsset.asset_code == "FA-reverse-edges")
                )
                source = session.get(BusinessEvent, asset.acquisition_event_id)
                supplier = session.get(Counterparty, asset.supplier_id)
                original_item = session.scalar(
                    sa.select(OpenItem).where(OpenItem.source_event_id == source.id)
                )
                with authority.attributed_call(session, tool_name="finance_negative_tamper"):
                    session.add(
                        OpenItem(
                            org_id=asset.org_id,
                            counterparty_id=supplier.id,
                            source_event_id=source.id,
                            source_component_id=original_item.source_component_id,
                            component_key=original_item.component_key,
                            account_id=original_item.account_id,
                            item_type="payable",
                            original_amount_fen=1,
                            settled_amount_fen=0,
                            status="open",
                            due_date=asset.due_date,
                            payable_category=original_item.payable_category,
                        )
                    )
                    with pytest.raises(DBAPIError, match="uq_open_item_component_key"):
                        session.commit()

            with Session(engine) as session:
                direct_asset = session.scalar(
                    sa.select(FixedAsset).where(FixedAsset.asset_code == "FA-direct-ready")
                )
                with authority.attributed_call(session, tool_name="finance_negative_tamper"):
                    direct_asset.cost_fen += 1
                    with pytest.raises(
                        DBAPIError, match="final fixed-asset facts are immutable"
                    ):
                        session.commit()

        finally:
            authority_stack.close()
            engine.dispose()


def test_postgres_fixed_asset_lifecycle_rejects_skip_overage_and_wrong_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    del monkeypatch
    with authenticated_business_database(
        "fixed_asset_lifecycle", name="PG 固定资产生命周期"
    ) as (engine, org_id, evidence_id, authority):
        with Session(engine) as session:
            asset, _ = _acquire_payable(session, org_id, evidence_id, authority, "skip")
            _activate(session, asset, evidence_id, authority, "skip")
            with authority.attributed_call(session, tool_name="finance_activate_fixed_asset"):
                duplicate_activation = FixedAssetService(session).activate_fixed_asset(
                    ActivateFixedAssetRequest.model_validate(
                        {
                            "org_id": org_id,
                            "asset_id": asset.id,
                            "idempotency_key": "duplicate-activation",
                            "activation_date": "2026-01-11",
                            "posting_date": "2026-01-11",
                            "useful_life_months": 13,
                            "residual_value_fen": 10_000,
                            "benefit_area": "management",
                            "evidence_references": [evidence_id],
                        }
                    )
                )
            assert duplicate_activation.errors == ["FIXED_ASSET_ALREADY_ACTIVATED"]

            service = FixedAssetService(session)
            wrong_month = service.preview_fixed_asset_depreciation(
                PreviewFixedAssetDepreciationRequest(
                    org_id=org_id,
                    asset_id=asset.id,
                    depreciation_period="2026-02",
                    posting_date=date(2026, 3, 1),
                )
            )
            assert wrong_month.errors == ["FIXED_ASSET_DEPRECIATION_PERIOD_INVALID"]
            skipped = service.preview_fixed_asset_depreciation(
                PreviewFixedAssetDepreciationRequest(
                    org_id=org_id,
                    asset_id=asset.id,
                    depreciation_period="2026-03",
                    posting_date=date(2026, 3, 31),
                )
            )
            assert skipped.errors == ["FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE"]

            disposal_asset, _ = _acquire_payable(
                session, org_id, evidence_id, authority, "duplicate-disposal"
            )
            _activate(session, disposal_asset, evidence_id, authority, "duplicate-disposal")
            disposal_request = DisposeFixedAssetRequest.model_validate(
                {
                    "org_id": org_id,
                    "asset_id": disposal_asset.id,
                    "idempotency_key": "first-disposal",
                    "disposal_date": "2026-01-20",
                    "posting_date": "2026-01-20",
                    "disposal_kind": "retirement",
                    "settlement_method": "none",
                    "clearance_cost_fen": 0,
                    "evidence_references": [evidence_id],
                }
            )
            with authority.attributed_call(session, tool_name="finance_dispose_fixed_asset"):
                disposed = service.dispose_fixed_asset(disposal_request)
            assert disposed.status == "posted", disposed.errors
            session.commit()
            with authority.attributed_call(session, tool_name="finance_dispose_fixed_asset"):
                duplicate_disposal = service.dispose_fixed_asset(
                    disposal_request.model_copy(
                        update={"idempotency_key": "duplicate-disposal-second"}
                    )
                )
            assert duplicate_disposal.errors == ["FIXED_ASSET_ALREADY_DISPOSED"]

            concurrent_asset, _ = _acquire_payable(
                session, org_id, evidence_id, authority, "concurrent-disposal"
            )
            _activate(session, concurrent_asset, evidence_id, authority, "concurrent-disposal")
            concurrent_asset_id = concurrent_asset.id

        barrier = Barrier(2)

        def dispose_concurrently(index: int) -> tuple[str, list[str]]:
            with Session(engine) as session:
                barrier.wait(timeout=5)
                with authority.attributed_call(
                    session, tool_name="finance_dispose_fixed_asset"
                ):
                    result = FixedAssetService(session).dispose_fixed_asset(
                        DisposeFixedAssetRequest.model_validate(
                            {
                                "org_id": org_id,
                                "asset_id": concurrent_asset_id,
                                "idempotency_key": f"concurrent-disposal-{index}",
                                "disposal_date": "2026-01-20",
                                "posting_date": "2026-01-20",
                                "disposal_kind": "retirement",
                                "settlement_method": "none",
                                "clearance_cost_fen": 0,
                                "evidence_references": [evidence_id],
                            }
                        )
                    )
                session.commit()
                return str(result.status), result.errors

        with ThreadPoolExecutor(max_workers=2) as executor:
            concurrent_results = list(executor.map(dispose_concurrently, (1, 2)))
        assert [status for status, _ in concurrent_results].count("posted") == 1
        assert [errors for _, errors in concurrent_results].count(
            ["FIXED_ASSET_ALREADY_DISPOSED"]
        ) == 1
        with Session(engine) as session:
            assert session.scalar(
                sa.select(sa.func.count())
                .select_from(FixedAssetDisposal)
                .join(BusinessEvent, BusinessEvent.id == FixedAssetDisposal.event_id)
                .where(
                    FixedAssetDisposal.asset_id == concurrent_asset_id,
                    BusinessEvent.status == "posted",
                )
            ) == 1

            tax_asset, _ = _acquire_payable(
                session, org_id, evidence_id, authority, "tax-rule"
            )
            _activate(session, tax_asset, evidence_id, authority, "tax-rule")
            with authority.attributed_call(session, tool_name="finance_dispose_fixed_asset"):
                before_effective = FixedAssetService(session).dispose_fixed_asset(
                    DisposeFixedAssetRequest.model_validate(
                        {
                            "org_id": org_id,
                            "asset_id": tax_asset.id,
                            "idempotency_key": "tax-before-effective",
                            "disposal_date": "2026-01-20",
                            "posting_date": "2026-01-20",
                            "disposal_kind": "sale",
                            "gross_proceeds_fen": 500_000,
                            "invoice_type": "ordinary",
                            "waive_exemption": False,
                            "settlement_method": "receivable",
                            "customer": {"kind": "customer", "name": "税则客户"},
                            "tax_obligation_date": "2025-12-31",
                            "clearance_cost_fen": 0,
                            "evidence_references": [evidence_id],
                        }
                    )
                )
            assert before_effective.errors == ["MODULE_NOT_ENABLED:used_fixed_asset_vat_rule"]
            with authority.attributed_call(session, tool_name="finance_dispose_fixed_asset"):
                sold = FixedAssetService(session).dispose_fixed_asset(
                    DisposeFixedAssetRequest.model_validate(
                        {
                            "org_id": org_id,
                            "asset_id": tax_asset.id,
                            "idempotency_key": "tax-on-effective-date",
                            "disposal_date": "2026-01-20",
                            "posting_date": "2026-01-20",
                            "disposal_kind": "sale",
                            "gross_proceeds_fen": 500_000,
                            "invoice_type": "ordinary",
                            "waive_exemption": False,
                            "settlement_method": "receivable",
                            "customer": {"kind": "customer", "name": "税则客户"},
                            "tax_obligation_date": "2026-01-01",
                            "clearance_cost_fen": 0,
                            "evidence_references": [evidence_id],
                        }
                    )
                )
            assert sold.status == "posted", sold.errors
            session.commit()
            disposal = session.scalar(
                sa.select(FixedAssetDisposal).where(FixedAssetDisposal.event_id == sold.event_id)
            )
            tax_rule = session.get(TaxRule, disposal.tax_rule_id)
            with authority.attributed_call(session, tool_name="finance_negative_tamper"):
                tax_rule.parameters = {
                    **tax_rule.parameters,
                    "effective_levy_rate_percent": "1",
                }
                with pytest.raises(DBAPIError, match="FIXED_ASSET_DISPOSAL_TAX_RULE_INVALID"):
                    session.commit()
