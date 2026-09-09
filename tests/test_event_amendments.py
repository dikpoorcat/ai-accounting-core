from __future__ import annotations

import uuid
from datetime import date

import pytest
from sqlalchemy import func, select
from test_fixed_asset_service import _acquisition_request, _evidence
from test_intangible_asset_service import _request as intangible_request
from test_payroll_service import payroll_evidence, preview_and_confirm
from test_service import sale_request, voucher_totals

from ai_accounting.accounting_periods import canonical_sha256
from ai_accounting.event_amendment_schemas import AmendEventRequest
from ai_accounting.event_amendments import EventAmendmentService
from ai_accounting.fixed_asset_service import FixedAssetService
from ai_accounting.intangible_asset_service import IntangibleAssetService
from ai_accounting.models import (
    BusinessEvent,
    BusinessEventAmendment,
    FixedAsset,
    IntangibleAsset,
    OpenItem,
    PayrollBatch,
    PayrollLine,
    Voucher,
)
from ai_accounting.schemas import PreviewPayrollRequest
from ai_accounting.service import FinanceService


@pytest.fixture
def session(committable_session):
    return committable_session


@pytest.fixture(autouse=True)
def composition_evidence(session, organization):
    return _evidence(session, organization, "amendment")


def amendment(session, original, replacement, *, key="edit"):
    source = session.get(BusinessEvent, original.event_id)
    return AmendEventRequest(
        org_id=source.org_id,
        event_id=source.id,
        idempotency_key=key,
        expected_facts_hash=canonical_sha256(source.facts),
        reason="核对原始资料后修改",
        replacement=replacement,
    )


def test_sale_replaces_voucher_and_open_item_with_audit_and_replay(session, organization):
    service = FinanceService(session)
    source_request = sale_request(organization, event_type="service_credit_sale")
    source = service.record_event(source_request)
    replacement = source_request.model_copy(
        update={
            "components": [
                source_request.components[0].model_copy(update={"amount_fen": 2_020_000})
            ]
        }
    )
    old_facts = session.get(BusinessEvent, source.event_id).facts
    request = amendment(session, source, replacement)
    result = EventAmendmentService(session).amend(request)
    assert result["status"] == "posted", result
    assert result["event_id"] == str(source.event_id)
    assert result["voucher_number"] == source.voucher_number
    assert voucher_totals(session, source.voucher_id) == (2_020_000, 2_020_000)
    current_item = session.scalar(
        select(OpenItem).where(OpenItem.source_event_id == source.event_id)
    )
    assert current_item is not None and current_item.original_amount_fen == 2_020_000
    assert session.scalar(select(func.count()).select_from(Voucher)) == 1
    assert session.scalar(select(func.count()).select_from(BusinessEvent)) == 1
    history = session.scalar(select(BusinessEventAmendment))
    assert history.before_state["tables"]["business_events"][0]["facts"] == old_facts
    replay = EventAmendmentService(session).amend(request)
    assert replay["idempotent_replay"] is True
    mismatch = EventAmendmentService(session).amend(
        request.model_copy(update={"reason": "另一个原因"})
    )
    assert mismatch["errors"] == ["AMENDMENT_IDEMPOTENCY_PAYLOAD_MISMATCH"]
    stale = EventAmendmentService(session).amend(
        request.model_copy(update={"idempotency_key": "stale"})
    )
    assert stale["errors"] == ["AMENDMENT_FACTS_STALE"]


@pytest.mark.parametrize("failure", ["missing", "closed", "other_month", "derivation"])
def test_failed_amendment_leaves_original_intact(session, organization, failure, monkeypatch):
    request = sale_request(organization, event_type="service_credit_sale")
    source = FinanceService(session).record_event(request)
    if failure == "missing":
        replacement = request.model_copy(
            update={"components": [request.components[0].model_copy(update={"tax_facts": None})]}
        )
    elif failure == "other_month":
        replacement = request.model_copy(update={"posting_date": date(2026, 7, 8)})
    elif failure == "derivation":
        replacement = request.model_copy(
            update={
                "components": [
                    request.components[0].model_copy(
                        update={
                            "metadata": request.components[0].metadata.model_copy(
                                update={
                                    "counterparty": request.components[
                                        0
                                    ].metadata.counterparty.model_copy(update={"id": uuid.uuid4()})
                                }
                            )
                        }
                    )
                ]
            }
        )
    else:
        monkeypatch.setattr(
            "ai_accounting.ledger.posting_period_error_code",
            lambda *a, **k: "ACCOUNTING_PERIOD_CLOSED",
        )
        replacement = request
    result = EventAmendmentService(session).amend(amendment(session, source, replacement))
    assert result["status"] in {"rejected", "needs_information"}, result
    assert session.get(BusinessEvent, source.event_id).status == "posted"
    assert voucher_totals(session, source.voucher_id) == (1_010_000, 1_010_000)
    assert session.scalar(select(func.count()).select_from(BusinessEventAmendment)) == 0


@pytest.mark.parametrize("kind", ["fixed", "intangible"])
def test_asset_acquisition_amendment_replaces_projection_and_recalculates_cost(
    session, organization, kind
):
    evidence = _evidence(session, organization, "a")
    if kind == "fixed":
        request = _acquisition_request(organization, evidence)
        source = FixedAssetService(session).acquire_fixed_asset(request)
        model = FixedAsset
    else:
        request = intangible_request(organization, evidence)
        source = IntangibleAssetService(session).acquire_intangible_asset(request)
        model = IntangibleAsset
    assert source.status == "posted", source
    replacement = request.model_copy(
        update={
            "cost_fen": 100_000 if kind == "fixed" else 51_000,
            "cost_components": request.cost_components.model_copy(
                update={"purchase_price_fen": 50_000}
            ),
        }
    )
    result = EventAmendmentService(session).amend(amendment(session, source, replacement))
    assert result["status"] == "posted", result
    current_asset = session.scalar(
        select(model).where(model.acquisition_event_id == source.event_id)
    )
    assert current_asset is not None
    assert current_asset.cost_fen == (100_000 if kind == "fixed" else 51_000)
    assert result["voucher_id"] == str(source.voucher_id)
    assert session.scalar(select(func.count()).select_from(Voucher)) == 1


def test_payroll_amendment_recalculates_same_batch_and_liabilities(session, organization):
    _, source = preview_and_confirm(session, organization)
    batch = session.get(PayrollBatch, source.batch_id)
    request = PreviewPayrollRequest.model_validate(batch.calculation_input["request"])
    request.employee_items[0].tax_reported_salary_fen += 10_000
    old_total = voucher_totals(session, source.voucher_id)[0]
    result = EventAmendmentService(session).amend(amendment(session, source, request))
    assert result["status"] == "posted", result
    assert result["batch_id"] == str(source.batch_id)
    assert voucher_totals(session, source.voucher_id)[0] == old_total + 10_000
    assert session.scalar(select(PayrollLine.tax_reported_salary_fen)) == 1_010_000
    assert session.scalar(select(func.count()).select_from(Voucher)) == 1


def test_borrowing_contract_and_interest_amendments(session, organization):
    from test_borrowing_service import _bank_row, _confirm_bank_scope, _draw_request

    from ai_accounting.borrowing_schemas import (
        ConfirmBorrowingInterestRequest,
        PreviewBorrowingInterestRequest,
    )
    from ai_accounting.borrowing_service import BorrowingService
    from ai_accounting.models import Borrowing

    _confirm_bank_scope(session, organization)
    evidence = _evidence(session, organization, "b")
    bank = _bank_row(
        session, organization, amount_fen=1_000_000, booking_date=date(2026, 1, 1), seed="d"
    )
    request = _draw_request(organization, evidence, bank)
    service = BorrowingService(session)
    source = service.draw_borrowing(request)
    assert source.status == "posted", source
    replacement = request.model_copy(update={"contract_name": "Corrected contract"})
    result = EventAmendmentService(session).amend(amendment(session, source, replacement))
    assert result["status"] == "posted", result
    current_borrowing = session.scalar(
        select(Borrowing).where(Borrowing.drawdown_event_id == source.event_id)
    )
    assert current_borrowing is not None
    assert current_borrowing.contract_name == "Corrected contract"
    preview_request = PreviewBorrowingInterestRequest(
        org_id=organization.id,
        borrowing_id=current_borrowing.id,
        period_start=date(2026, 1, 1),
        period_end=date(2026, 7, 1),
    )
    preview = service.preview_borrowing_interest(preview_request)
    interest = service.confirm_borrowing_interest(
        ConfirmBorrowingInterestRequest.model_validate(
            preview_request.model_dump()
            | {"calculation_hash": preview.calculation_hash, "idempotency_key": "interest"}
        )
    )
    assert interest.status == "posted", interest
    result = EventAmendmentService(session).amend(
        amendment(session, interest, preview_request, key="interest-edit")
    )
    assert result["status"] == "posted", result
    blocked = EventAmendmentService(session).amend(
        amendment(session, source, replacement, key="blocked")
    )
    assert blocked["errors"] == ["AMENDMENT_DEPENDENT_FACTS_EXIST"]


def test_labor_batch_recalculates_tax_and_preserves_batch(session, organization):
    from test_labor_remuneration_service import _register_person

    from ai_accounting.labor_remuneration_schemas import (
        ConfirmLaborRemunerationBatchRequest,
        PreviewLaborRemunerationBatchRequest,
    )
    from ai_accounting.labor_remuneration_service import LaborRemunerationService

    evidence = _evidence(session, organization, "e")
    person_id = _register_person(session, organization, evidence, "L1", "Labor person")
    request = PreviewLaborRemunerationBatchRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "labor-preview",
            "remuneration_period": "2026-08",
            "business_date": "2026-08-31",
            "posting_date": "2026-08-31",
            "planned_payment_date": "2026-09-05",
            "items": [
                {
                    "labor_person_id": person_id,
                    "service_start_date": "2026-08-01",
                    "service_end_date": "2026-08-31",
                    "fixed_fee_fen": 100_000,
                    "commission_fen": 0,
                    "expense_role": "labor_management_expense",
                    "tax_identity": "resident",
                    "income_grouping": "continuous_monthly",
                    "is_full_time_student": False,
                    "external_declaration_status": "not_due",
                }
            ],
            "evidence_references": [evidence.id],
        }
    )
    service = LaborRemunerationService(session)
    preview = service.preview_batch(request)
    source = service.confirm_batch(
        ConfirmLaborRemunerationBatchRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="labor-confirm",
            confirmation_note="Confirm",
        )
    )
    assert source.status == "posted", source
    request.items[0].fixed_fee_fen = 500_000
    request.items[0].gross_remuneration_fen = 500_000
    result = EventAmendmentService(session).amend(amendment(session, source, request))
    assert result["status"] == "posted", result
    assert result["batch_id"] == str(source.batch_id)
    assert voucher_totals(session, source.voucher_id) == (500_000, 500_000)


def test_tax_snapshot_amendment_and_locked_source(session, organization):
    from ai_accounting.schemas import TaxPeriodConfirmRequest, TaxPeriodPreviewRequest

    sale = sale_request(organization, event_type="service_credit_sale")
    sale_component = sale.components[0].model_copy(
        update={
            "business_date": date(2026, 3, 8),
            "fulfillment_date": date(2026, 3, 8),
            "tax_obligation_date": date(2026, 3, 8),
        }
    )
    sale = sale.model_copy(
        update={
            "posting_date": date(2026, 3, 8),
            "components": [sale_component],
        }
    )
    assert FinanceService(session).record_event(sale).status == "posted"
    service = FinanceService(session)
    request = TaxPeriodPreviewRequest(
        org_id=organization.id,
        start_date=date(2026, 1, 1),
        end_date=date(2026, 3, 31),
        adjustment_posting_date=date(2026, 3, 31),
    )
    preview = service.preview_tax_period(request)
    source = service.confirm_tax_period(
        TaxPeriodConfirmRequest.model_validate(
            request.model_dump()
            | {"calculation_hash": preview["calculation_hash"], "idempotency_key": "vat-confirm"}
        )
    )
    assert source.status == "posted", source
    result = EventAmendmentService(session).amend(amendment(session, source, request))
    assert result["status"] == "posted", result
    assert result["voucher_id"] == str(source.voucher_id)


def test_enterprise_income_tax_confirmation_amendment(session, organization):
    from ai_accounting.financial_statement_schemas import ConfirmEnterpriseIncomeTaxQuarterRequest
    from ai_accounting.financial_statements import FinancialStatementService

    evidence = _evidence(session, organization, "c")
    request = ConfirmEnterpriseIncomeTaxQuarterRequest.model_validate(
        {
            "org_id": organization.id,
            "year": 2026,
            "quarter": 2,
            "treatment": "accrue",
            "amount_fen": 10_000,
            "posting_date": "2026-06-30",
            "idempotency_key": "cit-confirm",
            "confirmation_note": "Confirm tax",
            "evidence_references": [evidence.id],
        }
    )
    source = FinancialStatementService(session).confirm_enterprise_income_tax(request)
    assert source.status == "posted", source
    result = EventAmendmentService(session).amend(
        amendment(session, source, request.model_copy(update={"amount_fen": 20_000}))
    )
    assert result["status"] == "posted", result
    assert voucher_totals(session, source.voucher_id) == (20_000, 20_000)


def test_income_tax_result_amendment_recalculates_delta_in_same_voucher(session, organization):
    from types import SimpleNamespace

    from test_enterprise_income_tax import change, confirm, root

    from ai_accounting.enterprise_income_tax import EnterpriseIncomeTaxService

    evidence = _evidence(session, organization, "c")
    root_id = root(session, organization, evidence)
    request = change(organization, evidence, root_id)
    source, _ = confirm(EnterpriseIncomeTaxService(session), request)
    source = SimpleNamespace(event_id=uuid.UUID(source["event_id"]))
    voucher_count = session.scalar(select(func.count()).select_from(Voucher))
    request = request.model_copy(update={"declared_tax_fen": 20_000})
    result = EventAmendmentService(session).amend(amendment(session, source, request))
    assert result["status"] == "posted", result
    assert session.scalar(select(func.count()).select_from(Voucher)) == voucher_count
    # The original confirmation already accrued 10,000.  The amended result
    # changes the declared total to 20,000 and owns only that 10,000 difference.
    assert voucher_totals(session, uuid.UUID(result["voucher_id"])) == (10_000, 10_000)


def test_discovery_and_query_expose_typed_amendments_and_history(
    session, organization, monkeypatch
):
    from contextlib import nullcontext

    from pydantic import ValidationError

    from ai_accounting import mcp_server

    request = sale_request(organization, event_type="service_credit_sale")
    source = FinanceService(session).record_event(request)
    request = request.model_copy(
        update={
            "components": [
                request.components[0].model_copy(
                    update={"amount_fen": request.components[0].amount_fen + 100}
                )
            ]
        }
    )
    result = EventAmendmentService(session).amend(amendment(session, source, request))
    assert result["status"] == "posted", result
    monkeypatch.setattr(mcp_server, "SessionLocal", lambda: nullcontext(session))
    query = mcp_server.finance_get_event(str(organization.id), str(source.event_id))
    assert query["facts_hash"] == result["facts_hash"]
    assert len(query["amendments"]) == 1
    assert query["amendments"][0]["before_state"] != query["amendments"][0]["after_state"]
    schema = mcp_server.finance_get_event_schema()
    assert "amend_event_schema" in schema
    assert (
        schema["agent_operating_protocol"]["open_month_amendments"]["tool"] == "finance_amend_event"
    )
    invalid = amendment(session, source, request).model_dump(mode="json")
    invalid["replacement"] = {
        "org_id": str(organization.id),
        "entries": [{"debit_fen": 100, "account_code": "1002"}],
    }
    with pytest.raises(ValidationError):
        AmendEventRequest.model_validate(invalid)


def test_amendment_changes_close_source_hash_even_without_amount_change(session, organization):
    from types import SimpleNamespace

    from ai_accounting.accounting_period_service import AccountingPeriodService

    request = sale_request(organization, event_type="service_credit_sale")
    source = FinanceService(session).record_event(request)
    period = SimpleNamespace(start_date=date(2026, 8, 1), end_date=date(2026, 8, 31))
    service = AccountingPeriodService(session)
    before, _ = service._voucher_sources(organization.id, period, lock=False)
    result = EventAmendmentService(session).amend(amendment(session, source, request))
    assert result["status"] == "posted", result
    after, _ = service._voucher_sources(organization.id, period, lock=False)
    assert before[0]["request_payload_hash_at_close"] != after[0]["request_payload_hash_at_close"]


@pytest.mark.parametrize("method", ["combined", "separate"])
def test_annual_bonus_amendment_preserves_tax_state(session, organization, method):
    from ai_accounting.models import PayrollTaxStateSlot
    from ai_accounting.schemas import ConfirmPayrollRequest

    service, regular = preview_and_confirm(session, organization)
    employee_id = session.scalar(select(PayrollLine.employee_id))
    evidence = payroll_evidence(session, organization, f"bonus-amendment-{method}")
    request = PreviewPayrollRequest.model_validate(
        {
            "org_id": organization.id,
            "idempotency_key": "bonus-preview",
            "batch_kind": "annual_bonus",
            "payroll_period": "2026-03",
            "posting_date": "2026-03-05",
            "payment_date": "2026-03-05",
            "tax_method": method,
            "evidence_references": [evidence.id],
            "employee_items": [
                {
                    "employee_id": employee_id,
                    "annual_bonus_fen": 100_000,
                    **(
                        {"regular_payroll_batch_id": regular.batch_id}
                        if method == "combined"
                        else {}
                    ),
                }
            ],
        }
    )
    preview = service.preview_payroll(request)
    assert preview.status == "calculated", preview
    source = service.confirm_payroll(
        ConfirmPayrollRequest(
            org_id=organization.id,
            batch_id=preview.batch_id,
            calculation_hash=preview.calculation_hash,
            idempotency_key="bonus-confirm",
        )
    )
    assert source.status == "posted", source
    request.employee_items[0].annual_bonus_fen = 200_000
    result = EventAmendmentService(session).amend(amendment(session, source, request))
    assert result["status"] == "posted", result
    assert result["batch_id"] == str(source.batch_id)
    assert voucher_totals(session, source.voucher_id) == (200_000, 200_000)
    slot = session.scalar(select(PayrollTaxStateSlot))
    assert slot.final_batch_id == (source.batch_id if method == "combined" else regular.batch_id)


@pytest.mark.parametrize(
    "kind", ["activation", "depreciation", "batch", "disposal", "amortization", "retirement"]
)
def test_asset_lifecycle_amendment(session, organization, kind):
    from ai_accounting import intangible_asset_schemas as ins
    from ai_accounting import schemas as s

    evidence = _evidence(session, organization, "f")
    if kind in {"amortization", "retirement"}:
        service = IntangibleAssetService(session)
        acquired = service.acquire_intangible_asset(intangible_request(organization, evidence))
        request = ins.PreviewIntangibleAssetAmortizationRequest(
            org_id=organization.id,
            asset_id=acquired.asset_id,
            amortization_period="2026-01",
            posting_date=date(2026, 1, 31),
        )
        preview = service.preview_intangible_asset_amortization(request)
        source = service.confirm_intangible_asset_amortization(
            ins.ConfirmIntangibleAssetAmortizationRequest(
                **request.model_dump(),
                calculation_hash=preview.calculation_hash,
                idempotency_key="amortize",
            )
        )
        if kind == "retirement":
            request = ins.RetireIntangibleAssetRequest(
                org_id=organization.id,
                asset_id=acquired.asset_id,
                idempotency_key="retire",
                retirement_date=date(2026, 1, 31),
                posting_date=date(2026, 1, 31),
                gross_proceeds_fen=0,
                compensation_fen=0,
                taxes_and_fees_fen=0,
                residual_proceeds_fen=0,
                evidence_references=[evidence.id],
            )
            source = service.retire_intangible_asset(request)
    else:
        service = FixedAssetService(session)
        acquired = service.acquire_fixed_asset(_acquisition_request(organization, evidence))
        request = s.ActivateFixedAssetRequest(
            org_id=organization.id,
            asset_id=acquired.asset_id,
            idempotency_key="activate",
            activation_date=date(2026, 1, 10),
            posting_date=date(2026, 1, 10),
            useful_life_months=13,
            residual_value_fen=10_000,
            benefit_area="management",
            evidence_references=[evidence.id],
        )
        source = service.activate_fixed_asset(request)
        if kind == "activation":
            request = request.model_copy(update={"useful_life_months": 24})
        else:
            batch = kind == "batch"
            preview_schema = (
                s.PreviewFixedAssetDepreciationBatchRequest
                if batch
                else s.PreviewFixedAssetDepreciationRequest
            )
            confirm_schema = (
                s.ConfirmFixedAssetDepreciationBatchRequest
                if batch
                else s.ConfirmFixedAssetDepreciationRequest
            )
            preview_method = (
                service.preview_fixed_asset_depreciation_batch
                if batch
                else service.preview_fixed_asset_depreciation
            )
            confirm_method = (
                service.confirm_fixed_asset_depreciation_batch
                if batch
                else service.confirm_fixed_asset_depreciation
            )
            request = preview_schema(
                org_id=organization.id,
                depreciation_period="2026-02",
                posting_date=date(2026, 2, 28),
                **({} if batch else {"asset_id": acquired.asset_id}),
            )
            preview = preview_method(request)
            source = confirm_method(
                confirm_schema(
                    **request.model_dump(),
                    calculation_hash=preview.calculation_hash,
                    idempotency_key="depreciate",
                )
            )
            if kind == "disposal":
                request = s.DisposeFixedAssetRequest.model_validate(
                    {
                        "org_id": organization.id,
                        "asset_id": acquired.asset_id,
                        "idempotency_key": "dispose",
                        "disposal_date": "2026-02-28",
                        "posting_date": "2026-02-28",
                        "disposal_kind": "sale",
                        "gross_proceeds_fen": 500_000,
                        "invoice_type": "ordinary",
                        "waive_exemption": False,
                        "settlement_method": "receivable",
                        "customer": {"kind": "customer", "name": "Asset buyer"},
                        "tax_obligation_date": "2026-02-28",
                        "clearance_cost_fen": 0,
                        "evidence_references": [evidence.id],
                    }
                )
                source = service.dispose_fixed_asset(request)
                request = request.model_copy(update={"gross_proceeds_fen": 600_000})
    assert source.status == "posted", source
    voucher_count = session.scalar(select(func.count()).select_from(Voucher))
    result = EventAmendmentService(session).amend(amendment(session, source, request))
    assert result["status"] == "posted", result
    assert result["voucher_id"] == str(source.voucher_id)
    assert session.scalar(select(func.count()).select_from(Voucher)) == voucher_count
    debit, credit = voucher_totals(session, source.voucher_id)
    assert debit == credit and debit > 0
    if kind == "disposal":
        assert (
            session.scalar(
                select(OpenItem.original_amount_fen).where(
                    OpenItem.source_event_id == source.event_id
                )
            )
            == 600_000
        )
