from __future__ import annotations

import uuid
from contextlib import contextmanager
from datetime import date

import pytest
import sqlalchemy as sa
from _postgres_helpers import catalog_owner_authority, isolated_postgres_url
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from ai_accounting.accounting_periods import canonical_sha256
from ai_accounting.coa import seed_organization
from ai_accounting.component_schemas import ConfigureAccountRequest, RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.event_amendment_schemas import AmendEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.models import (
    Account,
    BusinessEvent,
    BusinessEventComponent,
    BusinessEventDependency,
    DeferredOutputVatTransfer,
    Evidence,
    OpenItem,
    TaxPeriod,
    Voucher,
    VoucherLine,
    ZeroTaxPeriodConfirmation,
)
from ai_accounting.schemas import (
    ReverseEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)
from ai_accounting.service import FinanceService
from ai_accounting.tax_accounts import period_liability_balances
from alembic import command

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


@pytest.fixture
def postgres_engine():
    with isolated_postgres_url("tax_account_component") as database_url:
        config = Config("alembic.ini")
        config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))
        config.attributes["database_url_override"] = database_url
        command.upgrade(config, "head")
        command.check(config)
        engine = sa.create_engine(database_url)
        try:
            yield engine
        finally:
            engine.dispose()


@contextmanager
def _organization_context(engine: sa.Engine, *, label: str, tin: str):
    with Session(engine) as session:
        organization = seed_organization(
            session,
            name=f"VAT component PostgreSQL {label}",
            taxpayer_identification_number=tin,
            accounting_period_control_enabled=False,
        )
        session.commit()
        with catalog_owner_authority(session, organization) as authority:
            with authority.attributed_call(
                session, tool_name="finance_register_tax_component_evidence"
            ):
                evidence = Evidence(
                    org_id=organization.id,
                    original_name=f"vat-component-{label}.txt",
                    storage_path=f"tests/{organization.id}/vat-component.txt",
                    sha256=uuid.uuid4().hex * 2,
                    size_bytes=1,
                    media_type="text/plain",
                    source="postgres-tax-component-test",
                    metadata_json={},
                )
                session.add(evidence)
                session.flush()
            session.commit()
            yield session, organization, evidence, authority


def _configure_vat_account(
    session,
    authority,
    org_id,
    *,
    code: str,
    name: str,
    business_class: str = "vat_payable",
) -> None:
    with authority.attributed_call(session, tool_name="finance_configure_account"):
        configured = ComponentService(session).configure_account(
            ConfigureAccountRequest(
                org_id=org_id,
                idempotency_key=f"configure-{code}",
                code=code,
                name=name,
                business_class=business_class,
            )
        )
    assert configured["code"] == code
    session.commit()


def _tax_facts(invoice_type: str) -> dict[str, object]:
    return {
        "taxable": True,
        "rate_percent": "1",
        "invoice_type": invoice_type,
        "waive_exemption": False,
        "tax_due_on_event": True,
    }


def _sale_component(
    *, key: str, day: str, amount_fen: int, account_code: str, invoice_type: str
) -> dict[str, object]:
    return {
        "key": key,
        "kind": "service_sale",
        "business_date": day,
        "payment_date": day,
        "amount_fen": amount_fen,
        "recognition_basis": "immediate",
        "fulfillment_date": day,
        "tax_obligation_date": day,
        "tax_facts": _tax_facts(invoice_type),
        "account_selections": {"vat_payable": account_code},
        "metadata": {"counterparty": {"kind": "customer", "name": f"customer-{key}"}},
    }


def _record_sales(session, authority, org_id, evidence_id, *, key: str, components):
    total = sum(component["amount_fen"] for component in components)
    request = RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "posting_date": components[0]["business_date"],
            "evidence_references": [evidence_id],
            "components": components,
            "funds": [
                {
                    "key": "receipts",
                    "account_code": "1001",
                    "direction": "receipt",
                    "payment_date": components[0]["business_date"],
                    "amount_fen": total,
                    "allocations": [
                        {
                            "component_key": component["key"],
                            "amount_fen": component["amount_fen"],
                        }
                        for component in components
                    ],
                }
            ],
        }
    )
    with authority.attributed_call(session, tool_name="finance_record_event"):
        result = ComponentService(session).record(request)
    assert result.status == "posted", result
    session.commit()
    return result


def _confirm_period(
    session,
    authority,
    org_id,
    *,
    start_date: date,
    end_date: date,
    key: str,
):
    service = FinanceService(session)
    preview_request = TaxPeriodPreviewRequest(
        org_id=org_id,
        start_date=start_date,
        end_date=end_date,
        adjustment_posting_date=end_date,
    )
    preview = service.preview_tax_period(preview_request)
    with authority.attributed_call(session, tool_name="finance_confirm_tax_period"):
        confirmed = service.confirm_tax_period(
            TaxPeriodConfirmRequest(
                **preview_request.model_dump(),
                calculation_hash=preview["calculation_hash"],
                idempotency_key=key,
            )
        )
    assert confirmed.status == "posted", confirmed
    session.commit()
    period = session.scalar(
        sa.select(TaxPeriod).where(TaxPeriod.adjustment_event_id == confirmed.event_id)
    )
    assert period is not None
    return confirmed, period


def _tax_payment_request(
    org_id,
    evidence_id,
    *,
    key: str,
    day: str,
    amount_fen: int,
    period_start: str,
    period_end: str,
    account_code: str | None = None,
) -> RecordEventRequest:
    component: dict[str, object] = {
        "key": "vat-payment",
        "kind": "tax_settlement",
        "business_date": day,
        "payment_date": day,
        "amount_fen": amount_fen,
        "tax_type": "vat",
        "settlement_kind": "payment",
        "period_start": period_start,
        "period_end": period_end,
    }
    if account_code is not None:
        component["account_selections"] = {"vat_payable": account_code}
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "posting_date": day,
            "evidence_references": [evidence_id],
            "components": [component],
            "funds": [
                {
                    "key": "cash-payment",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": day,
                    "amount_fen": amount_fen,
                    "allocations": [{"component_key": "vat-payment", "amount_fen": amount_fen}],
                }
            ],
        }
    )


def _component_lines(session, event_id, kind: str) -> dict[str, tuple[int, int]]:
    component = session.scalar(
        sa.select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == event_id,
            BusinessEventComponent.kind == kind,
        )
    )
    assert component is not None
    return {
        line.account.code: (line.debit_fen, line.credit_fen)
        for line in session.scalars(
            sa.select(VoucherLine).where(VoucherLine.component_id == component.id)
        )
    }


def _account_liability_balances(session, org_id, codes: set[str]) -> dict[str, int]:
    rows = session.execute(
        sa.select(
            Account.code,
            sa.func.sum(VoucherLine.credit_fen - VoucherLine.debit_fen),
        )
        .join(VoucherLine, VoucherLine.account_id == Account.id)
        .where(Account.org_id == org_id, Account.code.in_(codes))
        .group_by(Account.code)
    )
    return {code: int(balance) for code, balance in rows if balance}


def test_postgres_vat_details_relief_payment_ambiguity_and_ledger_balance(
    postgres_engine,
):
    with _organization_context(postgres_engine, label="details", tin="91330106MA1234567T") as (
        session,
        organization,
        evidence,
        authority,
    ):
        _configure_vat_account(
            session, authority, organization.id, code="222111", name="VAT detail A"
        )
        _configure_vat_account(
            session, authority, organization.id, code="222112", name="VAT detail B"
        )
        components = [
            _sale_component(
                key="ordinary-a",
                day="2026-03-05",
                amount_fen=10_100,
                account_code="222111",
                invoice_type="ordinary",
            ),
            _sale_component(
                key="ordinary-b",
                day="2026-03-05",
                amount_fen=20_200,
                account_code="222112",
                invoice_type="ordinary",
            ),
            _sale_component(
                key="special-a",
                day="2026-03-05",
                amount_fen=30_300,
                account_code="222111",
                invoice_type="special",
            ),
            _sale_component(
                key="special-b",
                day="2026-03-05",
                amount_fen=40_400,
                account_code="222112",
                invoice_type="special",
            ),
        ]
        _record_sales(
            session,
            authority,
            organization.id,
            evidence.id,
            key="postgres-vat-detail-sources",
            components=components,
        )
        confirmed, period = _confirm_period(
            session,
            authority,
            organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            key="postgres-vat-detail-period",
        )
        assert period.calculation["trace"][0]["taxable_component_count"] == 4
        assert period.calculation["trace"][0]["taxable_event_count"] == 1
        assert len(period.calculation["source_event_snapshots"]) == 4
        assert (
            len({source["component_id"] for source in period.calculation["source_event_snapshots"]})
            == 4
        )
        assert _component_lines(session, confirmed.event_id, "tax_relief") == {
            "222102": (0, period.calculation["surtax_total_fen"]),
            "222111": (100, 0),
            "222112": (200, 0),
            "5403": (period.calculation["surtax_total_fen"], 0),
            "6301": (0, 300),
        }
        expected = {"222111": 300, "222112": 400}
        assert period_liability_balances(session, organization.id, period, "vat") == expected
        assert _account_liability_balances(session, organization.id, set(expected)) == expected

        event_count = session.scalar(sa.select(sa.func.count()).select_from(BusinessEvent))
        voucher_count = session.scalar(sa.select(sa.func.count()).select_from(Voucher))
        ambiguous_request = _tax_payment_request(
            organization.id,
            evidence.id,
            key="postgres-ambiguous-partial-vat",
            day="2026-04-01",
            amount_fen=50,
            period_start="2026-01-01",
            period_end="2026-03-31",
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            ambiguous = ComponentService(session).record(ambiguous_request)
        assert ambiguous.status == "needs_information", ambiguous
        assert ambiguous.missing_information == [
            "components.vat-payment.account_selections.vat_payable"
        ]
        assert session.scalar(sa.select(sa.func.count()).select_from(BusinessEvent)) == event_count
        assert session.scalar(sa.select(sa.func.count()).select_from(Voucher)) == voucher_count
        session.rollback()

        full_request = _tax_payment_request(
            organization.id,
            evidence.id,
            key="postgres-full-detail-vat",
            day="2026-04-01",
            amount_fen=700,
            period_start="2026-01-01",
            period_end="2026-03-31",
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            paid = ComponentService(session).record(full_request)
        assert paid.status == "posted", paid
        session.commit()
        assert _component_lines(session, paid.event_id, "tax_settlement") == {
            "222111": (300, 0),
            "222112": (400, 0),
        }
        payment_component = session.scalar(
            sa.select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == paid.event_id,
                BusinessEventComponent.kind == "tax_settlement",
            )
        )
        payment_dependencies = list(
            session.scalars(
                sa.select(BusinessEventDependency).where(
                    BusinessEventDependency.child_component_id == payment_component.id
                )
            )
        )
        assert len(payment_dependencies) == len(period.calculation["source_event_snapshots"]) + 1
        assert period.adjustment_event_id in {
            dependency.parent_event_id for dependency in payment_dependencies
        }
        assert period_liability_balances(session, organization.id, period, "vat") == {}
        assert _account_liability_balances(session, organization.id, set(expected)) == {}

        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            blocked_adjustment_reversal = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=period.adjustment_event_id,
                    idempotency_key="postgres-reverse-paid-vat-adjustment-blocked",
                    reason="active tax payment keeps the assessment dependency locked",
                    posting_date=date(2026, 4, 2),
                )
            )
        assert blocked_adjustment_reversal.status == "rejected"
        assert blocked_adjustment_reversal.errors == ["REVERSE_DEPENDENT_EVENTS_FIRST"]

        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            payment_reversal = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=paid.event_id,
                    idempotency_key="postgres-reverse-full-detail-vat-payment",
                    reason="release the assessment dependency",
                    posting_date=date(2026, 4, 2),
                )
            )
        assert payment_reversal.status == "posted", payment_reversal
        session.commit()

        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            adjustment_reversal = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=period.adjustment_event_id,
                    idempotency_key="postgres-reverse-paid-vat-adjustment-released",
                    reason="payment reversal releases the assessment",
                    posting_date=date(2026, 4, 3),
                )
            )
        assert adjustment_reversal.status == "posted", adjustment_reversal
        assert session.get(TaxPeriod, period.id).status == "reversed"


def test_postgres_full_vat_payment_keeps_signed_refund_source_line(postgres_engine):
    with _organization_context(postgres_engine, label="refund", tin="91330106MA7654321P") as (
        session,
        organization,
        evidence,
        authority,
    ):
        _configure_vat_account(
            session, authority, organization.id, code="222112", name="VAT positive detail"
        )
        original = _record_sales(
            session,
            authority,
            organization.id,
            evidence.id,
            key="postgres-refund-original",
            components=[
                _sale_component(
                    key="original",
                    day="2026-01-15",
                    amount_fen=10_100,
                    account_code="222101",
                    invoice_type="special",
                )
            ],
        )
        source_component = session.scalar(
            sa.select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == original.event_id,
                BusinessEventComponent.key == "original",
            )
        )
        refund_day = "2026-04-15"
        refund_request = RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "postgres-signed-vat-refund-source",
                "posting_date": refund_day,
                "evidence_references": [evidence.id],
                "components": [
                    {
                        "key": "refund",
                        "kind": "customer_refund",
                        "refund_kind": "sale_return",
                        "business_date": refund_day,
                        "payment_date": refund_day,
                        "tax_obligation_date": refund_day,
                        "amount_fen": 10100,
                        "source": {"component_id": source_component.id},
                        "tax_facts": _tax_facts("special"),
                        "metadata": {
                            "counterparty": {"kind": "customer", "name": "customer-original"}
                        },
                    }
                ],
                "funds": [
                    {
                        "key": "refund-cash",
                        "account_code": "1001",
                        "direction": "payment",
                        "payment_date": refund_day,
                        "amount_fen": 10100,
                        "allocations": [{"component_key": "refund", "amount_fen": 10100}],
                    }
                ],
            }
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            refund = ComponentService(session).record(refund_request)
        assert refund.status == "posted", refund
        session.commit()
        _record_sales(
            session,
            authority,
            organization.id,
            evidence.id,
            key="postgres-refund-positive-source",
            components=[
                _sale_component(
                    key="positive",
                    day=refund_day,
                    amount_fen=20_200,
                    account_code="222112",
                    invoice_type="special",
                )
            ],
        )
        _, period = _confirm_period(
            session,
            authority,
            organization.id,
            start_date=date(2026, 4, 1),
            end_date=date(2026, 6, 30),
            key="postgres-signed-vat-period",
        )
        assert period_liability_balances(session, organization.id, period, "vat") == {
            "222101": -100,
            "222112": 200,
        }
        payment = _tax_payment_request(
            organization.id,
            evidence.id,
            key="postgres-signed-vat-payment",
            day="2026-07-01",
            amount_fen=100,
            period_start="2026-04-01",
            period_end="2026-06-30",
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            paid = ComponentService(session).record(payment)
        assert paid.status == "posted", paid
        session.commit()
        assert _component_lines(session, paid.event_id, "tax_settlement") == {
            "222101": (0, 100),
            "222112": (200, 0),
        }
        assert period_liability_balances(session, organization.id, period, "vat") == {}


def test_postgres_deferred_vat_source_resolves_eventual_configured_detail(postgres_engine):
    with _organization_context(postgres_engine, label="deferred", tin="91330106MA1234567T") as (
        session,
        organization,
        evidence,
        authority,
    ):
        _configure_vat_account(
            session,
            authority,
            organization.id,
            code="222109",
            name="Deferred VAT detail",
            business_class="deferred_output_vat",
        )
        _configure_vat_account(
            session,
            authority,
            organization.id,
            code="222119",
            name="Eventual VAT payable detail",
        )
        customer = {"kind": "customer", "name": "deferred-vat-customer"}
        sale_request = RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "postgres-deferred-vat-sale",
                "posting_date": "2026-01-05",
                "evidence_references": [evidence.id],
                "components": [
                    {
                        "key": "sale",
                        "kind": "service_sale",
                        "business_date": "2026-01-05",
                        "amount_fen": 10100,
                        "recognition_basis": "credit",
                        "fulfillment_date": "2026-01-05",
                        "tax_obligation_date": "2026-03-05",
                        "tax_facts": {**_tax_facts("ordinary"), "tax_due_on_event": False},
                        "account_selections": {"deferred_output_vat": "222109"},
                        "metadata": {"counterparty": customer},
                    }
                ],
            }
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            sale = ComponentService(session).record(sale_request)
        assert sale.status == "posted", sale
        session.commit()
        source_component = session.scalar(
            sa.select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == sale.event_id,
                BusinessEventComponent.key == "sale",
            )
        )
        item = session.scalar(
            sa.select(OpenItem).where(OpenItem.source_component_id == source_component.id)
        )
        assert item is not None
        receipt_request = RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "postgres-deferred-vat-receipt",
                "posting_date": "2026-03-05",
                "evidence_references": [evidence.id],
                "components": [
                    {
                        "key": "receipt",
                        "kind": "receivable_settlement",
                        "business_date": "2026-03-05",
                        "payment_date": "2026-03-05",
                        "allocations": [{"open_item_id": item.id, "amount_fen": 10100}],
                        "account_selections": {"vat_payable": "222119"},
                        "metadata": {"counterparty": customer},
                    }
                ],
                "funds": [
                    {
                        "key": "receipt-cash",
                        "account_code": "1001",
                        "direction": "receipt",
                        "payment_date": "2026-03-05",
                        "amount_fen": 10100,
                        "allocations": [{"component_key": "receipt", "amount_fen": 10100}],
                    }
                ],
            }
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            receipt = ComponentService(session).record(receipt_request)
        assert receipt.status == "posted", receipt
        session.commit()
        assert _component_lines(session, receipt.event_id, "receivable_settlement") == {
            "1122": (0, 10_100),
            "222109": (100, 0),
            "222119": (0, 100),
        }
        transfer = session.scalar(
            sa.select(DeferredOutputVatTransfer).where(
                DeferredOutputVatTransfer.source_open_item_id == item.id
            )
        )
        assert transfer is not None and transfer.transfer_event_id == receipt.event_id
        expected_account_id = session.scalar(
            sa.select(Account.id).where(Account.org_id == organization.id, Account.code == "222119")
        )
        resolved_account_id = session.scalar(
            sa.text("SELECT finance_vat_source_account_id(:org_id, :component_id)").bindparams(
                org_id=organization.id,
                component_id=source_component.id,
            )
        )
        assert resolved_account_id == expected_account_id


def test_postgres_tiny_vat_no_adjustment_confirmation_payment_lifecycle(postgres_engine):
    with _organization_context(postgres_engine, label="tiny-vat", tin="91330108MABXE0HA3F") as (
        session,
        organization,
        evidence,
        authority,
    ):
        source_component = _sale_component(
            key="tiny-special",
            day="2026-03-05",
            amount_fen=51,
            account_code="222101",
            invoice_type="special",
        )
        source_request = RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "postgres-tiny-vat-source",
                "posting_date": "2026-03-05",
                "evidence_references": [evidence.id],
                "components": [source_component],
                "funds": [
                    {
                        "key": "tiny-receipt",
                        "account_code": "1001",
                        "direction": "receipt",
                        "payment_date": "2026-03-05",
                        "amount_fen": 51,
                        "allocations": [{"component_key": "tiny-special", "amount_fen": 51}],
                    }
                ],
            }
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            source = ComponentService(session).record(source_request)
        assert source.status == "posted", source
        session.commit()

        def formal_counts() -> tuple[int, ...]:
            return tuple(
                int(session.scalar(sa.select(sa.func.count()).select_from(model)))
                for model in (
                    BusinessEvent,
                    BusinessEventComponent,
                    BusinessEventDependency,
                    Voucher,
                    VoucherLine,
                    ZeroTaxPeriodConfirmation,
                )
            )

        unconfirmed_counts = formal_counts()
        unconfirmed_request = _tax_payment_request(
            organization.id,
            evidence.id,
            key="postgres-tiny-vat-unconfirmed",
            day="2026-04-01",
            amount_fen=1,
            period_start="2026-01-01",
            period_end="2026-03-31",
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            unconfirmed = ComponentService(session).record(unconfirmed_request)
        assert unconfirmed.status == "rejected", unconfirmed
        assert unconfirmed.errors == ["TAX_SETTLEMENT_PERIOD_NOT_CONFIRMED"]
        assert formal_counts() == unconfirmed_counts

        service = FinanceService(session)
        request = TaxPeriodPreviewRequest(
            org_id=organization.id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
        )
        preview = service.preview_tax_period(request)
        assert preview["vat_accrued_fen"] == preview["vat_payable_fen"] == 1
        assert preview["vat_relief_fen"] == preview["surtax_total_fen"] == 0

        voucher_count_before_confirmation = session.scalar(
            sa.select(sa.func.count()).select_from(Voucher)
        )
        with authority.attributed_call(session, tool_name="finance_confirm_tax_period"):
            confirmed = service.confirm_tax_period(
                TaxPeriodConfirmRequest(
                    **request.model_dump(),
                    calculation_hash=preview["calculation_hash"],
                    idempotency_key="postgres-tiny-vat-period",
                )
            )
        assert confirmed.status == "posted", confirmed
        assert confirmed.event_id is None and confirmed.voucher_id is None
        assert session.scalar(sa.select(sa.func.count()).select_from(TaxPeriod)) == 0
        confirmation = session.scalar(sa.select(ZeroTaxPeriodConfirmation))
        assert confirmation is not None
        assert confirmation.calculation["gross_sales_fen"] == 51
        assert confirmation.calculation["net_sales_fen"] == 50
        assert confirmation.calculation["vat_payable_fen"] == 1
        assert confirmation.calculation["vat_relief_fen"] == 0
        assert confirmation.calculation["surtax_total_fen"] == 0
        assert (
            session.scalar(sa.select(sa.func.count()).select_from(Voucher))
            == voucher_count_before_confirmation
        )
        session.commit()

        overpay_counts = formal_counts()
        with authority.attributed_call(session, tool_name="finance_record_event"):
            overpaid = ComponentService(session).record(
                _tax_payment_request(
                    organization.id,
                    evidence.id,
                    key="postgres-tiny-vat-overpay",
                    day="2026-04-01",
                    amount_fen=2,
                    period_start="2026-01-01",
                    period_end="2026-03-31",
                )
            )
        assert overpaid.status == "rejected", overpaid
        assert overpaid.errors == ["TAX_SETTLEMENT_EXCEEDS_PERIOD_BALANCE"]
        assert formal_counts() == overpay_counts

        same_event_counts = formal_counts()
        same_event_request = RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "postgres-tiny-vat-same-event-stale",
                "posting_date": "2026-04-01",
                "evidence_references": [evidence.id],
                "components": [
                    _tax_payment_request(
                        organization.id,
                        evidence.id,
                        key="ignored",
                        day="2026-04-01",
                        amount_fen=1,
                        period_start="2026-01-01",
                        period_end="2026-03-31",
                    )
                    .components[0]
                    .model_dump(mode="json"),
                    _sale_component(
                        key="same-event-tiny",
                        day="2026-03-06",
                        amount_fen=51,
                        account_code="222101",
                        invoice_type="special",
                    )
                    | {"payment_date": "2026-04-01"},
                ],
                "funds": [
                    {
                        "key": "tax-cash",
                        "account_code": "1001",
                        "direction": "payment",
                        "payment_date": "2026-04-01",
                        "amount_fen": 1,
                        "allocations": [{"component_key": "vat-payment", "amount_fen": 1}],
                    },
                    {
                        "key": "sale-cash",
                        "account_code": "1001",
                        "direction": "receipt",
                        "payment_date": "2026-04-01",
                        "amount_fen": 51,
                        "allocations": [{"component_key": "same-event-tiny", "amount_fen": 51}],
                    },
                ],
            }
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            same_event = ComponentService(session).record(same_event_request)
        assert same_event.status == "rejected", same_event
        assert same_event.errors == ["TAX_PERIOD_CALCULATION_STALE"]
        assert formal_counts() == same_event_counts

        payment_request = _tax_payment_request(
            organization.id,
            evidence.id,
            key="postgres-tiny-vat-payment",
            day="2026-04-01",
            amount_fen=1,
            period_start="2026-01-01",
            period_end="2026-03-31",
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            paid = ComponentService(session).record(payment_request)
        assert paid.status == "posted", paid
        session.commit()
        paid_counts = formal_counts()
        with authority.attributed_call(session, tool_name="finance_record_event"):
            replay = ComponentService(session).record(payment_request)
        assert replay.status == "posted" and replay.event_id == paid.event_id
        assert formal_counts() == paid_counts
        assert _component_lines(session, paid.event_id, "tax_settlement") == {"222101": (1, 0)}
        payment_component = session.scalar(
            sa.select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == paid.event_id,
                BusinessEventComponent.kind == "tax_settlement",
            )
        )
        source_row = session.scalar(
            sa.select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == source.event_id,
                BusinessEventComponent.key == "tiny-special",
            )
        )
        dependency = session.scalar(
            sa.select(BusinessEventDependency).where(
                BusinessEventDependency.child_component_id == payment_component.id
            )
        )
        assert dependency.parent_component_id == source_row.id
        assert dependency.parent_event_id == source.event_id
        assert dependency.amount_fen == 1
        assert payment_component.derived["tax_confirmation_id"] == str(confirmation.id)
        assert payment_component.derived["tax_confirmation_hash"] == confirmation.calculation_hash
        session.execute(
            sa.text("SELECT finance_assert_no_adjustment_tax_settlement(:component_id)").bindparams(
                component_id=payment_component.id
            )
        )
        funds_component = session.scalar(
            sa.select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == source.event_id,
                BusinessEventComponent.kind == "funds",
            )
        )
        exact_source_counts = formal_counts()
        with authority.attributed_call(session, tool_name="finance_test_tax_source_lineage"):
            with pytest.raises(DBAPIError, match="TAX_SETTLEMENT_CONFIRMATION_SOURCE_MISMATCH"):
                with session.begin_nested():
                    session.add(
                        BusinessEventDependency(
                            org_id=organization.id,
                            parent_event_id=source.event_id,
                            child_event_id=paid.event_id,
                            dependency_kind="component_source",
                            parent_component_id=funds_component.id,
                            child_component_id=payment_component.id,
                            amount_fen=1,
                        )
                    )
                    session.flush()
                    session.execute(
                        sa.text(
                            "SELECT finance_assert_no_adjustment_tax_settlement(:component_id)"
                        ).bindparams(component_id=payment_component.id)
                    )
        assert formal_counts() == exact_source_counts

        locked_counts = formal_counts()
        new_source_request = RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "postgres-active-payment-new-source",
                "posting_date": "2026-03-06",
                "evidence_references": [evidence.id],
                "components": [
                    _sale_component(
                        key="locked-new-source",
                        day="2026-03-06",
                        amount_fen=51,
                        account_code="222101",
                        invoice_type="special",
                    )
                ],
                "funds": [
                    {
                        "key": "locked-receipt",
                        "account_code": "1001",
                        "direction": "receipt",
                        "payment_date": "2026-03-06",
                        "amount_fen": 51,
                        "allocations": [{"component_key": "locked-new-source", "amount_fen": 51}],
                    }
                ],
            }
        )
        with authority.attributed_call(session, tool_name="finance_record_event"):
            new_source_locked = ComponentService(session).record(new_source_request)
        assert new_source_locked.status == "rejected", new_source_locked
        assert new_source_locked.errors == ["TAX_PERIOD_SOURCE_LOCKED"]
        assert formal_counts() == locked_counts

        assert (
            session.scalar(
                sa.text(
                    "SELECT finance_no_adjustment_tax_payment_locks_date(:org_id, :tax_date)"
                ).bindparams(org_id=organization.id, tax_date=date(2026, 3, 5))
            )
            is True
        )
        with authority.attributed_call(session, tool_name="finance_test_tax_source_guard"):
            with pytest.raises(DBAPIError, match="TAX_PERIOD_SOURCE_LOCKED"):
                with session.begin_nested():
                    session.execute(
                        sa.update(BusinessEvent)
                        .where(BusinessEvent.id == source.event_id)
                        .values(status="reversed")
                    )
        assert formal_counts() == locked_counts

        replacement = source_request.model_copy(
            update={
                "components": [source_request.components[0].model_copy(update={"amount_fen": 52})],
                "funds": [
                    source_request.funds[0].model_copy(
                        update={
                            "amount_fen": 52,
                            "allocations": [
                                source_request.funds[0]
                                .allocations[0]
                                .model_copy(update={"amount_fen": 52})
                            ],
                        }
                    )
                ],
            }
        )
        with authority.attributed_call(session, tool_name="finance_amend_event"):
            amendment = EventAmendmentService(session).amend(
                AmendEventRequest(
                    org_id=organization.id,
                    event_id=source.event_id,
                    idempotency_key="amend-locked-tiny-source",
                    expected_facts_hash=canonical_sha256(
                        session.get(BusinessEvent, source.event_id).facts
                    ),
                    reason="active tax payment must freeze source",
                    replacement=replacement,
                )
            )
        assert amendment["status"] == "rejected", amendment
        assert amendment["errors"] == ["AMENDMENT_DEPENDENT_FACTS_EXIST"]
        assert formal_counts() == locked_counts

        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            source_reversal_blocked = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=source.event_id,
                    idempotency_key="reverse-locked-tiny-source",
                    reason="active tax payment must freeze source",
                    posting_date="2026-04-02",
                )
            )
        assert source_reversal_blocked.status == "rejected", source_reversal_blocked
        assert source_reversal_blocked.errors == ["REVERSE_DEPENDENT_EVENTS_FIRST"]
        assert formal_counts() == locked_counts

        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            payment_reversal = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=paid.event_id,
                    idempotency_key="reverse-tiny-vat-payment",
                    reason="correct tax payment",
                    posting_date="2026-04-02",
                )
            )
        assert payment_reversal.status == "posted", payment_reversal
        session.commit()
        assert (
            session.scalar(
                sa.text(
                    "SELECT finance_no_adjustment_tax_payment_locks_date(:org_id, :tax_date)"
                ).bindparams(org_id=organization.id, tax_date=date(2026, 3, 5))
            )
            is False
        )

        with authority.attributed_call(session, tool_name="finance_amend_event"):
            amended_source = EventAmendmentService(session).amend(
                AmendEventRequest(
                    org_id=organization.id,
                    event_id=source.event_id,
                    idempotency_key="amend-unlocked-tiny-source",
                    expected_facts_hash=canonical_sha256(
                        session.get(BusinessEvent, source.event_id).facts
                    ),
                    reason="reversed payment releases source amendment lock",
                    replacement=replacement,
                )
            )
        assert amended_source["status"] == "posted", amended_source
        session.commit()
        amended_component = session.scalar(
            sa.select(BusinessEventComponent).where(
                BusinessEventComponent.event_id == source.event_id,
                BusinessEventComponent.key == "tiny-special",
            )
        )
        historical_dependency = session.get(BusinessEventDependency, dependency.id)
        assert amended_component.id == source_row.id
        assert historical_dependency.parent_component_id == amended_component.id
        assert session.get(BusinessEvent, paid.event_id).status == "reversed"

        second_source = _record_sales(
            session,
            authority,
            organization.id,
            evidence.id,
            key="postgres-second-tiny-vat-source",
            components=[
                _sale_component(
                    key="second-tiny",
                    day="2026-03-06",
                    amount_fen=51,
                    account_code="222101",
                    invoice_type="special",
                )
            ],
        )
        assert second_source.status == "posted"

        stale_counts = formal_counts()
        with authority.attributed_call(session, tool_name="finance_record_event"):
            stale = ComponentService(session).record(
                _tax_payment_request(
                    organization.id,
                    evidence.id,
                    key="postgres-stale-tiny-vat-payment",
                    day="2026-04-03",
                    amount_fen=2,
                    period_start="2026-01-01",
                    period_end="2026-03-31",
                )
            )
        assert stale.status == "rejected", stale
        assert stale.errors == ["TAX_PERIOD_CALCULATION_STALE"]
        assert formal_counts() == stale_counts

        fresh_preview = service.preview_tax_period(request)
        with authority.attributed_call(session, tool_name="finance_confirm_tax_period"):
            fresh_confirmation = service.confirm_tax_period(
                TaxPeriodConfirmRequest(
                    **request.model_dump(),
                    calculation_hash=fresh_preview["calculation_hash"],
                    idempotency_key="postgres-fresh-tiny-vat-period",
                )
            )
        assert fresh_confirmation.status == "posted", fresh_confirmation
        assert fresh_confirmation.event_id is None and fresh_confirmation.voucher_id is None
        session.commit()

        with authority.attributed_call(session, tool_name="finance_reverse_event"):
            source_reversed = FinanceService(session).reverse_event(
                ReverseEventRequest(
                    org_id=organization.id,
                    event_id=source.event_id,
                    idempotency_key="reverse-unlocked-tiny-source",
                    reason="reversed payment releases active source lock",
                    posting_date="2026-04-03",
                )
            )
        assert source_reversed.status == "posted", source_reversed
        session.commit()
