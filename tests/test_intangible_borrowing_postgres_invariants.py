from __future__ import annotations

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from decimal import Decimal
from hashlib import sha256
from threading import Barrier

import pytest
import sqlalchemy as sa
from alembic.config import Config
from conftest import authenticate_and_confirm_bank_scope
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import sessionmaker
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.borrowing_schemas import (
    ConfirmBorrowingInterestRequest,
    DrawBorrowingRequest,
    PayBorrowingInterestRequest,
    PreviewBorrowingInterestRequest,
    RepayBorrowingPrincipalRequest,
)
from ai_accounting.borrowing_service import BorrowingService
from ai_accounting.coa import seed_organization
from ai_accounting.intangible_asset_schemas import (
    AcquireIntangibleAssetRequest,
    ConfirmIntangibleAssetAmortizationRequest,
    PreviewIntangibleAssetAmortizationRequest,
    RetireIntangibleAssetRequest,
)
from ai_accounting.intangible_asset_service import IntangibleAssetService
from ai_accounting.models import (
    Borrowing,
    BorrowingInterestAccrual,
    BorrowingPayment,
    BusinessEvent,
    Evidence,
    IntangibleAsset,
    IntangibleAssetAmortization,
    IntangibleAssetRetirement,
    Organization,
)
from ai_accounting.schemas import ReverseEventRequest
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _draw_request(*, org_id: uuid.UUID, evidence_id: uuid.UUID) -> DrawBorrowingRequest:
    return DrawBorrowingRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": "pg-large-rate-draw",
            "borrowing_code": "PG-RATE-001",
            "contract_name": "PostgreSQL 大额本金精度合同",
            "lender": {"name": "PostgreSQL 精度测试银行", "external_ref": "PG-BANK-1"},
            "lender_is_licensed_financial_institution": True,
            "currency": "CNY",
            "principal_fen": 9_000_000_000_000_000_000,
            "drawdown_date": "2026-01-01",
            "due_date": "2026-07-01",
            "posting_date": "2026-01-01",
            "annual_rate_percent": "3.650000",
            "day_count_basis": "actual_365",
            "interest_due_dates": ["2026-07-01"],
            "capitalization_applicable": False,
            "purpose_description": "验证 PostgreSQL 大额本金利息精度",
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
            "bank_account_code": "1002",
            "evidence_references": [evidence_id],
        }
    )


def test_postgres_rate_hash_identity_immutability_and_nonposted_concurrency() -> None:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        command.check(_config(database_url))
        engine = sa.create_engine(database_url)
        factory = sessionmaker(engine, expire_on_commit=False)
        try:
            with factory() as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    accounting_period_control_enabled=False,
                    name="PG 无形资产借款闭包",
                )
                evidence = Evidence(
                    org_id=organization.id,
                    sha256=sha256(b"pg-borrowing-contract").hexdigest(),
                    original_name="contract.pdf",
                    media_type="application/pdf",
                    source="test",
                    size_bytes=1,
                    storage_path="test/pg-borrowing-contract.pdf",
                )
                intangible_evidence = Evidence(
                    org_id=organization.id,
                    sha256=sha256(b"pg-intangible-contract").hexdigest(),
                    original_name="intangible-contract.pdf",
                    media_type="application/pdf",
                    source="test",
                    size_bytes=1,
                    storage_path="test/pg-intangible-contract.pdf",
                )
                retirement_evidence = Evidence(
                    org_id=organization.id,
                    sha256=sha256(b"pg-intangible-retirement").hexdigest(),
                    original_name="intangible-retirement.pdf",
                    media_type="application/pdf",
                    source="test",
                    size_bytes=1,
                    storage_path="test/pg-intangible-retirement.pdf",
                )
                session.add_all([evidence, intangible_evidence, retirement_evidence])
                session.flush()
                org_id, evidence_id, intangible_evidence_id, retirement_evidence_id = (
                    organization.id,
                    evidence.id,
                    intangible_evidence.id,
                    retirement_evidence.id,
                )
                session.commit()

            with factory() as session:
                acquired = IntangibleAssetService(session).acquire_intangible_asset(
                    AcquireIntangibleAssetRequest.model_validate(
                        {
                            "org_id": org_id,
                            "idempotency_key": "pg-intangible-acquisition",
                            "asset_code": "PG-IA-001",
                            "asset_name": "PostgreSQL 软件许可",
                            "category": "software",
                            "rights_description": "十二个月软件使用许可",
                            "supplier": {
                                "kind": "supplier",
                                "name": "PostgreSQL 软件供应商",
                                "external_ref": "PG-SUPPLIER-1",
                            },
                            "acquisition_date": "2026-01-02",
                            "available_for_use_date": "2026-01-02",
                            "posting_date": "2026-01-02",
                            "cost_components": {
                                "purchase_price_fen": 11_000,
                                "noncreditable_tax_fen": 500,
                                "directly_attributable_cost_fen": 500,
                            },
                            "settlement_method": "payable",
                            "due_date": "2026-02-02",
                            "benefit_area": "management",
                            "life_basis": "legal_or_contractual",
                            "useful_life_months": 12,
                            "life_basis_explanation": "合同约定十二个月许可期",
                            "is_available_for_use": True,
                            "claims_creditable_input_vat": False,
                            "evidence_references": [intangible_evidence_id],
                        }
                    )
                )
                assert acquired.status == "posted", acquired.errors
                session.commit()
                acquired_asset_id = acquired.asset_id
                acquisition_event_id = acquired.event_id
                supplier_id = session.get(IntangibleAsset, acquired_asset_id).supplier_id

            with pytest.raises(DBAPIError, match="INTANGIBLE_ASSET_ACQUISITION_FACT_SHAPE_INVALID"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            "UPDATE counterparties SET external_ref = 'changed' WHERE id = :id"
                        ),
                        {"id": supplier_id},
                    )

            with factory() as session:
                preview_request = PreviewIntangibleAssetAmortizationRequest(
                    org_id=org_id,
                    asset_id=acquired_asset_id,
                    amortization_period="2026-01",
                    posting_date=date(2026, 1, 31),
                )
                preview = IntangibleAssetService(session).preview_intangible_asset_amortization(
                    preview_request
                )
                amortized = IntangibleAssetService(session).confirm_intangible_asset_amortization(
                    ConfirmIntangibleAssetAmortizationRequest(
                        **preview_request.model_dump(),
                        idempotency_key="pg-intangible-amortization",
                        calculation_hash=preview.calculation_hash,
                    )
                )
                assert amortized.status == "posted", amortized.errors
                session.commit()
                amortization_event_id = amortized.event_id
                amortization = session.scalar(
                    sa.select(IntangibleAssetAmortization).where(
                        IntangibleAssetAmortization.event_id == amortization_event_id
                    )
                )
                assert amortization.amount_fen == 1_000

            with factory() as session:
                retired = IntangibleAssetService(session).retire_intangible_asset(
                    RetireIntangibleAssetRequest(
                        org_id=org_id,
                        asset_id=acquired_asset_id,
                        idempotency_key="pg-intangible-retirement",
                        retirement_date=date(2026, 1, 31),
                        posting_date=date(2026, 1, 31),
                        gross_proceeds_fen=0,
                        compensation_fen=0,
                        taxes_and_fees_fen=0,
                        residual_proceeds_fen=0,
                        evidence_references=[retirement_evidence_id],
                    )
                )
                assert retired.status == "posted", retired.errors
                session.commit()
                retirement_event_id = retired.event_id
                retirement = session.scalar(
                    sa.select(IntangibleAssetRetirement).where(
                        IntangibleAssetRetirement.event_id == retirement_event_id
                    )
                )
                assert retirement.accumulated_amortization_fen == 1_000
                assert retirement.book_value_fen == 11_000

            with factory() as session:
                blocked = IntangibleAssetService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=acquisition_event_id,
                        idempotency_key="pg-reverse-intangible-blocked",
                        reason="reverse order must be downstream first",
                        posting_date=date(2026, 2, 1),
                    )
                )
                assert blocked.errors == ["INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST"]
                session.commit()

            for index, event_id in enumerate(
                (retirement_event_id, amortization_event_id, acquisition_event_id), start=1
            ):
                with factory() as session:
                    reversed_result = IntangibleAssetService(session).reverse_event(
                        ReverseEventRequest(
                            org_id=org_id,
                            event_id=event_id,
                            idempotency_key=f"pg-reverse-intangible-{index}",
                            reason="validated downstream-first reversal",
                            posting_date=date(2026, 2, index + 1),
                        )
                    )
                    assert reversed_result.status == "posted", reversed_result.errors
                    session.commit()

            with factory() as session:
                organization = session.get(Organization, org_id)
                evidence = session.get(Evidence, evidence_id)
                authority = authenticate_and_confirm_bank_scope(
                    session,
                    organization,
                    evidence_id=evidence.id,
                    accounts=[
                        {
                            "bank_account_code": "1002",
                            "account_name": "银行存款",
                            "start_date": date(2026, 1, 1),
                        }
                    ],
                )
                session.commit()

            with factory() as session:
                with authority.attributed_call(session, tool_name="finance_draw_borrowing"):
                    drawn = BorrowingService(session).draw_borrowing(
                        _draw_request(org_id=org_id, evidence_id=evidence_id)
                    )
                assert drawn.status == "posted", drawn.errors
                session.commit()
                borrowing_id = drawn.borrowing_id

            with factory() as session:
                borrowing = session.get(Borrowing, borrowing_id)
                assert borrowing.annual_rate_percent == Decimal("3.650000")
                preview = BorrowingService(session).preview_borrowing_interest(
                    PreviewBorrowingInterestRequest(
                        org_id=org_id,
                        borrowing_id=borrowing_id,
                        period_start=date(2026, 1, 1),
                        period_end=date(2026, 7, 1),
                    )
                )
                with authority.attributed_call(
                    session, tool_name="finance_confirm_borrowing_interest"
                ):
                    confirmed = BorrowingService(session).confirm_borrowing_interest(
                        ConfirmBorrowingInterestRequest(
                            org_id=org_id,
                            borrowing_id=borrowing_id,
                            period_start=date(2026, 1, 1),
                            period_end=date(2026, 7, 1),
                            calculation_hash=preview.calculation_hash,
                            idempotency_key="pg-large-rate-interest",
                        )
                    )
                assert confirmed.status == "posted", confirmed.errors
                session.commit()
                accrual_event_id = confirmed.event_id

            with factory() as session:
                accrual = session.scalar(
                    sa.select(BorrowingInterestAccrual).where(
                        BorrowingInterestAccrual.event_id == accrual_event_id
                    )
                )
                event = session.get(BusinessEvent, accrual_event_id)
                lender_id = session.get(Borrowing, borrowing_id).lender_id
                assert accrual.annual_rate_percent == Decimal("3.650000")
                assert accrual.amount_fen == 162_900_000_000_000_000
                assert event.business_date == accrual.period_start
                assert event.facts["calculation"]["annual_rate_percent"] == "3.650000"
                assert event.facts["calculation"]["drawdown_event_id"] == str(
                    session.get(Borrowing, borrowing_id).drawdown_event_id
                )
                assert event.facts["calculation"]["prior_active_accrual_event_ids"] == []
                assert event.facts["_result_data"]["calculation_hash"] == accrual.calculation_hash

            with factory() as session:
                late_evidence = Evidence(
                    org_id=org_id,
                    sha256=sha256(b"pg-late-interest-evidence").hexdigest(),
                    original_name="late-interest.pdf",
                    media_type="application/pdf",
                    source="test",
                    size_bytes=1,
                    storage_path="test/pg-late-interest.pdf",
                )
                with authority.attributed_call(
                    session, tool_name="finance_register_evidence"
                ) as attribution:
                    late_evidence.execution_attribution_id = attribution.id
                    session.add(late_evidence)
                    session.flush()
                with authority.attributed_call(session, tool_name="finance_pay_borrowing_interest"):
                    late = BorrowingService(session).pay_borrowing_interest(
                        PayBorrowingInterestRequest(
                            org_id=org_id,
                            borrowing_id=borrowing_id,
                            accrual_event_id=accrual_event_id,
                            idempotency_key="pg-late-interest-payment",
                            payment_date=date(2026, 7, 2),
                            posting_date=date(2026, 7, 2),
                            bank_account_code="1002",
                            evidence_references=[late_evidence.id],
                        )
                    )
                assert late.errors == ["BORROWING_INTEREST_PAYMENT_DATE_INVALID"]
                session.commit()
                assert (
                    session.scalar(
                        sa.select(sa.func.count())
                        .select_from(BorrowingPayment)
                        .where(BorrowingPayment.borrowing_id == borrowing_id)
                    )
                    == 0
                )

            with pytest.raises(DBAPIError, match="IMMUTABLE"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text("UPDATE borrowings SET contract_name = 'tampered' WHERE id = :id"),
                        {"id": borrowing_id},
                    )

            with pytest.raises(DBAPIError, match="BORROWING_DRAWDOWN_FACT_SHAPE_INVALID"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text("UPDATE counterparties SET name = 'changed' WHERE id = :id"),
                        {"id": lender_id},
                    )

            with factory() as session:
                payment_evidence = Evidence(
                    org_id=org_id,
                    sha256=sha256(b"pg-interest-payment-evidence").hexdigest(),
                    original_name="interest-payment.pdf",
                    media_type="application/pdf",
                    source="test",
                    size_bytes=1,
                    storage_path="test/pg-interest-payment.pdf",
                )
                with authority.attributed_call(
                    session, tool_name="finance_register_evidence"
                ) as attribution:
                    payment_evidence.execution_attribution_id = attribution.id
                    session.add(payment_evidence)
                    session.flush()
                with authority.attributed_call(session, tool_name="finance_pay_borrowing_interest"):
                    paid = BorrowingService(session).pay_borrowing_interest(
                        PayBorrowingInterestRequest(
                            org_id=org_id,
                            borrowing_id=borrowing_id,
                            accrual_event_id=accrual_event_id,
                            idempotency_key="pg-interest-payment",
                            payment_date=date(2026, 7, 1),
                            posting_date=date(2026, 7, 1),
                            bank_account_code="1002",
                            evidence_references=[payment_evidence.id],
                        )
                    )
                assert paid.status == "posted", paid.errors
                session.commit()
                payment_event_id = paid.event_id

            with factory() as session:
                principal_evidence = Evidence(
                    org_id=org_id,
                    sha256=sha256(b"pg-principal-payment-evidence").hexdigest(),
                    original_name="principal-payment.pdf",
                    media_type="application/pdf",
                    source="test",
                    size_bytes=1,
                    storage_path="test/pg-principal-payment.pdf",
                )
                with authority.attributed_call(
                    session, tool_name="finance_register_evidence"
                ) as attribution:
                    principal_evidence.execution_attribution_id = attribution.id
                    session.add(principal_evidence)
                    session.flush()
                with authority.attributed_call(
                    session, tool_name="finance_repay_borrowing_principal"
                ):
                    repaid = BorrowingService(session).repay_borrowing_principal(
                        RepayBorrowingPrincipalRequest(
                            org_id=org_id,
                            borrowing_id=borrowing_id,
                            idempotency_key="pg-principal-repayment",
                            repayment_date=date(2026, 7, 1),
                            posting_date=date(2026, 7, 1),
                            bank_account_code="1002",
                            evidence_references=[principal_evidence.id],
                        )
                    )
                assert repaid.status == "posted", repaid.errors
                session.commit()
                repayment_event_id = repaid.event_id

            with factory() as session:
                with authority.attributed_call(session, tool_name="finance_reverse_event"):
                    blocked = BorrowingService(session).reverse_event(
                        ReverseEventRequest(
                            org_id=org_id,
                            event_id=drawn.event_id,
                            idempotency_key="pg-reverse-borrowing-blocked",
                            reason="reverse order must be downstream first",
                            posting_date=date(2026, 7, 2),
                        )
                    )
                assert blocked.errors == ["BORROWING_OPEN_DEPENDENCIES_EXIST"]
                session.commit()

            for index, event_id in enumerate(
                (repayment_event_id, payment_event_id, accrual_event_id, drawn.event_id),
                start=1,
            ):
                with factory() as session:
                    with authority.attributed_call(session, tool_name="finance_reverse_event"):
                        reversed_result = BorrowingService(session).reverse_event(
                            ReverseEventRequest(
                                org_id=org_id,
                                event_id=event_id,
                                idempotency_key=f"pg-reverse-borrowing-{index}",
                                reason="validated downstream-first reversal",
                                posting_date=date(2026, 7, index + 2),
                            )
                        )
                    assert reversed_result.status == "posted", reversed_result.errors
                    session.commit()

            barrier = Barrier(2)

            def store_missing() -> tuple[str, uuid.UUID | None]:
                with factory() as session:
                    barrier.wait()
                    with authority.attributed_call(
                        session, tool_name="finance_acquire_intangible_asset"
                    ):
                        result = IntangibleAssetService(session).acquire_intangible_asset(
                            AcquireIntangibleAssetRequest(
                                org_id=org_id,
                                idempotency_key="pg-concurrent-missing",
                            )
                        )
                    session.commit()
                    return result.status.value, result.event_id

            with ThreadPoolExecutor(max_workers=2) as pool:
                results = list(pool.map(lambda _index: store_missing(), range(2)))
            assert {status for status, _event_id in results} == {"needs_information"}
            assert len({event_id for _status, event_id in results}) == 1

            with factory() as session:
                with authority.attributed_call(
                    session, tool_name="finance_acquire_intangible_asset"
                ):
                    changed = IntangibleAssetService(session).acquire_intangible_asset(
                        AcquireIntangibleAssetRequest(
                            org_id=org_id,
                            idempotency_key="pg-concurrent-missing",
                            asset_code="DIFFERENT-PAYLOAD",
                        )
                    )
                session.commit()
                assert changed.errors == ["INTANGIBLE_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"]
                assert (
                    session.scalar(
                        sa.select(sa.func.count())
                        .select_from(BusinessEvent)
                        .where(
                            BusinessEvent.org_id == org_id,
                            BusinessEvent.idempotency_key == "pg-concurrent-missing",
                        )
                    )
                    == 1
                )

        finally:
            engine.dispose()
