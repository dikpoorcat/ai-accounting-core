from __future__ import annotations

from datetime import date

import pytest
from sqlalchemy import select

from ai_accounting.component_schemas import ConfigureAccountRequest, RecordEventRequest
from ai_accounting.component_service import ComponentService
from ai_accounting.models import (
    BusinessEventComponent,
    DeferredOutputVatTransfer,
    Evidence,
    OpenItem,
    TaxPeriod,
    VoucherLine,
)
from ai_accounting.schemas import TaxPeriodConfirmRequest, TaxPeriodPreviewRequest
from ai_accounting.service import FinanceService
from ai_accounting.tax_accounts import period_liability_balances, vat_source_accounts


@pytest.fixture
def tax_evidence(session, organization):
    evidence = Evidence(
        org_id=organization.id,
        original_name="tax-account-source.txt",
        storage_path="tax-account-source.txt",
        sha256="7" * 64,
        size_bytes=1,
        media_type="text/plain",
        source="test",
    )
    session.add(evidence)
    session.flush()
    return evidence


def _configure(service, organization, *, code, name, business_class):
    return service.configure_account(
        ConfigureAccountRequest(
            org_id=organization.id,
            idempotency_key=f"configure-{code}",
            code=code,
            name=name,
            business_class=business_class,
        )
    )


def _tax_source_request(organization, evidence, *, key, invoice_type):
    customer = {"kind": "customer", "name": f"tax-source-{key}"}
    day = "2026-03-05"
    tax_facts = {
        "taxable": True,
        "rate_percent": "1",
        "invoice_type": invoice_type,
        "waive_exemption": False,
        "tax_due_on_event": True,
    }
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "posting_date": day,
            "evidence_references": [evidence.id],
            "components": [
                {
                    "key": "sale-a",
                    "kind": "service_sale",
                    "business_date": day,
                    "payment_date": day,
                    "amount_fen": 10100,
                    "recognition_basis": "immediate",
                    "fulfillment_date": day,
                    "tax_obligation_date": day,
                    "tax_facts": tax_facts,
                    "account_selections": {"vat_payable": "222111"},
                    "metadata": {"counterparty": customer},
                },
                {
                    "key": "advance-b",
                    "kind": "customer_advance",
                    "business_date": day,
                    "payment_date": day,
                    "amount_fen": 20200,
                    "tax_obligation_date": day,
                    "tax_facts": tax_facts,
                    "account_selections": {"vat_payable": "222112"},
                    "metadata": {"counterparty": customer},
                },
            ],
            "funds": [
                {
                    "key": "receipts",
                    "account_code": "1001",
                    "direction": "receipt",
                    "payment_date": day,
                    "amount_fen": 30300,
                    "allocations": [
                        {"component_key": "sale-a", "amount_fen": 10100},
                        {"component_key": "advance-b", "amount_fen": 20200},
                    ],
                }
            ],
        }
    )


def _prepare_sources(session, organization, evidence, *, key, invoice_type):
    service = ComponentService(session)
    _configure(
        service,
        organization,
        code="222111",
        name="销项税额明细甲",
        business_class="vat_payable",
    )
    _configure(
        service,
        organization,
        code="222112",
        name="销项税额明细乙",
        business_class="vat_payable",
    )
    result = service.record(
        _tax_source_request(organization, evidence, key=key, invoice_type=invoice_type)
    )
    assert result.status == "posted", result
    return result


def _confirm_q1(session, organization, *, key):
    service = FinanceService(session)
    preview_request = TaxPeriodPreviewRequest(
        org_id=organization.id,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        adjustment_posting_date=date(2026, 3, 31),
    )
    preview = service.preview_tax_period(preview_request)
    result = service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            **preview_request.model_dump(),
            calculation_hash=preview["calculation_hash"],
            idempotency_key=key,
        )
    )
    assert result.status == "posted", result
    period = session.scalar(
        select(TaxPeriod).where(TaxPeriod.adjustment_event_id == result.event_id)
    )
    assert period is not None
    return result, period


def _tax_payment_request(
    organization,
    evidence,
    *,
    key,
    amount_fen,
    tax_type="vat",
    account_code=None,
):
    component = {
        "key": "pay-tax",
        "kind": "tax_settlement",
        "business_date": "2026-04-01",
        "payment_date": "2026-04-01",
        "amount_fen": amount_fen,
        "tax_type": tax_type,
        "settlement_kind": "payment",
        "period_start": "2026-01-01",
        "period_end": "2026-03-31",
    }
    if account_code:
        component["account_selections"] = {
            "vat_payable" if tax_type == "vat" else "surtax_payable": account_code
        }
    return RecordEventRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": key,
            "posting_date": "2026-04-01",
            "evidence_references": [evidence.id],
            "components": [component],
            "funds": [
                {
                    "key": "payment",
                    "account_code": "1001",
                    "direction": "payment",
                    "payment_date": "2026-04-01",
                    "amount_fen": amount_fen,
                    "allocations": [{"component_key": "pay-tax", "amount_fen": amount_fen}],
                }
            ],
        }
    )


def _component_line_amounts(session, event_id, kind):
    component = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == event_id,
            BusinessEventComponent.kind == kind,
        )
    )
    assert component is not None
    return {
        line.account.code: (line.debit_fen, line.credit_fen)
        for line in session.scalars(
            select(VoucherLine).where(VoucherLine.component_id == component.id)
        )
    }


def test_tax_period_relief_clears_each_configured_vat_detail(session, organization, tax_evidence):
    _prepare_sources(
        session,
        organization,
        tax_evidence,
        key="relief-detail-sources",
        invoice_type="ordinary",
    )

    confirmed, period = _confirm_q1(session, organization, key="relief-detail-period")

    lines = _component_line_amounts(session, confirmed.event_id, "tax_relief")
    assert lines["222111"] == (100, 0)
    assert lines["222112"] == (200, 0)
    assert lines["6301"] == (0, 300)
    assert period_liability_balances(session, organization.id, period, "vat") == {}


def test_full_vat_and_surtax_payment_use_period_source_liabilities(
    session, organization, tax_evidence
):
    _prepare_sources(
        session,
        organization,
        tax_evidence,
        key="payable-detail-sources",
        invoice_type="special",
    )
    _, period = _confirm_q1(session, organization, key="payable-detail-period")
    assert period.calculation["vat_payable_fen"] == 300
    assert period.calculation["surtax_total_fen"] > 0

    vat_payment = ComponentService(session).record(
        _tax_payment_request(
            organization,
            tax_evidence,
            key="full-detail-vat-payment",
            amount_fen=300,
        )
    )
    assert vat_payment.status == "posted", vat_payment
    vat_lines = _component_line_amounts(session, vat_payment.event_id, "tax_settlement")
    assert vat_lines == {"222111": (100, 0), "222112": (200, 0)}

    surtax_amount = period.calculation["surtax_total_fen"]
    surtax_payment = ComponentService(session).record(
        _tax_payment_request(
            organization,
            tax_evidence,
            key="full-surtax-payment",
            amount_fen=surtax_amount,
            tax_type="surtax",
        )
    )
    assert surtax_payment.status == "posted", surtax_payment
    assert _component_line_amounts(session, surtax_payment.event_id, "tax_settlement") == {
        "222102": (surtax_amount, 0)
    }
    assert period_liability_balances(session, organization.id, period, "vat") == {}
    assert period_liability_balances(session, organization.id, period, "surtax") == {}


def test_partial_multi_account_vat_payment_requires_explicit_source_selection(
    session, organization, tax_evidence
):
    _prepare_sources(
        session,
        organization,
        tax_evidence,
        key="partial-detail-sources",
        invoice_type="special",
    )
    _, period = _confirm_q1(session, organization, key="partial-detail-period")

    ambiguous = ComponentService(session).record(
        _tax_payment_request(
            organization,
            tax_evidence,
            key="ambiguous-partial-vat-payment",
            amount_fen=50,
        )
    )
    assert ambiguous.status == "needs_information", ambiguous
    assert ambiguous.missing_information == ["components.pay-tax.account_selections.vat_payable"]

    selected = ComponentService(session).record(
        _tax_payment_request(
            organization,
            tax_evidence,
            key="selected-partial-vat-payment",
            amount_fen=50,
            account_code="222111",
        )
    )
    assert selected.status == "posted", selected
    assert _component_line_amounts(session, selected.event_id, "tax_settlement") == {
        "222111": (50, 0)
    }
    assert period_liability_balances(session, organization.id, period, "vat") == {
        "222111": 50,
        "222112": 200,
    }


def test_deferred_vat_transfer_keeps_source_and_selected_payable_details(
    session, organization, tax_evidence
):
    service = ComponentService(session)
    _configure(
        service,
        organization,
        code="222109",
        name="待转销项税额明细",
        business_class="deferred_output_vat",
    )
    _configure(
        service,
        organization,
        code="222119",
        name="销项税额收款明细",
        business_class="vat_payable",
    )
    customer = {"kind": "customer", "name": "deferred-tax-customer"}
    sale = service.record(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "deferred-detail-sale",
                "posting_date": "2026-01-05",
                "evidence_references": [tax_evidence.id],
                "components": [
                    {
                        "key": "sale",
                        "kind": "service_sale",
                        "business_date": "2026-01-05",
                        "amount_fen": 10100,
                        "recognition_basis": "credit",
                        "fulfillment_date": "2026-01-05",
                        "tax_obligation_date": "2026-03-05",
                        "tax_facts": {
                            "taxable": True,
                            "rate_percent": "1",
                            "invoice_type": "special",
                            "waive_exemption": False,
                            "tax_due_on_event": False,
                        },
                        "account_selections": {"deferred_output_vat": "222109"},
                        "metadata": {"counterparty": customer},
                    }
                ],
            }
        )
    )
    assert sale.status == "posted", sale
    item = session.scalar(select(OpenItem).where(OpenItem.source_event_id == sale.event_id))
    assert item is not None

    receipt = service.record(
        RecordEventRequest.model_validate(
            {
                "org_id": organization.id,
                "idempotency_key": "deferred-detail-receipt",
                "posting_date": "2026-03-05",
                "evidence_references": [tax_evidence.id],
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
                        "key": "cash",
                        "account_code": "1001",
                        "direction": "receipt",
                        "payment_date": "2026-03-05",
                        "amount_fen": 10100,
                        "allocations": [{"component_key": "receipt", "amount_fen": 10100}],
                    }
                ],
            }
        )
    )
    assert receipt.status == "posted", receipt
    receipt_lines = _component_line_amounts(session, receipt.event_id, "receivable_settlement")
    assert receipt_lines["222109"] == (100, 0)
    assert receipt_lines["222119"] == (0, 100)
    transfer = session.scalar(
        select(DeferredOutputVatTransfer).where(
            DeferredOutputVatTransfer.source_open_item_id == item.id
        )
    )
    assert transfer is not None
    assert transfer.transfer_event_id == receipt.event_id

    source_component = session.scalar(
        select(BusinessEventComponent).where(
            BusinessEventComponent.event_id == sale.event_id,
            BusinessEventComponent.kind == "service_sale",
        )
    )
    assert vat_source_accounts(
        session,
        organization.id,
        [
            {
                "component_id": str(source_component.id),
                "vat_fen": 100,
                "exemption_eligible": False,
            }
        ],
    ) == {"222119": (100, 0)}
