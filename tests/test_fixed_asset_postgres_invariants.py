from __future__ import annotations

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from hashlib import sha256
from threading import Barrier

import pytest
import sqlalchemy as sa
from alembic.config import Config
from conftest import authenticate_and_confirm_bank_scope
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import get_account_by_role, seed_organization
from ai_accounting.fixed_asset_service import FixedAssetService
from ai_accounting.ledger import Entry, create_voucher
from ai_accounting.models import (
    BusinessEvent,
    Counterparty,
    Evidence,
    FixedAsset,
    FixedAssetActivation,
    FixedAssetDepreciation,
    FixedAssetDisposal,
    OpenItem,
    Organization,
    TaxRule,
    event_evidence,
)
from ai_accounting.schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    DisposeFixedAssetRequest,
    RecordEventRequest,
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


def _acquire_payable(session: Session, key: str) -> tuple[FixedAsset, BusinessEvent]:
    organization = seed_organization(
        session, accounting_period_control_enabled=False, name=f"PG 固定资产 {key}"
    )
    evidence = _evidence(session, organization.id, key[0])
    result = FixedAssetService(session).acquire_fixed_asset(
        AcquireFixedAssetRequest.model_validate(
            {
                "org_id": organization.id,
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
                "evidence_references": [evidence.id],
                "claims_creditable_input_vat": False,
            }
        )
    )
    assert result.status == "posted", result.errors
    session.commit()
    return session.get(FixedAsset, result.asset_id), session.get(BusinessEvent, result.event_id)


def _activate(session: Session, asset: FixedAsset, key: str) -> FixedAssetActivation:
    evidence = _evidence(session, asset.org_id, f"{key}-activation")
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
                "evidence_references": [evidence.id],
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


def test_postgres_fixed_asset_reverse_edges_and_normal_settlement(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.setenv("DATABASE_URL", url)
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        engine = sa.create_engine(url)
        try:
            with Session(engine) as session:
                asset, event = _acquire_payable(session, "reverse-edges")
                scope_evidence = _evidence(session, asset.org_id, "reverse-edges-scope")
                authority = authenticate_and_confirm_bank_scope(
                    session,
                    session.get(Organization, asset.org_id),
                    evidence_id=scope_evidence.id,
                    accounts=[
                        {
                            "bank_account_code": "1002",
                            "account_name": "银行存款",
                            "start_date": date(2026, 1, 1),
                        }
                    ],
                )
                item = session.scalar(
                    sa.select(OpenItem).where(OpenItem.source_event_id == event.id)
                )
                with authority.attributed_call(session, tool_name="finance_record_event"):
                    payment = FinanceService(session).record_event(
                        RecordEventRequest.model_validate(
                            {
                                "org_id": asset.org_id,
                                "idempotency_key": "asset-payable-settlement",
                                "event_type": "supplier_payment",
                                "business_dates": {
                                    "business_date": "2026-02-02",
                                    "posting_date": "2026-02-02",
                                    "payment_date": "2026-02-02",
                                },
                                "counterparty": {
                                    "kind": "supplier",
                                    "name": "供应商-reverse-edges",
                                },
                                "amounts": {"amount_fen": asset.cost_fen},
                                "bank_account_code": "1002",
                                "allocations": [
                                    {"open_item_id": item.id, "amount_fen": asset.cost_fen}
                                ],
                            }
                        )
                    )
                assert payment.status == "posted"
                session.commit()
                session.refresh(item)
                assert item.status == "settled"
                assert item.settled_amount_fen == asset.cost_fen

            with Session(engine) as session:
                asset = session.scalar(
                    sa.select(FixedAsset).where(FixedAsset.asset_code == "FA-reverse-edges")
                )
                source = session.get(BusinessEvent, asset.acquisition_event_id)
                supplier = session.get(Counterparty, asset.supplier_id)
                session.add(
                    OpenItem(
                        org_id=asset.org_id,
                        counterparty_id=supplier.id,
                        source_event_id=source.id,
                        item_type="payable",
                        original_amount_fen=1,
                        settled_amount_fen=0,
                        status="open",
                        due_date=asset.due_date,
                    )
                )
                with pytest.raises(DBAPIError, match="FIXED_ASSET_ACQUISITION_SETTLEMENT"):
                    session.commit()

            with Session(engine) as session:
                asset = session.scalar(
                    sa.select(FixedAsset).where(FixedAsset.asset_code == "FA-reverse-edges")
                )
                pending = get_account_by_role(session, asset.org_id, "fixed_asset_pending")
                pending.system_role = "tampered_fixed_asset_pending"
                with pytest.raises(DBAPIError, match="FIXED_ASSET_ACQUISITION_VOUCHER"):
                    session.commit()

        finally:
            engine.dispose()


def test_postgres_fixed_asset_lifecycle_rejects_skip_overage_and_wrong_month(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.setenv("DATABASE_URL", url)
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        engine = sa.create_engine(url)
        try:
            with Session(engine) as session:
                asset, _ = _acquire_payable(session, "skip")
                activation = _activate(session, asset, "skip")
                _add_duplicate_activation_attempt(session, asset=asset, key="duplicate-activation")
                with pytest.raises(DBAPIError, match="FIXED_ASSET_ALREADY_ACTIVATED"):
                    session.commit()

            with Session(engine) as session:
                asset = session.scalar(
                    sa.select(FixedAsset).where(FixedAsset.asset_code == "FA-skip")
                )
                activation = session.scalar(
                    sa.select(FixedAssetActivation).where(FixedAssetActivation.asset_id == asset.id)
                )
                _add_depreciation_attempt(
                    session,
                    asset=asset,
                    activation=activation,
                    key="skip-march",
                    period_start=date(2026, 3, 1),
                    posting_date=date(2026, 3, 31),
                    sequence_no=2,
                    amount_fen=80_000,
                    accumulated_after_fen=80_000,
                )
                with pytest.raises(DBAPIError, match="FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE"):
                    session.commit()

            with Session(engine) as session:
                asset = session.scalar(
                    sa.select(FixedAsset).where(FixedAsset.asset_code == "FA-skip")
                )
                activation = session.scalar(
                    sa.select(FixedAssetActivation).where(FixedAssetActivation.asset_id == asset.id)
                )
                _add_depreciation_attempt(
                    session,
                    asset=asset,
                    activation=activation,
                    key="over-depreciation",
                    period_start=date(2026, 2, 1),
                    posting_date=date(2026, 2, 28),
                    sequence_no=1,
                    amount_fen=80_001,
                    accumulated_after_fen=80_001,
                )
                with pytest.raises(DBAPIError, match="FIXED_ASSET_DEPRECIATION_AMOUNT_INVALID"):
                    session.commit()

            with Session(engine) as session:
                asset = session.scalar(
                    sa.select(FixedAsset).where(FixedAsset.asset_code == "FA-skip")
                )
                activation = session.scalar(
                    sa.select(FixedAssetActivation).where(FixedAssetActivation.asset_id == asset.id)
                )
                with pytest.raises(DBAPIError):
                    _add_depreciation_attempt(
                        session,
                        asset=asset,
                        activation=activation,
                        key="wrong-posting-month",
                        period_start=date(2026, 2, 1),
                        posting_date=date(2026, 3, 1),
                        sequence_no=1,
                        amount_fen=80_000,
                        accumulated_after_fen=80_000,
                    )

            with Session(engine) as session:
                asset, _ = _acquire_payable(session, "duplicate-disposal")
                activation = _activate(session, asset, "duplicate-disposal")
                evidence = _evidence(session, asset.org_id, "first-disposal")
                disposed = FixedAssetService(session).dispose_fixed_asset(
                    DisposeFixedAssetRequest.model_validate(
                        {
                            "org_id": asset.org_id,
                            "asset_id": asset.id,
                            "idempotency_key": "first-disposal",
                            "disposal_date": "2026-01-20",
                            "posting_date": "2026-01-20",
                            "disposal_kind": "retirement",
                            "settlement_method": "none",
                            "clearance_cost_fen": 0,
                            "evidence_references": [evidence.id],
                        }
                    )
                )
                assert disposed.status == "posted", disposed.errors
                session.commit()

            with Session(engine) as session:
                asset = session.scalar(
                    sa.select(FixedAsset).where(FixedAsset.asset_code == "FA-duplicate-disposal")
                )
                activation = session.scalar(
                    sa.select(FixedAssetActivation).where(FixedAssetActivation.asset_id == asset.id)
                )
                _add_duplicate_disposal_attempt(
                    session,
                    asset=asset,
                    activation=activation,
                    key="duplicate-disposal-direct",
                )
                with pytest.raises(DBAPIError, match="FIXED_ASSET_ALREADY_DISPOSED"):
                    session.commit()

            with Session(engine) as session:
                concurrent_asset, _ = _acquire_payable(session, "concurrent-disposal")
                _activate(session, concurrent_asset, "concurrent-disposal")
                concurrent_evidence = _evidence(
                    session, concurrent_asset.org_id, "concurrent-disposal"
                )
                concurrent_evidence_id = concurrent_evidence.id
                concurrent_asset_id = concurrent_asset.id
                concurrent_org_id = concurrent_asset.org_id
                session.commit()

            barrier = Barrier(2)

            def dispose_concurrently(index: int) -> tuple[str, list[str]]:
                with Session(engine) as session:
                    barrier.wait(timeout=5)
                    result = FixedAssetService(session).dispose_fixed_asset(
                        DisposeFixedAssetRequest.model_validate(
                            {
                                "org_id": concurrent_org_id,
                                "asset_id": concurrent_asset_id,
                                "idempotency_key": f"concurrent-disposal-{index}",
                                "disposal_date": "2026-01-20",
                                "posting_date": "2026-01-20",
                                "disposal_kind": "retirement",
                                "settlement_method": "none",
                                "clearance_cost_fen": 0,
                                "evidence_references": [concurrent_evidence_id],
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
                active_disposals = session.scalar(
                    sa.select(sa.func.count())
                    .select_from(FixedAssetDisposal)
                    .join(BusinessEvent, BusinessEvent.id == FixedAssetDisposal.event_id)
                    .where(
                        FixedAssetDisposal.asset_id == concurrent_asset_id,
                        BusinessEvent.status == "posted",
                    )
                )
                assert active_disposals == 1

            with Session(engine) as session:
                tax_asset, _ = _acquire_payable(session, "tax-rule")
                _activate(session, tax_asset, "tax-rule")
                tax_evidence = _evidence(session, tax_asset.org_id, "tax-disposal")
                before_effective = FixedAssetService(session).dispose_fixed_asset(
                    DisposeFixedAssetRequest.model_validate(
                        {
                            "org_id": tax_asset.org_id,
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
                            "evidence_references": [tax_evidence.id],
                        }
                    )
                )
                assert before_effective.errors == ["MODULE_NOT_ENABLED:used_fixed_asset_vat_rule"]
                sold = FixedAssetService(session).dispose_fixed_asset(
                    DisposeFixedAssetRequest.model_validate(
                        {
                            "org_id": tax_asset.org_id,
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
                            "evidence_references": [tax_evidence.id],
                        }
                    )
                )
                assert sold.status == "posted", sold.errors
                session.commit()
                disposal = session.scalar(
                    sa.select(FixedAssetDisposal).where(
                        FixedAssetDisposal.event_id == sold.event_id
                    )
                )
                tax_rule = session.get(TaxRule, disposal.tax_rule_id)
                tax_rule.parameters = {
                    **tax_rule.parameters,
                    "effective_levy_rate_percent": "1",
                }
                with pytest.raises(DBAPIError, match="FIXED_ASSET_DISPOSAL_TAX_RULE_INVALID"):
                    session.commit()
        finally:
            engine.dispose()


def test_postgres_fixed_asset_upgrade_downgrade_round_trip(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        monkeypatch.setenv("DATABASE_URL", url)
        config = Config("alembic.ini")
        command.upgrade(config, "head")
        command.check(config)
        command.downgrade(config, "0008_payroll_r7_tax_closure")
        engine = sa.create_engine(url)
        try:
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "0008_payroll_r7_tax_closure"
                )
                dangling = connection.scalar(
                    sa.text(
                        """
                        SELECT COUNT(*) FROM pg_trigger
                         WHERE NOT tgisinternal AND tgname LIKE 'fixed_asset_%'
                        """
                    )
                )
                assert dangling == 0
            command.upgrade(config, "head")
            command.check(config)
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    "0015_late_bank_evidence"
                )
        finally:
            engine.dispose()
