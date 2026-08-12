"""Deterministic purchased-intangible-asset workflow service."""

from __future__ import annotations

import uuid
from calendar import monthrange
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .intangible_asset_schemas import (
    AcquireIntangibleAssetRequest,
    ConfirmIntangibleAssetAmortizationRequest,
    IntangibleAssetInformationRequirement,
    IntangibleAssetResult,
    IntangibleAssetResultStatus,
    PreviewIntangibleAssetAmortizationRequest,
    RetireIntangibleAssetRequest,
)
from .intangible_assets import (
    SMALL_ENTERPRISE_INTANGIBLE_ASSET_RULE_VERSION,
    IntangibleAssetCalculationError,
    calculate_acquisition_cost,
    calculate_straight_line_amortization,
    intangible_asset_calculation_hash,
)
from .ledger import AccountingPeriodError, Entry, create_voucher
from .models import (
    AuditLog,
    BankTransactionMatch,
    BusinessEvent,
    Counterparty,
    Evidence,
    IntangibleAsset,
    IntangibleAssetAmortization,
    IntangibleAssetRetirement,
    OpenItem,
    Organization,
    Voucher,
    event_evidence,
)
from .schemas import FinanceResult, ResultStatus, ReverseEventRequest
from .service import FinanceService

ACCOUNTING_RULE_SOURCE_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
INTANGIBLE_ASSET_EVENT_TYPES = {
    "intangible_asset_acquisition",
    "intangible_asset_amortization",
    "intangible_asset_retirement",
}


class _IntangibleAssetDecision(ValueError):
    def __init__(
        self,
        status: IntangibleAssetResultStatus,
        code: str,
        *,
        missing: list[IntangibleAssetInformationRequirement] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.missing = missing or []
        super().__init__(code)


class IntangibleAssetService(FinanceService):
    """Post only the finite templates frozen for the Phase-1 workflow."""

    def __init__(self, session: Session):
        super().__init__(session)

    def acquire_intangible_asset(
        self, request: AcquireIntangibleAssetRequest
    ) -> IntangibleAssetResult:
        return self._run_intangible_write(
            "finance_acquire_intangible_asset",
            request,
            lambda: self._acquire_intangible_asset_write(request),
        )

    def preview_intangible_asset_amortization(
        self, request: PreviewIntangibleAssetAmortizationRequest
    ) -> IntangibleAssetResult:
        if self.session.get(Organization, request.org_id) is None:
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        if missing := request.missing_information():
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.NEEDS_INFORMATION,
                asset_id=request.asset_id,
                missing_information=missing,
                trace=[{"stage": "validation", "status": "needs_information"}],
            )
        try:
            snapshot = self._amortization_snapshot(request, lock=False)
        except _IntangibleAssetDecision as exc:
            return IntangibleAssetResult(
                status=exc.status,
                asset_id=request.asset_id,
                errors=[exc.code],
            )
        except IntangibleAssetCalculationError as exc:
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                asset_id=request.asset_id,
                errors=[exc.code],
            )
        return IntangibleAssetResult(
            status=IntangibleAssetResultStatus.CALCULATED,
            asset_id=snapshot["asset"].id,
            calculation_hash=snapshot["calculation_hash"],
            trace=snapshot["trace"],
            data=snapshot["data"],
        )

    def confirm_intangible_asset_amortization(
        self, request: ConfirmIntangibleAssetAmortizationRequest
    ) -> IntangibleAssetResult:
        return self._run_intangible_write(
            "finance_confirm_intangible_asset_amortization",
            request,
            lambda: self._confirm_intangible_asset_amortization_write(request),
        )

    def retire_intangible_asset(
        self, request: RetireIntangibleAssetRequest
    ) -> IntangibleAssetResult:
        return self._run_intangible_write(
            "finance_retire_intangible_asset",
            request,
            lambda: self._retire_intangible_asset_write(request),
        )

    def get_intangible_asset(self, org_id: uuid.UUID, asset_id: uuid.UUID) -> IntangibleAssetResult:
        if self.session.get(Organization, org_id) is None:
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        asset = self._get_asset(org_id, asset_id)
        if asset is None:
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                errors=["INTANGIBLE_ASSET_NOT_FOUND"],
            )
        acquisition = self.session.get(BusinessEvent, asset.acquisition_event_id)
        active_amortizations = self._active_amortizations(asset.id)
        amortizations = self._amortization_history(asset.id)
        retirement = self._active_retirement(asset.id)
        retirement_history = self._retirement_history(asset.id)
        acquisition_reversed = acquisition is not None and acquisition.status == "reversed"
        accumulated = (
            0 if acquisition_reversed else sum(item.amount_fen for item in active_amortizations)
        )
        book_value = 0 if acquisition_reversed else asset.cost_fen - accumulated
        on_book = not acquisition_reversed and retirement is None
        return IntangibleAssetResult(
            status=(
                IntangibleAssetResultStatus.REVERSED
                if acquisition_reversed
                else IntangibleAssetResultStatus.POSTED
            ),
            asset_id=asset.id,
            data={
                "asset_code": asset.asset_code,
                "asset_name": asset.name,
                "category": asset.category,
                "rights_description": asset.rights_description,
                "cost_fen": asset.cost_fen,
                "residual_value_fen": 0,
                "useful_life_months": asset.useful_life_months,
                "life_basis": asset.life_basis,
                "benefit_area": asset.benefit_area,
                "available_for_use_date": asset.available_for_use_date.isoformat(),
                "accumulated_amortization_fen": accumulated,
                "book_value_fen": book_value,
                "on_book": on_book,
                "retired": retirement is not None,
                "accounting_rule_version": asset.accounting_rule_version,
                "accounting_rule_source_url": asset.accounting_rule_source_url,
                "acquisition_event": self._event_projection(acquisition),
                "amortizations": [
                    {
                        "id": str(item.id),
                        "event_id": str(item.event_id),
                        "period_start": item.period_start.isoformat(),
                        "posting_date": item.posting_date.isoformat(),
                        "sequence_no": item.sequence_no,
                        "amount_fen": item.amount_fen,
                        "accumulated_after_fen": item.accumulated_after_fen,
                        "calculation_hash": item.calculation_hash,
                        "active": (
                            self.session.get(BusinessEvent, item.event_id).status == "posted"
                        ),
                        "event": self._event_projection(
                            self.session.get(BusinessEvent, item.event_id)
                        ),
                    }
                    for item in amortizations
                ],
                "retirement": (
                    {
                        "id": str(retirement.id),
                        "event_id": str(retirement.event_id),
                        "retirement_date": retirement.retirement_date.isoformat(),
                        "posting_date": retirement.posting_date.isoformat(),
                        "accumulated_amortization_fen": (retirement.accumulated_amortization_fen),
                        "book_value_fen": retirement.book_value_fen,
                        "event": self._event_projection(
                            self.session.get(BusinessEvent, retirement.event_id)
                        ),
                    }
                    if retirement is not None
                    else None
                ),
                "retirement_history": [
                    {
                        "id": str(item.id),
                        "event_id": str(item.event_id),
                        "retirement_date": item.retirement_date.isoformat(),
                        "posting_date": item.posting_date.isoformat(),
                        "accumulated_amortization_fen": (item.accumulated_amortization_fen),
                        "book_value_fen": item.book_value_fen,
                        "active": (
                            self.session.get(BusinessEvent, item.event_id).status == "posted"
                        ),
                        "event": self._event_projection(
                            self.session.get(BusinessEvent, item.event_id)
                        ),
                    }
                    for item in retirement_history
                ],
            },
        )

    def reverse_event(self, request: ReverseEventRequest) -> FinanceResult:
        original = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.id == request.event_id,
            )
        )
        if original is None or original.event_type not in INTANGIBLE_ASSET_EVENT_TYPES:
            return super().reverse_event(request)
        request_payload_hash = self._request_payload_hash(request)
        existing = self._idempotent_event(request.org_id, request.idempotency_key)
        if existing is not None:
            if existing.request_payload_hash != request_payload_hash:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["INTANGIBLE_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                )
            return self._result_for_existing(existing)
        return super().reverse_event(request)

    def _reverse_event_write(self, request: ReverseEventRequest) -> FinanceResult:
        original = self.session.scalar(
            select(BusinessEvent)
            .where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.id == request.event_id,
            )
            .with_for_update()
        )
        if original is None or original.event_type not in INTANGIBLE_ASSET_EVENT_TYPES:
            return super()._reverse_event_write(request)
        asset = self._asset_for_event(original)
        if asset is None:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["INTANGIBLE_ASSET_NORMALIZED_FACT_NOT_FOUND"],
            )
        asset = self._get_asset(request.org_id, asset.id, lock=True)
        if asset is None:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["INTANGIBLE_ASSET_NOT_FOUND"],
            )
        if self.intangible_asset_reversal_dependency_error(original, asset):
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST"],
            )
        return super()._reverse_event_write(request)

    def _run_intangible_write(
        self,
        command: str,
        request: Any,
        writer: Callable[[], IntangibleAssetResult],
    ) -> IntangibleAssetResult:
        if self.session.get(Organization, request.org_id) is None:
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        payload_hash = self._intangible_request_hash(command, request)
        existing = self._idempotent_event(request.org_id, request.idempotency_key)
        if existing is not None:
            return self._existing_result(existing, payload_hash)
        if (
            isinstance(request, AcquireIntangibleAssetRequest)
            and request.settlement_method is not None
            and request.settlement_method.value == "bank"
            and not self._bank_reconciliation_scope_is_confirmed(
                self.session.get(Organization, request.org_id)
            )
        ):
            requirement = IntangibleAssetInformationRequirement(
                code="BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED",
                message="owner-confirmed bank reconciliation scope is required",
                fields=["bank_reconciliation_scope_confirmation"],
            )
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.NEEDS_INFORMATION,
                missing_information=[requirement],
                trace=[
                    {
                        "stage": "validation",
                        "status": "needs_information",
                        "code": requirement.code,
                    }
                ],
            )
        if missing := request.missing_information():
            return self._persist_nonposted_decision(
                command,
                request,
                status=IntangibleAssetResultStatus.NEEDS_INFORMATION,
                missing=missing,
            )
        try:
            with self.session.begin_nested():
                return writer()
        except _IntangibleAssetDecision as exc:
            return self._persist_nonposted_decision(
                command,
                request,
                status=exc.status,
                errors=[exc.code] if not exc.missing else [],
                missing=exc.missing,
            )
        except IntangibleAssetCalculationError as exc:
            return self._persist_nonposted_decision(
                command,
                request,
                status=IntangibleAssetResultStatus.REJECTED,
                errors=[exc.code],
            )
        except AccountingPeriodError as exc:
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                errors=[exc.code],
            )
        except IntegrityError:
            existing = self._idempotent_event(request.org_id, request.idempotency_key)
            if existing is not None:
                return self._existing_result(existing, payload_hash)
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                errors=["INTANGIBLE_ASSET_CONCURRENT_WRITE_CONFLICT"],
            )
        except OperationalError:
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                errors=["INTANGIBLE_ASSET_CONCURRENT_WRITE_CONFLICT"],
            )

    def _persist_nonposted_decision(
        self,
        command: str,
        request: Any,
        *,
        status: IntangibleAssetResultStatus,
        missing: list[IntangibleAssetInformationRequirement] | None = None,
        errors: list[str] | None = None,
    ) -> IntangibleAssetResult:
        payload_hash = self._intangible_request_hash(command, request)
        try:
            with self.session.begin_nested():
                existing = self._idempotent_event(request.org_id, request.idempotency_key)
                if existing is not None:
                    return self._existing_result(existing, payload_hash)
                return self._store_nonposted(
                    command,
                    request,
                    status=status,
                    missing=missing,
                    errors=errors,
                )
        except IntegrityError:
            existing = self._idempotent_event(request.org_id, request.idempotency_key)
            if existing is not None:
                return self._existing_result(existing, payload_hash)
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                errors=["INTANGIBLE_ASSET_CONCURRENT_WRITE_CONFLICT"],
            )
        except OperationalError:
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                errors=["INTANGIBLE_ASSET_CONCURRENT_WRITE_CONFLICT"],
            )

    def _acquire_intangible_asset_write(
        self, request: AcquireIntangibleAssetRequest
    ) -> IntangibleAssetResult:
        if request.is_available_for_use is not True:
            self._reject("INTANGIBLE_ASSET_NOT_READY_WORKFLOW_NOT_ENABLED")
        if request.claims_creditable_input_vat is not False:
            self._reject("INTANGIBLE_ASSET_CREDITABLE_INPUT_VAT_NOT_ENABLED")
        if request.category.value == "other_identifiable_non_land" and (
            not request.other_right_type_description or not request.identifiability_basis
        ):
            self._reject("INTANGIBLE_ASSET_OTHER_RIGHT_FACTS_REQUIRED")
        if self._month_start(request.acquisition_date) != self._month_start(
            request.available_for_use_date
        ) or self._month_start(request.acquisition_date) != self._month_start(request.posting_date):
            self._reject("INTANGIBLE_ASSET_ACQUISITION_DATES_INVALID")
        if request.available_for_use_date < request.acquisition_date:
            self._reject("INTANGIBLE_ASSET_ACQUISITION_DATES_INVALID")
        if self.session.scalar(
            select(IntangibleAsset.id).where(
                IntangibleAsset.org_id == request.org_id,
                IntangibleAsset.asset_code == request.asset_code,
            )
        ):
            self._reject("INTANGIBLE_ASSET_CODE_ALREADY_EXISTS")
        components = request.cost_components
        calculation = calculate_acquisition_cost(
            purchase_price_fen=components.purchase_price_fen,
            noncreditable_tax_fen=components.noncreditable_tax_fen,
            directly_attributable_cost_fen=components.directly_attributable_cost_fen,
        )
        if calculation.cost_fen < request.useful_life_months:
            self._reject("INTANGIBLE_ASSET_INVALID_AMORTIZATION_POLICY")
        if (
            request.life_basis.value == "not_reliably_estimated"
            and request.useful_life_months < 120
        ):
            self._reject("INTANGIBLE_ASSET_INVALID_AMORTIZATION_POLICY")
        self._add_months(
            self._month_start(request.available_for_use_date),
            request.useful_life_months - 1,
        )
        supplier = self._resolve_supplier(request.org_id, request.supplier)
        settlement = request.settlement_method.value
        if settlement == "bank":
            if request.due_date is not None:
                self._reject("INTANGIBLE_ASSET_BANK_SETTLEMENT_FORBIDS_DUE_DATE")
            self._validate_bank_account(
                request.org_id, request.bank_account_code, request.payment_date
            )
        else:
            if (
                request.payment_date is not None
                or request.bank_account_code is not None
                or request.bank_transaction_references
            ):
                self._reject("INTANGIBLE_ASSET_PAYABLE_FORBIDS_BANK_FACTS")
        self._validate_evidence(request.org_id, request.evidence_references)
        trace = [
            {
                "stage": "facts_validated",
                "command": "finance_acquire_intangible_asset",
                "evidence_ids": sorted(map(str, request.evidence_references)),
                "cost_components": asdict(calculation),
                "residual_value_fen": 0,
                "life_basis": request.life_basis.value,
                "useful_life_months": request.useful_life_months,
                "bank_account_code": request.bank_account_code,
            },
            self._rule_trace(),
        ]
        event = self._new_event(
            request,
            command="finance_acquire_intangible_asset",
            event_type="intangible_asset_acquisition",
            business_date=request.acquisition_date,
            posting_date=request.posting_date,
            payment_date=request.payment_date if settlement == "bank" else None,
            trace=trace,
        )
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        if settlement == "bank" and request.bank_transaction_references:
            self._match_bank_transactions(
                event,
                request.bank_transaction_references,
                bank_account_code=request.bank_account_code,
                expected_outflow_fen=calculation.cost_fen,
                expected_date=request.payment_date,
            )
        asset = IntangibleAsset(
            org_id=request.org_id,
            asset_code=request.asset_code,
            name=request.asset_name,
            category=request.category.value,
            rights_description=request.rights_description,
            other_right_type_description=request.other_right_type_description,
            identifiability_basis=request.identifiability_basis,
            supplier_id=supplier.id,
            acquisition_date=request.acquisition_date,
            available_for_use_date=request.available_for_use_date,
            posting_date=request.posting_date,
            purchase_price_fen=calculation.purchase_price_fen,
            noncreditable_tax_fen=calculation.noncreditable_tax_fen,
            directly_attributable_cost_fen=calculation.directly_attributable_cost_fen,
            cost_fen=calculation.cost_fen,
            settlement_method=settlement,
            payment_date=request.payment_date if settlement == "bank" else None,
            due_date=request.due_date if settlement == "payable" else None,
            benefit_area=request.benefit_area.value,
            life_basis=request.life_basis.value,
            useful_life_months=request.useful_life_months,
            life_basis_explanation=request.life_basis_explanation,
            is_available_for_use=True,
            claims_creditable_input_vat=False,
            acquisition_event_id=event.id,
            accounting_rule_version=SMALL_ENTERPRISE_INTANGIBLE_ASSET_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(asset)
        self.session.flush()
        entries = [
            Entry(account_role="intangible_asset_cost", debit_fen=calculation.cost_fen),
            Entry(
                account_code=request.bank_account_code if settlement == "bank" else None,
                account_role=None if settlement == "bank" else "accounts_payable",
                credit_fen=calculation.cost_fen,
                counterparty_id=supplier.id if settlement == "payable" else None,
            ),
        ]
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.posting_date,
            description=request.description or f"取得无形资产 {request.asset_code}",
            entries=entries,
        )
        if settlement == "payable":
            self.session.add(
                OpenItem(
                    org_id=request.org_id,
                    counterparty_id=supplier.id,
                    source_event_id=event.id,
                    item_type="payable",
                    original_amount_fen=calculation.cost_fen,
                    due_date=request.due_date,
                )
            )
        trace.extend(
            [
                self._entries_trace(entries),
                {"stage": "normalized_fact_created", "asset_id": str(asset.id)},
            ]
        )
        event.rule_trace = [dict(item) for item in trace]
        event.facts = {**event.facts, "asset_id": str(asset.id)}
        data = {
            "cost_fen": calculation.cost_fen,
            "residual_value_fen": 0,
            "useful_life_months": asset.useful_life_months,
            "next_amortization_period": self._month_start(asset.available_for_use_date).strftime(
                "%Y-%m"
            ),
        }
        self._finalize_event(event, voucher, asset.id, data)
        return self._posted_result(asset.id, event, voucher, data=data)

    def _amortization_snapshot(
        self,
        request: PreviewIntangibleAssetAmortizationRequest
        | ConfirmIntangibleAssetAmortizationRequest,
        *,
        lock: bool,
    ) -> dict[str, Any]:
        asset = self._get_asset(request.org_id, request.asset_id, lock=lock)
        if asset is None or not self._acquisition_is_active(asset):
            self._reject("INTANGIBLE_ASSET_NOT_FOUND")
        if self._active_retirement(asset.id) is not None:
            self._reject("INTANGIBLE_ASSET_ALREADY_RETIRED")
        try:
            period_start = date.fromisoformat(f"{request.amortization_period}-01")
        except ValueError:
            self._reject("INTANGIBLE_ASSET_AMORTIZATION_PERIOD_INVALID")
        if self._month_start(request.posting_date) != period_start:
            self._reject("INTANGIBLE_ASSET_AMORTIZATION_PERIOD_INVALID")
        amortizations = self._active_amortizations(asset.id, lock=lock)
        latest_posting_date = (
            amortizations[-1].posting_date if amortizations else asset.posting_date
        )
        if request.posting_date < latest_posting_date:
            self._reject("INTANGIBLE_ASSET_POSTING_DATE_OUT_OF_SEQUENCE")
        completed_months = len(amortizations)
        if completed_months >= asset.useful_life_months:
            self._reject("INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE")
        expected_period = self._add_months(
            self._month_start(asset.available_for_use_date), completed_months
        )
        if period_start != expected_period:
            self._reject("INTANGIBLE_ASSET_AMORTIZATION_OUT_OF_SEQUENCE")
        opening = sum(item.amount_fen for item in amortizations)
        calculation = calculate_straight_line_amortization(
            cost_fen=asset.cost_fen,
            useful_life_months=asset.useful_life_months,
            completed_months=completed_months,
            opening_accumulated_amortization_fen=opening,
        )
        acquisition_evidence_ids = self._event_evidence_ids(asset.acquisition_event_id)
        hash_request = {
            "org_id": str(request.org_id),
            "asset_id": str(asset.id),
            "amortization_period": request.amortization_period,
            "posting_date": request.posting_date.isoformat(),
            "immutable_asset_facts": {
                "acquisition_event_id": str(asset.acquisition_event_id),
                "available_for_use_date": asset.available_for_use_date.isoformat(),
                "cost_fen": asset.cost_fen,
                "useful_life_months": asset.useful_life_months,
                "benefit_area": asset.benefit_area,
                "accounting_rule_version": asset.accounting_rule_version,
            },
            "dependency_event_ids": [str(item.event_id) for item in amortizations],
            "evidence_ids": [str(item) for item in acquisition_evidence_ids],
        }
        calculation_hash = intangible_asset_calculation_hash(
            command="finance_preview_intangible_asset_amortization",
            request=hash_request,
            calculation=calculation,
        )
        data = {
            **asdict(calculation),
            "amortization_period": request.amortization_period,
            "posting_date": request.posting_date.isoformat(),
            "sequence_no": completed_months + 1,
            "benefit_area": asset.benefit_area,
            "dependency_event_ids": [str(item.event_id) for item in amortizations],
            "evidence_ids": [str(item) for item in acquisition_evidence_ids],
            "calculation_hash": calculation_hash,
        }
        trace = [
            {
                "stage": "facts_validated",
                "asset_id": str(asset.id),
                "period_start": period_start.isoformat(),
                "dependency_event_ids": data["dependency_event_ids"],
            },
            self._rule_trace(),
            {
                "stage": "straight_line_amortization_calculated",
                **asdict(calculation),
                "policy": "full_calendar_month_from_available_for_use_month",
            },
            {"stage": "calculation_hashed", "calculation_hash": calculation_hash},
        ]
        return {
            "asset": asset,
            "period_start": period_start,
            "calculation": calculation,
            "calculation_hash": calculation_hash,
            "trace": trace,
            "data": data,
        }

    def _confirm_intangible_asset_amortization_write(
        self, request: ConfirmIntangibleAssetAmortizationRequest
    ) -> IntangibleAssetResult:
        snapshot = self._amortization_snapshot(request, lock=True)
        if request.calculation_hash != snapshot["calculation_hash"]:
            self._reject("INTANGIBLE_ASSET_CALCULATION_STALE")
        calculation = snapshot["calculation"]
        asset = snapshot["asset"]
        trace = [dict(item) for item in snapshot["trace"]]
        trace.append(
            {
                "stage": "calculation_confirmed",
                "confirmation_note": request.confirmation_note,
            }
        )
        event = self._new_event(
            request,
            command="finance_confirm_intangible_asset_amortization",
            event_type="intangible_asset_amortization",
            business_date=snapshot["period_start"],
            posting_date=request.posting_date,
            trace=trace,
        )
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(
            event,
            self._event_evidence_ids(asset.acquisition_event_id),
            relation_kind="inherited",
        )
        row = IntangibleAssetAmortization(
            org_id=request.org_id,
            asset_id=asset.id,
            event_id=event.id,
            period_start=snapshot["period_start"],
            posting_date=request.posting_date,
            sequence_no=snapshot["data"]["sequence_no"],
            amount_fen=calculation.amortization_fen,
            accumulated_after_fen=calculation.closing_accumulated_amortization_fen,
            calculation_hash=snapshot["calculation_hash"],
            accounting_rule_version=SMALL_ENTERPRISE_INTANGIBLE_ASSET_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(row)
        expense_role = {
            "management": "management_amortization_expense",
            "sales": "sales_amortization_expense",
            "service_delivery": "service_cost_amortization",
        }[asset.benefit_area]
        entries = [
            Entry(account_role=expense_role, debit_fen=calculation.amortization_fen),
            Entry(
                account_role="accumulated_amortization",
                credit_fen=calculation.amortization_fen,
            ),
        ]
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.posting_date,
            description=request.confirmation_note
            or f"无形资产摊销 {asset.asset_code} {request.amortization_period}",
            entries=entries,
        )
        trace.extend(
            [
                self._entries_trace(entries),
                {
                    "stage": "normalized_fact_created",
                    "asset_id": str(asset.id),
                    "amortization_id": str(row.id),
                },
            ]
        )
        event.rule_trace = [dict(item) for item in trace]
        event.facts = {**event.facts, "asset_id": str(asset.id)}
        self._finalize_event(event, voucher, asset.id, snapshot["data"])
        return self._posted_result(asset.id, event, voucher, data=snapshot["data"])

    def _retire_intangible_asset_write(
        self, request: RetireIntangibleAssetRequest
    ) -> IntangibleAssetResult:
        asset = self._get_asset(request.org_id, request.asset_id, lock=True)
        if asset is None or not self._acquisition_is_active(asset):
            self._reject("INTANGIBLE_ASSET_NOT_FOUND")
        if self._active_retirement(asset.id) is not None:
            self._reject("INTANGIBLE_ASSET_ALREADY_RETIRED")
        if any(
            value != 0
            for value in (
                request.gross_proceeds_fen,
                request.compensation_fen,
                request.taxes_and_fees_fen,
                request.residual_proceeds_fen,
            )
        ):
            self._reject("INTANGIBLE_ASSET_RETIREMENT_ZERO_FACTS_REQUIRED")
        if request.retirement_date != self._month_end(request.retirement_date):
            self._reject("INTANGIBLE_ASSET_RETIREMENT_NOT_MONTH_END")
        if request.posting_date != request.retirement_date:
            self._reject("INTANGIBLE_ASSET_RETIREMENT_POSTING_DATE_INVALID")
        amortizations = self._active_amortizations(asset.id, lock=True)
        latest_posting_date = (
            amortizations[-1].posting_date if amortizations else asset.posting_date
        )
        if request.posting_date < latest_posting_date:
            self._reject("INTANGIBLE_ASSET_POSTING_DATE_OUT_OF_SEQUENCE")
        accumulated = sum(item.amount_fen for item in amortizations)
        if accumulated < asset.cost_fen:
            retirement_period = self._month_start(request.retirement_date)
            if not amortizations or amortizations[-1].period_start != retirement_period:
                self._reject("INTANGIBLE_ASSET_RETIREMENT_WITH_UNPOSTED_AMORTIZATION")
        self._validate_evidence(request.org_id, request.evidence_references)
        book_value = asset.cost_fen - accumulated
        dependency_ids = [str(item.event_id) for item in amortizations]
        trace = [
            {
                "stage": "facts_validated",
                "command": "finance_retire_intangible_asset",
                "asset_id": str(asset.id),
                "evidence_ids": sorted(map(str, request.evidence_references)),
                "zero_income_facts": {
                    "gross_proceeds_fen": request.gross_proceeds_fen,
                    "compensation_fen": request.compensation_fen,
                    "taxes_and_fees_fen": request.taxes_and_fees_fen,
                    "residual_proceeds_fen": request.residual_proceeds_fen,
                },
                "dependency_event_ids": dependency_ids,
            },
            self._rule_trace(),
            {
                "stage": "retirement_book_value_calculated",
                "cost_fen": asset.cost_fen,
                "accumulated_amortization_fen": accumulated,
                "book_value_fen": book_value,
            },
        ]
        event = self._new_event(
            request,
            command="finance_retire_intangible_asset",
            event_type="intangible_asset_retirement",
            business_date=request.retirement_date,
            posting_date=request.posting_date,
            trace=trace,
        )
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        row = IntangibleAssetRetirement(
            org_id=request.org_id,
            asset_id=asset.id,
            event_id=event.id,
            retirement_date=request.retirement_date,
            posting_date=request.posting_date,
            gross_proceeds_fen=0,
            compensation_fen=0,
            taxes_and_fees_fen=0,
            residual_proceeds_fen=0,
            accumulated_amortization_fen=accumulated,
            book_value_fen=book_value,
            accounting_rule_version=SMALL_ENTERPRISE_INTANGIBLE_ASSET_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(row)
        entries: list[Entry] = []
        if accumulated:
            entries.append(Entry(account_role="accumulated_amortization", debit_fen=accumulated))
        if book_value:
            entries.append(
                Entry(account_role="intangible_asset_retirement_loss", debit_fen=book_value)
            )
        entries.append(Entry(account_role="intangible_asset_cost", credit_fen=asset.cost_fen))
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.posting_date,
            description=request.description or f"报废无形资产 {asset.asset_code}",
            entries=entries,
        )
        trace.extend(
            [
                self._entries_trace(entries),
                {
                    "stage": "normalized_fact_created",
                    "asset_id": str(asset.id),
                    "retirement_id": str(row.id),
                },
            ]
        )
        event.rule_trace = [dict(item) for item in trace]
        event.facts = {**event.facts, "asset_id": str(asset.id)}
        data = {
            "accumulated_amortization_fen": accumulated,
            "book_value_fen": book_value,
            "dependency_event_ids": dependency_ids,
        }
        self._finalize_event(event, voucher, asset.id, data)
        return self._posted_result(asset.id, event, voucher, data=data)

    def _intangible_request_hash(self, command: str, request: Any) -> str:
        return self._canonical_payload_hash(
            {"command": command, "request": request.model_dump(mode="json")}
        )

    def _idempotent_event(self, org_id: uuid.UUID, idempotency_key: str) -> BusinessEvent | None:
        return self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == org_id,
                BusinessEvent.idempotency_key == idempotency_key,
            )
        )

    def _existing_result(
        self, event: BusinessEvent, request_payload_hash: str
    ) -> IntangibleAssetResult:
        if event.request_payload_hash != request_payload_hash:
            return IntangibleAssetResult(
                status=IntangibleAssetResultStatus.REJECTED,
                errors=["INTANGIBLE_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"],
            )
        voucher = event.vouchers[0] if event.vouchers else None
        asset_id = event.facts.get("asset_id")
        status = (
            IntangibleAssetResultStatus.REVERSED
            if event.status == "reversed"
            else IntangibleAssetResultStatus(event.status)
        )
        decision = event.facts.get("_decision", {})
        return IntangibleAssetResult(
            status=status,
            asset_id=uuid.UUID(asset_id) if asset_id else None,
            event_id=event.id,
            voucher_id=voucher.id if voucher else None,
            voucher_number=voucher.voucher_number if voucher else None,
            calculation_hash=event.facts.get("_result_calculation_hash"),
            missing_information=decision.get("missing", []),
            errors=decision.get("errors", []),
            trace=event.rule_trace,
            data={
                **event.facts.get("_result_data", {}),
                "idempotent_replay": True,
                "original_status": event.status,
            },
        )

    def _store_nonposted(
        self,
        command: str,
        request: Any,
        *,
        status: IntangibleAssetResultStatus,
        missing: list[IntangibleAssetInformationRequirement] | None = None,
        errors: list[str] | None = None,
    ) -> IntangibleAssetResult:
        missing = missing or []
        errors = errors or []
        event_type = {
            "finance_acquire_intangible_asset": "intangible_asset_acquisition",
            "finance_confirm_intangible_asset_amortization": ("intangible_asset_amortization"),
            "finance_retire_intangible_asset": "intangible_asset_retirement",
        }[command]
        business_date, posting_date = self._request_dates(request)
        trace = [{"stage": "validation", "status": status.value, "command": command}]
        facts = request.model_dump(mode="json")
        facts["_command"] = command
        facts["_decision"] = {
            "missing": [item.model_dump(mode="json") for item in missing],
            "errors": errors,
        }
        event = BusinessEvent(
            org_id=request.org_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=self._intangible_request_hash(command, request),
            event_type=event_type,
            status=status.value,
            description=getattr(request, "description", ""),
            facts=facts,
            business_date=business_date,
            posting_date=posting_date,
            rule_trace=trace,
            rule_version=SMALL_ENTERPRISE_INTANGIBLE_ASSET_RULE_VERSION,
        )
        self.session.add(event)
        self.session.flush()
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=event.id,
                action=f"intangible_asset_{status.value}",
                details=facts["_decision"],
            )
        )
        return IntangibleAssetResult(
            status=status,
            asset_id=getattr(request, "asset_id", None),
            event_id=event.id,
            missing_information=missing,
            errors=errors,
            trace=trace,
        )

    @staticmethod
    def _request_dates(request: Any) -> tuple[date, date]:
        posting = request.posting_date or date(1970, 1, 1)
        business = (
            getattr(request, "acquisition_date", None)
            or getattr(request, "retirement_date", None)
            or posting
        )
        return business, posting

    def _new_event(
        self,
        request: Any,
        *,
        command: str,
        event_type: str,
        business_date: date,
        posting_date: date,
        trace: list[dict[str, Any]],
        payment_date: date | None = None,
    ) -> BusinessEvent:
        facts = request.model_dump(mode="json")
        facts["_command"] = command
        facts["accounting_rule_version"] = SMALL_ENTERPRISE_INTANGIBLE_ASSET_RULE_VERSION
        facts["accounting_rule_source_url"] = ACCOUNTING_RULE_SOURCE_URL
        return BusinessEvent(
            org_id=request.org_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=self._intangible_request_hash(command, request),
            event_type=event_type,
            status="draft",
            description=getattr(request, "description", ""),
            facts=facts,
            business_date=business_date,
            payment_date=payment_date,
            posting_date=posting_date,
            rule_trace=[dict(item) for item in trace],
            rule_version=SMALL_ENTERPRISE_INTANGIBLE_ASSET_RULE_VERSION,
        )

    def _get_asset(
        self, org_id: uuid.UUID, asset_id: uuid.UUID | None, *, lock: bool = False
    ) -> IntangibleAsset | None:
        if asset_id is None:
            return None
        query = select(IntangibleAsset).where(
            IntangibleAsset.org_id == org_id,
            IntangibleAsset.id == asset_id,
        )
        if lock:
            query = query.order_by(IntangibleAsset.id).with_for_update()
        return self.session.scalar(query)

    def _acquisition_is_active(self, asset: IntangibleAsset) -> bool:
        event = self.session.get(BusinessEvent, asset.acquisition_event_id)
        return event is not None and event.status == "posted"

    def _active_amortizations(
        self, asset_id: uuid.UUID, *, lock: bool = False
    ) -> list[IntangibleAssetAmortization]:
        query = (
            select(IntangibleAssetAmortization)
            .join(BusinessEvent, BusinessEvent.id == IntangibleAssetAmortization.event_id)
            .where(
                IntangibleAssetAmortization.asset_id == asset_id,
                BusinessEvent.status == "posted",
            )
            .order_by(
                IntangibleAssetAmortization.period_start,
                IntangibleAssetAmortization.id,
            )
        )
        if lock:
            query = query.with_for_update()
        return list(self.session.scalars(query).all())

    def _active_retirement(self, asset_id: uuid.UUID) -> IntangibleAssetRetirement | None:
        return self.session.scalar(
            select(IntangibleAssetRetirement)
            .join(BusinessEvent, BusinessEvent.id == IntangibleAssetRetirement.event_id)
            .where(
                IntangibleAssetRetirement.asset_id == asset_id,
                BusinessEvent.status == "posted",
            )
        )

    def _amortization_history(self, asset_id: uuid.UUID) -> list[IntangibleAssetAmortization]:
        return list(
            self.session.scalars(
                select(IntangibleAssetAmortization)
                .where(IntangibleAssetAmortization.asset_id == asset_id)
                .order_by(
                    IntangibleAssetAmortization.period_start,
                    IntangibleAssetAmortization.id,
                )
            ).all()
        )

    def _retirement_history(self, asset_id: uuid.UUID) -> list[IntangibleAssetRetirement]:
        return list(
            self.session.scalars(
                select(IntangibleAssetRetirement)
                .where(IntangibleAssetRetirement.asset_id == asset_id)
                .order_by(
                    IntangibleAssetRetirement.retirement_date,
                    IntangibleAssetRetirement.id,
                )
            ).all()
        )

    def _resolve_supplier(self, org_id: uuid.UUID, reference: Any) -> Counterparty:
        if reference.id is not None:
            row = self.session.scalar(
                select(Counterparty).where(
                    Counterparty.org_id == org_id,
                    Counterparty.id == reference.id,
                )
            )
            if row is None:
                self._reject("INTANGIBLE_ASSET_COUNTERPARTY_NOT_FOUND")
            if row.kind != "supplier" or any(
                supplied is not None and supplied != actual
                for supplied, actual in (
                    (reference.kind, row.kind),
                    (reference.name, row.name),
                    (reference.external_ref, row.external_ref),
                )
            ):
                self._reject("INTANGIBLE_ASSET_COUNTERPARTY_IDENTITY_MISMATCH")
            return row
        if reference.kind != "supplier":
            self._reject("INTANGIBLE_ASSET_COUNTERPARTY_KIND_MISMATCH")
        row = self.session.scalar(
            select(Counterparty).where(
                Counterparty.org_id == org_id,
                Counterparty.kind == "supplier",
                Counterparty.name == reference.name,
            )
        )
        if row is not None:
            if reference.external_ref is not None and reference.external_ref != row.external_ref:
                self._reject("INTANGIBLE_ASSET_COUNTERPARTY_IDENTITY_MISMATCH")
            return row
        if reference.external_ref is not None:
            external_matches = list(
                self.session.scalars(
                    select(Counterparty).where(
                        Counterparty.org_id == org_id,
                        Counterparty.kind == "supplier",
                        Counterparty.external_ref == reference.external_ref,
                    )
                ).all()
            )
            if external_matches:
                self._reject("INTANGIBLE_ASSET_COUNTERPARTY_IDENTITY_MISMATCH")
        row = Counterparty(
            org_id=org_id,
            kind="supplier",
            name=reference.name,
            external_ref=reference.external_ref,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def _validate_evidence(self, org_id: uuid.UUID, evidence_ids: list[uuid.UUID]) -> None:
        if len(evidence_ids) != len(set(evidence_ids)):
            self._reject("INTANGIBLE_ASSET_DUPLICATE_EVIDENCE_REFERENCE")
        found = self.session.scalars(
            select(Evidence.id).where(
                Evidence.org_id == org_id,
                Evidence.id.in_(evidence_ids),
            )
        ).all()
        if len(found) != len(evidence_ids):
            self._reject("INTANGIBLE_ASSET_EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH")

    def _event_evidence_ids(self, event_id: uuid.UUID) -> list[uuid.UUID]:
        return list(
            self.session.scalars(
                select(event_evidence.c.evidence_id)
                .where(event_evidence.c.event_id == event_id)
                .order_by(event_evidence.c.evidence_id)
            ).all()
        )

    def _match_bank_transactions(
        self,
        event: BusinessEvent,
        references: list[Any],
        *,
        bank_account_code: str,
        expected_outflow_fen: int,
        expected_date: date,
    ) -> None:
        try:
            rows = self._resolve_bank_transaction_references(event.org_id, references)
        except ValueError as exc:
            self._reject(str(exc))
        resolved_ids = [row.id for row in rows]
        if any(row.bank_account_code != bank_account_code for row in rows):
            self._reject("BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH")
        if any(row.currency != "CNY" for row in rows):
            self._reject("INTANGIBLE_ASSET_BANK_TRANSACTION_CURRENCY_MISMATCH")
        if any(row.booking_date != expected_date for row in rows):
            self._reject("INTANGIBLE_ASSET_BANK_TRANSACTION_DATE_MISMATCH")
        inflow = sum(row.amount_fen for row in rows if row.amount_fen > 0)
        outflow = -sum(row.amount_fen for row in rows if row.amount_fen < 0)
        if inflow != 0 or outflow != expected_outflow_fen:
            self._reject("INTANGIBLE_ASSET_BANK_TRANSACTION_AMOUNT_MISMATCH")
        matches = self.session.scalars(
            select(BankTransactionMatch)
            .where(
                BankTransactionMatch.org_id == event.org_id,
                BankTransactionMatch.bank_transaction_id.in_(resolved_ids),
                BankTransactionMatch.invalidated_by_event_id.is_(None),
            )
            .with_for_update()
        ).all()
        if matches or any(row.matched_event_id is not None for row in rows):
            self._reject("BANK_TRANSACTION_ALREADY_MATCHED")
        for row in rows:
            self.session.add(
                BankTransactionMatch(
                    org_id=event.org_id,
                    bank_transaction_id=row.id,
                    event_id=event.id,
                )
            )
            row.matched_event_id = event.id

    def _asset_for_event(self, event: BusinessEvent) -> IntangibleAsset | None:
        if event.event_type == "intangible_asset_acquisition":
            return self.session.scalar(
                select(IntangibleAsset).where(
                    IntangibleAsset.org_id == event.org_id,
                    IntangibleAsset.acquisition_event_id == event.id,
                )
            )
        model = {
            "intangible_asset_amortization": IntangibleAssetAmortization,
            "intangible_asset_retirement": IntangibleAssetRetirement,
        }.get(event.event_type)
        if model is None:
            return None
        asset_id = self.session.scalar(
            select(model.asset_id).where(
                model.org_id == event.org_id,
                model.event_id == event.id,
            )
        )
        return self._get_asset(event.org_id, asset_id)

    def intangible_asset_reversal_dependency_error(
        self, original: BusinessEvent, asset: IntangibleAsset
    ) -> str | None:
        if original.status != "posted" or original.reversed_by_event_id is not None:
            return None
        retirement = self._active_retirement(asset.id)
        amortizations = self._active_amortizations(asset.id, lock=True)
        if original.event_type == "intangible_asset_retirement":
            return None
        if original.event_type == "intangible_asset_amortization":
            source = self.session.scalar(
                select(IntangibleAssetAmortization).where(
                    IntangibleAssetAmortization.org_id == original.org_id,
                    IntangibleAssetAmortization.event_id == original.id,
                )
            )
            if source is None or retirement is not None:
                return "INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST"
            if any(item.period_start > source.period_start for item in amortizations):
                return "INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST"
            return None
        if original.event_type == "intangible_asset_acquisition" and (
            retirement is not None or amortizations
        ):
            return "INTANGIBLE_ASSET_OPEN_DEPENDENCIES_EXIST"
        return None

    def _finalize_event(
        self,
        event: BusinessEvent,
        voucher: Voucher,
        asset_id: uuid.UUID,
        result_data: dict[str, Any],
    ) -> None:
        event.facts = {
            **event.facts,
            "_result_data": result_data,
            "_result_calculation_hash": result_data.get("calculation_hash"),
        }
        self.session.flush()
        event.status = "posted"
        self.session.add(
            AuditLog(
                org_id=event.org_id,
                event_id=event.id,
                action="intangible_asset_event_posted",
                details={
                    "asset_id": str(asset_id),
                    "voucher_id": str(voucher.id),
                    "voucher_number": voucher.voucher_number,
                },
            )
        )
        self.session.flush()

    @staticmethod
    def _posted_result(
        asset_id: uuid.UUID,
        event: BusinessEvent,
        voucher: Voucher,
        *,
        data: dict[str, Any],
    ) -> IntangibleAssetResult:
        return IntangibleAssetResult(
            status=IntangibleAssetResultStatus.POSTED,
            asset_id=asset_id,
            event_id=event.id,
            voucher_id=voucher.id,
            voucher_number=voucher.voucher_number,
            calculation_hash=data.get("calculation_hash"),
            trace=event.rule_trace,
            data=data,
        )

    @staticmethod
    def _reject(code: str) -> None:
        raise _IntangibleAssetDecision(IntangibleAssetResultStatus.REJECTED, code)

    @staticmethod
    def _month_start(value: date) -> date:
        return date(value.year, value.month, 1)

    @classmethod
    def _month_end(cls, value: date) -> date:
        return date(value.year, value.month, monthrange(value.year, value.month)[1])

    @staticmethod
    def _add_months(value: date, months: int) -> date:
        if months < 0:
            raise IntangibleAssetCalculationError(
                "INTANGIBLE_ASSET_AMORTIZATION_DATE_OUT_OF_RANGE",
                "month offset must be non-negative",
            )
        index = value.year * 12 + value.month - 1 + months
        if index > 9999 * 12 + 11:
            raise IntangibleAssetCalculationError(
                "INTANGIBLE_ASSET_AMORTIZATION_DATE_OUT_OF_RANGE",
                "useful life exceeds the supported calendar",
            )
        return date(index // 12, index % 12 + 1, 1)

    @staticmethod
    def _rule_trace() -> dict[str, Any]:
        return {
            "stage": "rule_selected",
            "rule": "small_enterprise_intangible_assets",
            "version": SMALL_ENTERPRISE_INTANGIBLE_ASSET_RULE_VERSION,
            "effective_from": "2013-01-01",
            "source_url": ACCOUNTING_RULE_SOURCE_URL,
            "scope": "book accounting only; no tax amortization adjustment",
        }

    @staticmethod
    def _entries_trace(entries: list[Entry]) -> dict[str, Any]:
        return {
            "stage": "entries_created",
            "template_lines": [
                {
                    "account_role": line.account_role,
                    "account_code": line.account_code,
                    "debit_fen": line.debit_fen,
                    "credit_fen": line.credit_fen,
                }
                for line in entries
            ],
            "debit_fen": sum(line.debit_fen for line in entries),
            "credit_fen": sum(line.credit_fen for line in entries),
        }

    def _event_projection(self, event: BusinessEvent | None) -> dict[str, Any] | None:
        if event is None:
            return None
        voucher = self.session.scalar(select(Voucher).where(Voucher.event_id == event.id))
        evidence_ids = self.session.scalars(
            select(event_evidence.c.evidence_id)
            .where(
                event_evidence.c.org_id == event.org_id,
                event_evidence.c.event_id == event.id,
            )
            .order_by(event_evidence.c.evidence_id)
        ).all()
        return {
            "id": str(event.id),
            "event_type": event.event_type,
            "status": event.status,
            "reversed_by_event_id": (
                str(event.reversed_by_event_id) if event.reversed_by_event_id else None
            ),
            "rule_version": event.rule_version,
            "evidence_ids": [str(item) for item in evidence_ids],
            "voucher": (
                {
                    "id": str(voucher.id),
                    "voucher_number": voucher.voucher_number,
                    "status": voucher.status,
                }
                if voucher is not None
                else None
            ),
            "trace": event.rule_trace,
        }
