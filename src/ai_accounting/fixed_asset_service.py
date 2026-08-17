"""Deterministic fixed-asset workflow service.

Public requests contain business facts only.  Every journal line below is selected
from a closed posting template and an organization-owned system account role.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import asdict
from datetime import date
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError, IntegrityError, OperationalError
from sqlalchemy.orm import Session

from .fixed_assets import (
    SMALL_ENTERPRISE_FIXED_ASSET_RULE_VERSION,
    SMALL_SCALE_USED_FIXED_ASSET_VAT_RULE_VERSION,
    FixedAssetCalculationError,
    calculate_acquisition_cost,
    calculate_straight_line_depreciation,
    calculate_used_fixed_asset_vat,
    fixed_asset_calculation_hash,
)
from .ledger import AccountingPeriodError, Entry, create_voucher
from .models import (
    AuditLog,
    BankTransactionMatch,
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
    Voucher,
    event_evidence,
)
from .schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    ConfirmFixedAssetDepreciationRequest,
    DisposeFixedAssetRequest,
    FinanceResult,
    FixedAssetInformationRequirement,
    FixedAssetResult,
    FixedAssetResultStatus,
    PreviewFixedAssetDepreciationRequest,
    ResultStatus,
    ReverseEventRequest,
)
from .service import FinanceService

ACCOUNTING_RULE_SOURCE_URL = "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852319878.pdf"
USED_FIXED_ASSET_VAT_RULE_CODE = "small_scale_used_fixed_asset_vat_2026"
FIXED_ASSET_EVENT_TYPES = {
    "fixed_asset_acquisition",
    "fixed_asset_activation",
    "fixed_asset_depreciation",
    "fixed_asset_disposal",
}


class _FixedAssetDecision(ValueError):
    def __init__(
        self,
        status: FixedAssetResultStatus,
        code: str,
        *,
        missing: list[FixedAssetInformationRequirement] | None = None,
    ) -> None:
        self.status = status
        self.code = code
        self.missing = missing or []
        super().__init__(code)


class FixedAssetService(FinanceService):
    """Post the specialized fixed-asset lifecycle on top of common ledger controls."""

    def __init__(self, session: Session):
        super().__init__(session)

    def reverse_event(self, request: ReverseEventRequest) -> FinanceResult:
        """Preserve fixed-asset idempotency codes around the common reversal writer."""

        original = self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.id == request.event_id,
            )
        )
        if original is None or original.event_type not in FIXED_ASSET_EVENT_TYPES:
            return super().reverse_event(request)
        request_payload_hash = self._request_payload_hash(request)
        existing = self._fixed_asset_idempotent_event(request.org_id, request.idempotency_key)
        if existing is not None:
            if existing.request_payload_hash != request_payload_hash:
                return FinanceResult(
                    status=ResultStatus.REJECTED,
                    errors=["FIXED_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"],
                )
            return self._result_for_existing(existing)
        return super().reverse_event(request)

    def _reverse_event_write(self, request: ReverseEventRequest) -> FinanceResult:
        """Enter the asset lock domain and enforce strict downstream-first reversal."""

        original = self.session.scalar(
            select(BusinessEvent)
            .where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.id == request.event_id,
            )
            .with_for_update()
        )
        if original is None or original.event_type not in FIXED_ASSET_EVENT_TYPES:
            return super()._reverse_event_write(request)
        asset = self._asset_for_fixed_asset_event(original)
        if asset is None:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["FIXED_ASSET_NORMALIZED_FACT_NOT_FOUND"],
            )
        asset = self._get_asset(request.org_id, asset.id, lock=True)
        if asset is None:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["FIXED_ASSET_NOT_FOUND"],
            )
        if self.fixed_asset_reversal_dependency_error(original, asset) is not None:
            return FinanceResult(
                status=ResultStatus.REJECTED,
                errors=["FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"],
            )
        return super()._reverse_event_write(request)

    def acquire_fixed_asset(self, request: AcquireFixedAssetRequest) -> FixedAssetResult:
        return self._run_write(
            "finance_acquire_fixed_asset",
            request,
            lambda: self._acquire_fixed_asset_write(request),
        )

    def activate_fixed_asset(self, request: ActivateFixedAssetRequest) -> FixedAssetResult:
        return self._run_write(
            "finance_activate_fixed_asset",
            request,
            lambda: self._activate_fixed_asset_write(request),
        )

    def preview_fixed_asset_depreciation(
        self, request: PreviewFixedAssetDepreciationRequest
    ) -> FixedAssetResult:
        if self.session.get(Organization, request.org_id) is None:
            return FixedAssetResult(
                status=FixedAssetResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        missing = request.missing_information()
        if missing:
            return FixedAssetResult(
                status=FixedAssetResultStatus.NEEDS_INFORMATION,
                asset_id=request.asset_id,
                missing_information=missing,
                trace=[{"stage": "validation", "status": "needs_information"}],
            )
        try:
            snapshot = self._depreciation_snapshot(request, lock=False)
        except _FixedAssetDecision as exc:
            return FixedAssetResult(
                status=exc.status,
                asset_id=request.asset_id,
                errors=[exc.code],
            )
        except FixedAssetCalculationError as exc:
            return FixedAssetResult(
                status=FixedAssetResultStatus.REJECTED,
                asset_id=request.asset_id,
                errors=[exc.code],
            )
        return FixedAssetResult(
            status=FixedAssetResultStatus.CALCULATED,
            asset_id=snapshot["asset"].id,
            calculation_hash=snapshot["calculation_hash"],
            trace=snapshot["trace"],
            data=snapshot["data"],
        )

    def confirm_fixed_asset_depreciation(
        self, request: ConfirmFixedAssetDepreciationRequest
    ) -> FixedAssetResult:
        return self._run_write(
            "finance_confirm_fixed_asset_depreciation",
            request,
            lambda: self._confirm_fixed_asset_depreciation_write(request),
        )

    def dispose_fixed_asset(self, request: DisposeFixedAssetRequest) -> FixedAssetResult:
        return self._run_write(
            "finance_dispose_fixed_asset",
            request,
            lambda: self._dispose_fixed_asset_write(request),
        )

    def get_fixed_asset(self, org_id: uuid.UUID, asset_id: uuid.UUID) -> FixedAssetResult:
        if self.session.get(Organization, org_id) is None:
            return FixedAssetResult(
                status=FixedAssetResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        asset = self._get_asset(org_id, asset_id)
        if asset is None:
            return FixedAssetResult(
                status=FixedAssetResultStatus.REJECTED,
                errors=["FIXED_ASSET_NOT_FOUND"],
            )
        acquisition = self.session.get(BusinessEvent, asset.acquisition_event_id)
        activation = self._active_activation(asset.id)
        depreciations = self._active_depreciations(asset.id)
        disposal = self._active_disposal(asset.id)
        activation_history = list(
            self.session.scalars(
                select(FixedAssetActivation)
                .where(
                    FixedAssetActivation.org_id == org_id,
                    FixedAssetActivation.asset_id == asset.id,
                )
                .order_by(FixedAssetActivation.created_at, FixedAssetActivation.id)
            ).all()
        )
        depreciation_history = list(
            self.session.scalars(
                select(FixedAssetDepreciation)
                .where(
                    FixedAssetDepreciation.org_id == org_id,
                    FixedAssetDepreciation.asset_id == asset.id,
                )
                .order_by(
                    FixedAssetDepreciation.period_start,
                    FixedAssetDepreciation.created_at,
                    FixedAssetDepreciation.id,
                )
            ).all()
        )
        disposal_history = list(
            self.session.scalars(
                select(FixedAssetDisposal)
                .where(
                    FixedAssetDisposal.org_id == org_id,
                    FixedAssetDisposal.asset_id == asset.id,
                )
                .order_by(FixedAssetDisposal.created_at, FixedAssetDisposal.id)
            ).all()
        )
        accumulated = sum(item.amount_fen for item in depreciations)
        state = "acquired"
        result_status = FixedAssetResultStatus.POSTED
        if acquisition is None or acquisition.status == "reversed":
            state = "reversed"
            result_status = FixedAssetResultStatus.REVERSED
        elif disposal is not None:
            state = "disposed"
        elif activation is not None:
            state = "active"
        return FixedAssetResult(
            status=result_status,
            asset_id=asset.id,
            event_id=(
                disposal.event_id
                if disposal is not None
                else activation.event_id
                if activation is not None
                else asset.acquisition_event_id
            ),
            trace=[
                {
                    "stage": "fixed_asset_projected",
                    "state": state,
                    "source": "immutable normalized facts and business event statuses",
                }
            ],
            data={
                "state": state,
                "asset": {
                    "id": str(asset.id),
                    "asset_code": asset.asset_code,
                    "asset_name": asset.name,
                    "category": asset.category,
                    "acquisition_date": asset.acquisition_date.isoformat(),
                    "cost_fen": asset.cost_fen,
                    "settlement_method": asset.settlement_method,
                    "acquisition_event_id": str(asset.acquisition_event_id),
                    "purchase_price_fen": asset.purchase_price_fen,
                    "noncreditable_tax_fen": asset.noncreditable_tax_fen,
                    "transport_and_handling_fen": asset.transport_and_handling_fen,
                    "installation_and_direct_cost_fen": (asset.installation_and_direct_cost_fen),
                    "accounting_rule_version": asset.accounting_rule_version,
                    "accounting_rule_source_url": asset.accounting_rule_source_url,
                    "event": self._event_audit_projection(asset.acquisition_event_id),
                },
                "activation": self._activation_projection(activation),
                "activation_history": [
                    {
                        **(self._activation_projection(item) or {}),
                        "posting_date": item.posting_date.isoformat(),
                        "accounting_rule_version": item.accounting_rule_version,
                        "accounting_rule_source_url": item.accounting_rule_source_url,
                        "event": self._event_audit_projection(item.event_id),
                    }
                    for item in activation_history
                ],
                "depreciations": [
                    {
                        "id": str(item.id),
                        "event_id": str(item.event_id),
                        "activation_id": str(item.activation_id),
                        "period": item.period_start.strftime("%Y-%m"),
                        "sequence_no": item.sequence_no,
                        "amount_fen": item.amount_fen,
                        "accumulated_after_fen": item.accumulated_after_fen,
                        "calculation_hash": item.calculation_hash,
                    }
                    for item in depreciations
                ],
                "depreciation_history": [
                    {
                        "id": str(item.id),
                        "event_id": str(item.event_id),
                        "activation_id": str(item.activation_id),
                        "period": item.period_start.strftime("%Y-%m"),
                        "posting_date": item.posting_date.isoformat(),
                        "sequence_no": item.sequence_no,
                        "amount_fen": item.amount_fen,
                        "accumulated_after_fen": item.accumulated_after_fen,
                        "calculation_hash": item.calculation_hash,
                        "accounting_rule_version": item.accounting_rule_version,
                        "accounting_rule_source_url": item.accounting_rule_source_url,
                        "event": self._event_audit_projection(item.event_id),
                    }
                    for item in depreciation_history
                ],
                "accumulated_depreciation_fen": accumulated,
                "book_value_fen": asset.cost_fen - accumulated,
                "disposal": self._disposal_projection(disposal),
                "disposal_history": [
                    {
                        **(self._disposal_projection(item) or {}),
                        "posting_date": item.posting_date.isoformat(),
                        "accumulated_depreciation_fen": (item.accumulated_depreciation_fen),
                        "accounting_rule_version": item.accounting_rule_version,
                        "accounting_rule_source_url": item.accounting_rule_source_url,
                        "event": self._event_audit_projection(item.event_id),
                    }
                    for item in disposal_history
                ],
            },
        )

    def _run_write(
        self,
        command: str,
        request: Any,
        writer: Callable[[], FixedAssetResult],
    ) -> FixedAssetResult:
        if self.session.get(Organization, request.org_id) is None:
            return FixedAssetResult(
                status=FixedAssetResultStatus.REJECTED,
                errors=["ORGANIZATION_NOT_FOUND"],
            )
        payload_hash = self._fixed_asset_request_hash(command, request)
        existing = self._fixed_asset_idempotent_event(request.org_id, request.idempotency_key)
        if existing is not None:
            return self._fixed_asset_existing_result(existing, payload_hash)

        bank_settled = (
            isinstance(request, AcquireFixedAssetRequest)
            and request.settlement_method is not None
            and request.settlement_method.value == "bank"
        ) or (
            isinstance(request, DisposeFixedAssetRequest)
            and (
                (
                    request.settlement_method is not None
                    and request.settlement_method.value == "bank"
                )
                or bool(request.clearance_cost_fen)
            )
        )
        if bank_settled and not self._bank_reconciliation_scope_is_confirmed(
            self.session.get(Organization, request.org_id)
        ):
            requirement = FixedAssetInformationRequirement(
                code="BANK_RECONCILIATION_SCOPE_CONFIRMATION_REQUIRED",
                message="owner-confirmed bank reconciliation scope is required",
                fields=["bank_reconciliation_scope_confirmation"],
            )
            return FixedAssetResult(
                status=FixedAssetResultStatus.NEEDS_INFORMATION,
                missing_information=[requirement],
                trace=[
                    {
                        "stage": "validation",
                        "status": "needs_information",
                        "code": requirement.code,
                    }
                ],
            )

        missing = request.missing_information()
        if missing:
            return self._store_fixed_asset_nonposted(
                command,
                request,
                status=FixedAssetResultStatus.NEEDS_INFORMATION,
                missing=missing,
            )
        try:
            with self.session.begin_nested():
                return writer()
        except _FixedAssetDecision as exc:
            return self._store_fixed_asset_nonposted(
                command,
                request,
                status=exc.status,
                errors=[exc.code] if not exc.missing else [],
                missing=exc.missing,
            )
        except FixedAssetCalculationError as exc:
            return self._store_fixed_asset_nonposted(
                command,
                request,
                status=FixedAssetResultStatus.REJECTED,
                errors=[exc.code],
            )
        except AccountingPeriodError as exc:
            return FixedAssetResult(
                status=FixedAssetResultStatus.REJECTED,
                errors=[exc.code],
            )
        except IntegrityError as exc:
            if self._is_tax_period_source_lock_error(exc):
                return FixedAssetResult(
                    status=FixedAssetResultStatus.REJECTED,
                    errors=["TAX_PERIOD_SOURCE_LOCKED"],
                )
            existing = self._fixed_asset_idempotent_event(request.org_id, request.idempotency_key)
            if existing is not None:
                return self._fixed_asset_existing_result(existing, payload_hash)
            return FixedAssetResult(
                status=FixedAssetResultStatus.REJECTED,
                errors=["FIXED_ASSET_CONCURRENT_WRITE_CONFLICT"],
            )
        except OperationalError:
            return FixedAssetResult(
                status=FixedAssetResultStatus.REJECTED,
                errors=["FIXED_ASSET_CONCURRENT_WRITE_CONFLICT"],
            )
        except DBAPIError as exc:
            if self._is_tax_period_source_lock_error(exc):
                return FixedAssetResult(
                    status=FixedAssetResultStatus.REJECTED,
                    errors=["TAX_PERIOD_SOURCE_LOCKED"],
                )
            raise

    def _acquire_fixed_asset_write(self, request: AcquireFixedAssetRequest) -> FixedAssetResult:
        if request.expected_use_over_one_year is not True:
            self._reject("MODULE_NOT_ENABLED:fixed_asset_short_term_item")
        if request.claims_creditable_input_vat is not False:
            self._reject("MODULE_NOT_ENABLED:fixed_asset_creditable_input_vat")
        if self.session.scalar(
            select(FixedAsset.id).where(
                FixedAsset.org_id == request.org_id,
                FixedAsset.asset_code == request.asset_code,
            )
        ):
            self._reject("FIXED_ASSET_CODE_ALREADY_EXISTS")

        components = request.cost_components
        cost = calculate_acquisition_cost(
            purchase_price_fen=components.purchase_price_fen,
            noncreditable_tax_fen=components.noncreditable_tax_fen,
            transport_and_handling_fen=components.transport_and_handling_fen,
            installation_and_direct_cost_fen=components.installation_and_direct_cost_fen,
        )
        supplier = self._resolve_fixed_asset_counterparty(
            request.org_id, request.supplier, required_kind="supplier"
        )
        settlement_method = request.settlement_method.value
        reimbursing_employee = None
        if settlement_method == "employee_payable":
            reimbursing_employee = self._resolve_fixed_asset_counterparty(
                request.org_id, request.reimbursing_employee, required_kind="employee"
            )
        if settlement_method == "bank":
            self._validate_bank_account(
                request.org_id, request.bank_account_code, request.payment_date
            )
        trace = [
            {
                "stage": "facts_validated",
                "command": "finance_acquire_fixed_asset",
                "evidence_ids": sorted(map(str, request.evidence_references)),
                "cost_components": {
                    "purchase_price_fen": cost.purchase_price_fen,
                    "noncreditable_tax_fen": cost.noncreditable_tax_fen,
                    "transport_and_handling_fen": cost.transport_and_handling_fen,
                    "installation_and_direct_cost_fen": cost.installation_and_direct_cost_fen,
                    "cost_fen": cost.cost_fen,
                },
                "bank_account_code": request.bank_account_code,
            },
            self._accounting_rule_trace(),
        ]
        self._validate_fixed_asset_evidence(request.org_id, request.evidence_references)
        event = self._new_fixed_asset_event(
            request,
            command="finance_acquire_fixed_asset",
            event_type="fixed_asset_acquisition",
            business_date=request.purchase_date,
            posting_date=request.posting_date,
            payment_date=request.payment_date,
            trace=trace,
        )
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        if settlement_method == "bank" and request.bank_transaction_references:
            self._match_fixed_asset_bank_transactions(
                event,
                request.bank_transaction_references,
                bank_account_code=request.bank_account_code,
                expected_inflow_fen=0,
                expected_outflow_fen=cost.cost_fen,
                expected_date=request.payment_date,
            )
        elif request.bank_transaction_references:
            self._reject("FIXED_ASSET_PAYABLE_FORBIDS_BANK_TRANSACTIONS")

        asset = FixedAsset(
            org_id=request.org_id,
            asset_code=request.asset_code,
            name=request.asset_name,
            category=request.category.value,
            expected_use_over_one_year=True,
            acquisition_date=request.purchase_date,
            posting_date=request.posting_date,
            purchase_price_fen=cost.purchase_price_fen,
            noncreditable_tax_fen=cost.noncreditable_tax_fen,
            transport_and_handling_fen=cost.transport_and_handling_fen,
            installation_and_direct_cost_fen=cost.installation_and_direct_cost_fen,
            cost_fen=cost.cost_fen,
            supplier_id=supplier.id,
            reimbursing_employee_id=(
                reimbursing_employee.id if reimbursing_employee is not None else None
            ),
            settlement_method=settlement_method,
            payment_date=request.payment_date if settlement_method == "bank" else None,
            due_date=(
                request.due_date
                if settlement_method in {"payable", "employee_payable"}
                else None
            ),
            acquisition_event_id=event.id,
            accounting_rule_version=SMALL_ENTERPRISE_FIXED_ASSET_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(asset)
        self.session.flush()
        activation = None
        if request.ready_for_use is not None:
            ready = request.ready_for_use
            if ready.residual_value_fen >= cost.cost_fen:
                self._reject("FIXED_ASSET_INVALID_RESIDUAL_VALUE")
            if cost.cost_fen - ready.residual_value_fen < ready.useful_life_months:
                self._reject("FIXED_ASSET_INVALID_DEPRECIATION_POLICY")
            activation = FixedAssetActivation(
                org_id=request.org_id,
                asset_id=asset.id,
                event_id=event.id,
                in_service_date=ready.in_service_date,
                posting_date=request.posting_date,
                depreciation_method=ready.depreciation_method.value,
                useful_life_months=ready.useful_life_months,
                residual_value_fen=ready.residual_value_fen,
                benefit_area=ready.benefit_area.value,
                accounting_rule_version=SMALL_ENTERPRISE_FIXED_ASSET_RULE_VERSION,
                accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
            )
            self.session.add(activation)
            self.session.flush()
        entries = [
            Entry(
                account_role=(
                    "fixed_asset_cost" if activation is not None else "fixed_asset_pending"
                ),
                debit_fen=cost.cost_fen,
            ),
            Entry(
                account_code=(request.bank_account_code if settlement_method == "bank" else None),
                account_role=(
                    None
                    if settlement_method == "bank"
                    else (
                        "employee_payable"
                        if settlement_method == "employee_payable"
                        else "accounts_payable"
                    )
                ),
                credit_fen=cost.cost_fen,
                counterparty_id=(
                    reimbursing_employee.id
                    if reimbursing_employee is not None
                    else (supplier.id if settlement_method == "payable" else None)
                ),
            ),
        ]
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.posting_date,
            description=request.description
            or (
                f"购置并启用固定资产 {request.asset_code}"
                if activation is not None
                else f"购置待启用固定资产 {request.asset_code}"
            ),
            entries=entries,
        )
        if settlement_method in {"payable", "employee_payable"}:
            payable_counterparty = (
                reimbursing_employee if reimbursing_employee is not None else supplier
            )
            self.session.add(
                OpenItem(
                    org_id=request.org_id,
                    counterparty_id=payable_counterparty.id,
                    source_event_id=event.id,
                    item_type="payable",
                    original_amount_fen=cost.cost_fen,
                    due_date=request.due_date,
                )
            )
        trace.append(self._entries_trace(entries))
        trace.append({"stage": "normalized_fact_created", "asset_id": str(asset.id)})
        if activation is not None:
            trace.append(
                {
                    "stage": "normalized_fact_created",
                    "activation_id": str(activation.id),
                    "combined_with_acquisition": True,
                }
            )
        event.facts = {**event.facts, "asset_id": str(asset.id)}
        event.rule_trace = [dict(item) for item in trace]
        result_data = {
            "cost_fen": cost.cost_fen,
            "state": "active" if activation is not None else "acquired",
            "activation_id": str(activation.id) if activation is not None else None,
        }
        self._finalize_fixed_asset_event(event, voucher, asset.id, result_data)
        return self._posted_result(asset.id, event, voucher, data=result_data)

    def _activate_fixed_asset_write(self, request: ActivateFixedAssetRequest) -> FixedAssetResult:
        asset = self._get_asset(request.org_id, request.asset_id, lock=True)
        if asset is None:
            self._reject("FIXED_ASSET_NOT_FOUND")
        acquisition = self.session.get(BusinessEvent, asset.acquisition_event_id)
        if acquisition is None or acquisition.status != "posted":
            self._reject("FIXED_ASSET_NOT_ACTIVATABLE")
        if self._active_disposal(asset.id) is not None:
            self._reject("FIXED_ASSET_ALREADY_DISPOSED")
        if self._active_activation(asset.id) is not None:
            self._reject("FIXED_ASSET_ALREADY_ACTIVATED")
        if request.activation_date < asset.acquisition_date:
            self._reject("FIXED_ASSET_NOT_ACTIVATABLE")
        if request.posting_date < asset.posting_date:
            self._reject("FIXED_ASSET_POSTING_DATE_OUT_OF_SEQUENCE")
        if request.residual_value_fen >= asset.cost_fen:
            self._reject("FIXED_ASSET_INVALID_RESIDUAL_VALUE")
        if asset.cost_fen - request.residual_value_fen < request.useful_life_months:
            self._reject("FIXED_ASSET_INVALID_DEPRECIATION_POLICY")

        trace = [
            {
                "stage": "facts_validated",
                "command": "finance_activate_fixed_asset",
                "asset_id": str(asset.id),
                "cost_fen": asset.cost_fen,
                "residual_value_fen": request.residual_value_fen,
                "useful_life_months": request.useful_life_months,
                "benefit_area": request.benefit_area.value,
                "evidence_ids": sorted(map(str, request.evidence_references)),
                "dependency_event_ids": [str(asset.acquisition_event_id)],
            },
            self._accounting_rule_trace(),
        ]
        self._validate_fixed_asset_evidence(request.org_id, request.evidence_references)
        event = self._new_fixed_asset_event(
            request,
            command="finance_activate_fixed_asset",
            event_type="fixed_asset_activation",
            business_date=request.activation_date,
            posting_date=request.posting_date,
            trace=trace,
        )
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        activation = FixedAssetActivation(
            org_id=request.org_id,
            asset_id=asset.id,
            event_id=event.id,
            in_service_date=request.activation_date,
            posting_date=request.posting_date,
            depreciation_method=request.depreciation_method.value,
            useful_life_months=request.useful_life_months,
            residual_value_fen=request.residual_value_fen,
            benefit_area=request.benefit_area.value,
            accounting_rule_version=SMALL_ENTERPRISE_FIXED_ASSET_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(activation)
        self.session.flush()
        entries = [
            Entry(account_role="fixed_asset_cost", debit_fen=asset.cost_fen),
            Entry(account_role="fixed_asset_pending", credit_fen=asset.cost_fen),
        ]
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.posting_date,
            description=request.description or f"启用固定资产 {asset.asset_code}",
            entries=entries,
        )
        trace.append(self._entries_trace(entries))
        trace.append({"stage": "normalized_fact_created", "activation_id": str(activation.id)})
        event.facts = {**event.facts, "asset_id": str(asset.id)}
        event.rule_trace = [dict(item) for item in trace]
        self._finalize_fixed_asset_event(event, voucher, asset.id, {})
        return self._posted_result(asset.id, event, voucher)

    def _depreciation_snapshot(
        self,
        request: PreviewFixedAssetDepreciationRequest | ConfirmFixedAssetDepreciationRequest,
        *,
        lock: bool,
    ) -> dict[str, Any]:
        asset = self._get_asset(request.org_id, request.asset_id, lock=lock)
        if asset is None:
            self._reject("FIXED_ASSET_NOT_FOUND")
        if self._active_disposal(asset.id) is not None:
            self._reject("FIXED_ASSET_ALREADY_DISPOSED")
        activation = self._active_activation(asset.id)
        if activation is None:
            self._reject("FIXED_ASSET_NOT_ACTIVATABLE")
        period_start = self._parse_period(request.depreciation_period)
        if request.posting_date.replace(day=1) != period_start:
            self._reject("FIXED_ASSET_DEPRECIATION_PERIOD_INVALID")
        if request.posting_date < activation.posting_date:
            self._reject("FIXED_ASSET_POSTING_DATE_OUT_OF_SEQUENCE")
        depreciations = self._active_depreciations(asset.id, lock=lock)
        if any(item.period_start == period_start for item in depreciations):
            self._reject("FIXED_ASSET_DEPRECIATION_ALREADY_POSTED")
        expected_period = self._add_months(activation.in_service_date.replace(day=1), 1)
        if depreciations:
            expected_period = self._add_months(depreciations[-1].period_start, 1)
        if period_start != expected_period:
            self._reject("FIXED_ASSET_DEPRECIATION_OUT_OF_SEQUENCE")

        opening_accumulated = sum(item.amount_fen for item in depreciations)
        calculation = calculate_straight_line_depreciation(
            cost_fen=asset.cost_fen,
            residual_value_fen=activation.residual_value_fen,
            useful_life_months=activation.useful_life_months,
            completed_months=len(depreciations),
            opening_accumulated_depreciation_fen=opening_accumulated,
        )
        calculation_data = {
            **asdict(calculation),
            "amount_fen": calculation.depreciation_fen,
            "accumulated_after_fen": calculation.closing_accumulated_depreciation_fen,
            "asset_id": str(asset.id),
            "activation_id": str(activation.id),
            "activation_event_id": str(activation.event_id),
            "period_start": period_start.isoformat(),
            "sequence_no": len(depreciations) + 1,
            "benefit_area": activation.benefit_area,
            "accounting_rule_version": activation.accounting_rule_version,
            "accounting_rule_source_url": activation.accounting_rule_source_url,
        }
        hash_request = self._depreciation_hash_request(request)
        calculation_hash = fixed_asset_calculation_hash(
            command="finance_preview_fixed_asset_depreciation",
            request=hash_request,
            calculation=calculation_data,
        )
        trace = [
            {
                "stage": "facts_validated",
                "command": "finance_preview_fixed_asset_depreciation",
                "asset_id": str(asset.id),
                "activation_event_id": str(activation.event_id),
                "prior_depreciation_event_ids": [str(item.event_id) for item in depreciations],
            },
            self._accounting_rule_trace(),
            {
                "stage": "depreciation_calculated",
                "formula": (
                    "base=(cost-residual)//life; final month=depreciable-prior accumulated"
                ),
                **calculation_data,
                "calculation_hash": calculation_hash,
            },
        ]
        return {
            "asset": asset,
            "activation": activation,
            "depreciations": depreciations,
            "period_start": period_start,
            "calculation": calculation,
            "calculation_hash": calculation_hash,
            "trace": trace,
            "data": calculation_data,
        }

    def _confirm_fixed_asset_depreciation_write(
        self, request: ConfirmFixedAssetDepreciationRequest
    ) -> FixedAssetResult:
        snapshot = self._depreciation_snapshot(request, lock=True)
        if request.calculation_hash != snapshot["calculation_hash"]:
            self._reject("FIXED_ASSET_CALCULATION_STALE")
        asset: FixedAsset = snapshot["asset"]
        activation: FixedAssetActivation = snapshot["activation"]
        calculation = snapshot["calculation"]
        trace = list(snapshot["trace"])
        trace[0] = {
            **trace[0],
            "command": "finance_confirm_fixed_asset_depreciation",
        }
        event = self._new_fixed_asset_event(
            request,
            command="finance_confirm_fixed_asset_depreciation",
            event_type="fixed_asset_depreciation",
            business_date=snapshot["period_start"],
            posting_date=request.posting_date,
            trace=trace,
        )
        event.facts = {
            **event.facts,
            "asset_id": str(asset.id),
            "activation_id": str(activation.id),
            "calculation": snapshot["data"],
        }
        self.session.add(event)
        self.session.flush()
        inherited_evidence = self.session.scalars(
            select(event_evidence.c.evidence_id)
            .where(
                event_evidence.c.org_id == request.org_id,
                event_evidence.c.event_id == activation.event_id,
            )
            .order_by(event_evidence.c.evidence_id)
        ).all()
        self._attach_evidence(event, inherited_evidence, relation_kind="inherited")
        depreciation = FixedAssetDepreciation(
            org_id=request.org_id,
            asset_id=asset.id,
            activation_id=activation.id,
            event_id=event.id,
            period_start=snapshot["period_start"],
            posting_date=request.posting_date,
            sequence_no=len(snapshot["depreciations"]) + 1,
            amount_fen=calculation.depreciation_fen,
            accumulated_after_fen=calculation.closing_accumulated_depreciation_fen,
            calculation_hash=snapshot["calculation_hash"],
            accounting_rule_version=SMALL_ENTERPRISE_FIXED_ASSET_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(depreciation)
        self.session.flush()
        expense_role = {
            "management": "management_depreciation_expense",
            "sales": "sales_depreciation_expense",
            "service_delivery": "service_cost_depreciation",
        }[activation.benefit_area]
        entries = [
            Entry(account_role=expense_role, debit_fen=calculation.depreciation_fen),
            Entry(
                account_role="accumulated_depreciation",
                credit_fen=calculation.depreciation_fen,
            ),
        ]
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.posting_date,
            description=f"计提固定资产折旧 {asset.asset_code} {request.depreciation_period}",
            entries=entries,
        )
        trace.append(self._entries_trace(entries))
        trace.append({"stage": "normalized_fact_created", "depreciation_id": str(depreciation.id)})
        event.rule_trace = [dict(item) for item in trace]
        result_data = {
            **snapshot["data"],
            "calculation_hash": snapshot["calculation_hash"],
        }
        self._finalize_fixed_asset_event(event, voucher, asset.id, result_data)
        return self._posted_result(
            asset.id,
            event,
            voucher,
            data=result_data,
        )

    @staticmethod
    def _depreciation_hash_request(
        request: PreviewFixedAssetDepreciationRequest | ConfirmFixedAssetDepreciationRequest,
    ) -> dict[str, Any]:
        return {
            "org_id": str(request.org_id),
            "asset_id": str(request.asset_id),
            "depreciation_period": request.depreciation_period,
            "posting_date": request.posting_date.isoformat(),
        }

    @staticmethod
    def _parse_period(value: str) -> date:
        year, month = map(int, value.split("-"))
        return date(year, month, 1)

    @staticmethod
    def _add_months(value: date, months: int) -> date:
        ordinal = value.year * 12 + value.month - 1 + months
        return date(ordinal // 12, ordinal % 12 + 1, 1)

    def _active_depreciations(
        self, asset_id: uuid.UUID, *, lock: bool = False
    ) -> list[FixedAssetDepreciation]:
        query = (
            select(FixedAssetDepreciation)
            .join(BusinessEvent, BusinessEvent.id == FixedAssetDepreciation.event_id)
            .where(
                FixedAssetDepreciation.asset_id == asset_id,
                BusinessEvent.status == "posted",
            )
            .order_by(FixedAssetDepreciation.period_start, FixedAssetDepreciation.id)
        )
        if lock:
            query = query.with_for_update()
        return list(self.session.scalars(query).all())

    def _dispose_fixed_asset_write(self, request: DisposeFixedAssetRequest) -> FixedAssetResult:
        asset = self._get_asset(request.org_id, request.asset_id, lock=True)
        if asset is None:
            self._reject("FIXED_ASSET_NOT_FOUND")
        if self._active_disposal(asset.id) is not None:
            self._reject("FIXED_ASSET_ALREADY_DISPOSED")
        activation = self._active_activation(asset.id)
        if activation is None:
            self._reject("FIXED_ASSET_NOT_ACTIVATABLE")
        if request.disposal_date < activation.in_service_date:
            self._reject("FIXED_ASSET_NOT_ACTIVATABLE")
        depreciations = self._active_depreciations(asset.id, lock=True)
        if request.posting_date < activation.posting_date or any(
            request.posting_date < item.posting_date for item in depreciations
        ):
            self._reject("FIXED_ASSET_POSTING_DATE_OUT_OF_SEQUENCE")
        accumulated = sum(item.amount_fen for item in depreciations)
        depreciable = asset.cost_fen - activation.residual_value_fen
        disposal_period = request.disposal_date.replace(day=1)
        next_period = self._add_months(activation.in_service_date.replace(day=1), 1)
        if depreciations:
            next_period = self._add_months(depreciations[-1].period_start, 1)
        if any(item.period_start > disposal_period for item in depreciations) or (
            accumulated < depreciable and next_period <= disposal_period
        ):
            self._reject("FIXED_ASSET_DISPOSAL_WITH_UNPOSTED_DEPRECIATION")

        self._validate_fixed_asset_evidence(request.org_id, request.evidence_references)
        customer: Counterparty | None = None
        vat_tax_sales_fen = 0
        vat_fen = 0
        gross_proceeds_fen = request.gross_proceeds_fen or 0
        tax_rule: TaxRule | None = None
        if request.disposal_kind.value == "sale":
            if self._tax_obligation_date_is_locked(request.org_id, request.tax_obligation_date):
                self._reject("TAX_PERIOD_SOURCE_LOCKED")
            customer = self._resolve_fixed_asset_counterparty(
                request.org_id, request.customer, required_kind="customer"
            )
            tax_rule = self._active_used_fixed_asset_vat_rule(
                request.org_id, request.tax_obligation_date
            )
            vat = calculate_used_fixed_asset_vat(gross_proceeds_fen=gross_proceeds_fen)
            vat_tax_sales_fen = vat.tax_sales_fen
            vat_fen = vat.vat_fen

        clearance_cost_fen = request.clearance_cost_fen or 0
        expected_inflow = gross_proceeds_fen if request.settlement_method.value == "bank" else 0
        if expected_inflow or clearance_cost_fen:
            self._validate_bank_account(
                request.org_id, request.bank_account_code, request.disposal_date
            )
        book_value = asset.cost_fen - accumulated
        net_proceeds = gross_proceeds_fen - vat_fen
        clearance_debit = book_value + clearance_cost_fen
        gain_fen = max(0, net_proceeds - clearance_debit)
        loss_fen = max(0, clearance_debit - net_proceeds)
        trace = [
            {
                "stage": "facts_validated",
                "command": "finance_dispose_fixed_asset",
                "asset_id": str(asset.id),
                "activation_event_id": str(activation.event_id),
                "activation_id": str(activation.id),
                "depreciation_event_ids": [str(item.event_id) for item in depreciations],
                "cost_fen": asset.cost_fen,
                "accumulated_depreciation_fen": accumulated,
                "book_value_fen": book_value,
                "clearance_cost_fen": clearance_cost_fen,
                "gross_proceeds_fen": gross_proceeds_fen,
                "vat_tax_sales_fen": vat_tax_sales_fen,
                "vat_fen": vat_fen,
                "gain_fen": gain_fen,
                "loss_fen": loss_fen,
                "bank_account_code": request.bank_account_code,
                "evidence_ids": sorted(map(str, request.evidence_references)),
            },
            self._accounting_rule_trace(),
        ]
        if tax_rule is not None:
            trace.append(
                {
                    "stage": "tax_rule_selected",
                    "rule": tax_rule.code,
                    "version": SMALL_SCALE_USED_FIXED_ASSET_VAT_RULE_VERSION,
                    "effective_from": tax_rule.effective_from.isoformat(),
                    "effective_to": (
                        tax_rule.effective_to.isoformat() if tax_rule.effective_to else None
                    ),
                    "source_url": tax_rule.source_url,
                    "calculation": (
                        "tax_sales_fen=ROUND_HALF_UP(gross/(1+3%)); "
                        "vat_fen=ROUND_HALF_UP(tax_sales_fen*2%)"
                    ),
                }
            )
        event = self._new_fixed_asset_event(
            request,
            command="finance_dispose_fixed_asset",
            event_type="fixed_asset_disposal",
            business_date=request.disposal_date,
            posting_date=request.posting_date,
            tax_obligation_date=request.tax_obligation_date,
            trace=trace,
        )
        event.facts = {
            **event.facts,
            "asset_id": str(asset.id),
            "derived": {
                "accumulated_depreciation_fen": accumulated,
                "book_value_fen": book_value,
                "vat_tax_sales_fen": vat_tax_sales_fen,
                "taxable_gross_fen": gross_proceeds_fen,
                "net_sales_fen": vat_tax_sales_fen,
                "vat_fen": vat_fen,
                "exemption_eligible": bool(
                    request.disposal_kind.value == "sale"
                    and request.invoice_type != "special"
                    and not request.waive_exemption
                ),
                "gain_fen": gain_fen,
                "loss_fen": loss_fen,
            },
        }
        self.session.add(event)
        self.session.flush()
        self._attach_evidence(event, request.evidence_references)
        if request.bank_transaction_references:
            self._match_fixed_asset_bank_transactions(
                event,
                request.bank_transaction_references,
                bank_account_code=request.bank_account_code,
                expected_inflow_fen=expected_inflow,
                expected_outflow_fen=clearance_cost_fen,
                expected_date=request.disposal_date,
            )
        disposal = FixedAssetDisposal(
            org_id=request.org_id,
            asset_id=asset.id,
            activation_id=activation.id,
            event_id=event.id,
            disposal_date=request.disposal_date,
            posting_date=request.posting_date,
            disposal_kind=request.disposal_kind.value,
            settlement_method=request.settlement_method.value,
            customer_id=customer.id if customer else None,
            gross_proceeds_fen=gross_proceeds_fen,
            invoice_type=request.invoice_type or "none",
            waive_threshold_exemption=request.waive_exemption or False,
            vat_tax_sales_fen=vat_tax_sales_fen,
            vat_fen=vat_fen,
            clearance_cost_fen=clearance_cost_fen,
            accumulated_depreciation_fen=accumulated,
            book_value_fen=book_value,
            gain_fen=gain_fen,
            loss_fen=loss_fen,
            tax_rule_id=tax_rule.id if tax_rule else None,
            accounting_rule_version=SMALL_ENTERPRISE_FIXED_ASSET_RULE_VERSION,
            accounting_rule_source_url=ACCOUNTING_RULE_SOURCE_URL,
        )
        self.session.add(disposal)
        self.session.flush()
        entries = self._disposal_entries(
            asset=asset,
            accumulated_depreciation_fen=accumulated,
            book_value_fen=book_value,
            gross_proceeds_fen=gross_proceeds_fen,
            vat_fen=vat_fen,
            clearance_cost_fen=clearance_cost_fen,
            gain_fen=gain_fen,
            loss_fen=loss_fen,
            settlement_method=request.settlement_method.value,
            bank_account_code=request.bank_account_code,
            customer_id=customer.id if customer else None,
        )
        voucher = create_voucher(
            self.session,
            event=event,
            posting_date=request.posting_date,
            description=request.description or f"处置固定资产 {asset.asset_code}",
            entries=entries,
        )
        if request.settlement_method.value == "receivable":
            self.session.add(
                OpenItem(
                    org_id=request.org_id,
                    counterparty_id=customer.id,
                    source_event_id=event.id,
                    item_type="receivable",
                    original_amount_fen=gross_proceeds_fen,
                )
            )
        trace.append(self._entries_trace(entries))
        trace.append({"stage": "normalized_fact_created", "disposal_id": str(disposal.id)})
        event.rule_trace = [dict(item) for item in trace]
        result_data = {
            "accumulated_depreciation_fen": accumulated,
            "book_value_fen": book_value,
            "vat_tax_sales_fen": vat_tax_sales_fen,
            "vat_fen": vat_fen,
            "gain_fen": gain_fen,
            "loss_fen": loss_fen,
        }
        self._finalize_fixed_asset_event(event, voucher, asset.id, result_data)
        return self._posted_result(
            asset.id,
            event,
            voucher,
            data=result_data,
        )

    def _fixed_asset_request_hash(self, command: str, request: Any) -> str:
        return self._canonical_payload_hash(
            {"command": command, "request": request.model_dump(mode="json")}
        )

    def _fixed_asset_idempotent_event(
        self, org_id: uuid.UUID, idempotency_key: str
    ) -> BusinessEvent | None:
        return self.session.scalar(
            select(BusinessEvent).where(
                BusinessEvent.org_id == org_id,
                BusinessEvent.idempotency_key == idempotency_key,
            )
        )

    def _fixed_asset_existing_result(
        self, event: BusinessEvent, request_payload_hash: str
    ) -> FixedAssetResult:
        if event.request_payload_hash != request_payload_hash:
            return FixedAssetResult(
                status=FixedAssetResultStatus.REJECTED,
                errors=["FIXED_ASSET_IDEMPOTENCY_PAYLOAD_MISMATCH"],
            )
        voucher = event.vouchers[0] if event.vouchers else None
        asset_id = event.facts.get("asset_id")
        status = (
            FixedAssetResultStatus.REVERSED
            if event.status == "reversed"
            else FixedAssetResultStatus(event.status)
        )
        decision = event.facts.get("_decision", {})
        return FixedAssetResult(
            status=status,
            asset_id=uuid.UUID(asset_id) if asset_id else None,
            event_id=event.id,
            voucher_id=voucher.id if voucher else None,
            voucher_number=voucher.voucher_number if voucher else None,
            missing_information=decision.get("missing", []),
            errors=decision.get("errors", []),
            trace=event.rule_trace,
            calculation_hash=event.facts.get("_result_calculation_hash"),
            data={
                **event.facts.get("_result_data", {}),
                "idempotent_replay": True,
                "original_status": event.status,
            },
        )

    def _store_fixed_asset_nonposted(
        self,
        command: str,
        request: Any,
        *,
        status: FixedAssetResultStatus,
        missing: list[FixedAssetInformationRequirement] | None = None,
        errors: list[str] | None = None,
    ) -> FixedAssetResult:
        missing = missing or []
        errors = errors or []
        event_type = {
            "finance_acquire_fixed_asset": "fixed_asset_acquisition",
            "finance_activate_fixed_asset": "fixed_asset_activation",
            "finance_confirm_fixed_asset_depreciation": "fixed_asset_depreciation",
            "finance_dispose_fixed_asset": "fixed_asset_disposal",
        }[command]
        business_date, posting_date = self._request_business_and_posting_dates(request)
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
            request_payload_hash=self._fixed_asset_request_hash(command, request),
            event_type=event_type,
            status=status.value,
            description=getattr(request, "description", ""),
            facts=facts,
            business_date=business_date,
            posting_date=posting_date,
            rule_trace=[dict(item) for item in trace],
        )
        self.session.add(event)
        self.session.flush()
        self.session.add(
            AuditLog(
                org_id=request.org_id,
                event_id=event.id,
                action=f"fixed_asset_{status.value}",
                details=facts["_decision"],
            )
        )
        return FixedAssetResult(
            status=status,
            asset_id=getattr(request, "asset_id", None),
            event_id=event.id,
            missing_information=missing,
            errors=errors,
            trace=trace,
        )

    @staticmethod
    def _request_business_and_posting_dates(request: Any) -> tuple[date, date]:
        # Non-posted decisions still require database dates.  A fixed sentinel is
        # deterministic and cannot be mistaken for a caller-supplied business fact.
        posting = request.posting_date or date(1970, 1, 1)
        business = (
            getattr(request, "purchase_date", None)
            or getattr(request, "activation_date", None)
            or getattr(request, "disposal_date", None)
            or posting
        )
        return business, posting

    def _new_fixed_asset_event(
        self,
        request: Any,
        *,
        command: str,
        event_type: str,
        business_date: date,
        posting_date: date,
        trace: list[dict[str, Any]],
        payment_date: date | None = None,
        tax_obligation_date: date | None = None,
    ) -> BusinessEvent:
        facts = request.model_dump(mode="json")
        facts["_command"] = command
        return BusinessEvent(
            org_id=request.org_id,
            idempotency_key=request.idempotency_key,
            request_payload_hash=self._fixed_asset_request_hash(command, request),
            event_type=event_type,
            status="draft",
            description=getattr(request, "description", ""),
            facts=facts,
            business_date=business_date,
            payment_date=payment_date,
            tax_obligation_date=tax_obligation_date,
            posting_date=posting_date,
            rule_trace=[dict(item) for item in trace],
            rule_version=SMALL_ENTERPRISE_FIXED_ASSET_RULE_VERSION,
        )

    def _resolve_fixed_asset_counterparty(
        self, org_id: uuid.UUID, reference: Any, *, required_kind: str
    ) -> Counterparty:
        if reference.id is not None:
            row = self.session.scalar(
                select(Counterparty).where(
                    Counterparty.org_id == org_id, Counterparty.id == reference.id
                )
            )
            if row is None:
                self._reject("FIXED_ASSET_COUNTERPARTY_NOT_FOUND")
            if row.kind != required_kind:
                self._reject("FIXED_ASSET_COUNTERPARTY_KIND_MISMATCH")
            return row
        if reference.kind != required_kind:
            self._reject("FIXED_ASSET_COUNTERPARTY_KIND_MISMATCH")
        row = self.session.scalar(
            select(Counterparty).where(
                Counterparty.org_id == org_id,
                Counterparty.kind == required_kind,
                Counterparty.name == reference.name,
            )
        )
        if row is None:
            row = Counterparty(
                org_id=org_id,
                kind=required_kind,
                name=reference.name,
                external_ref=reference.external_ref,
            )
            self.session.add(row)
            self.session.flush()
        return row

    def _validate_fixed_asset_evidence(
        self, org_id: uuid.UUID, evidence_ids: list[uuid.UUID]
    ) -> None:
        if len(evidence_ids) != len(set(evidence_ids)):
            self._reject("FIXED_ASSET_DUPLICATE_EVIDENCE_REFERENCE")
        found = self.session.scalars(
            select(Evidence.id).where(Evidence.org_id == org_id, Evidence.id.in_(evidence_ids))
        ).all()
        if len(found) != len(evidence_ids):
            self._reject("FIXED_ASSET_EVIDENCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH")

    def _match_fixed_asset_bank_transactions(
        self,
        event: BusinessEvent,
        references: list[Any],
        *,
        bank_account_code: str,
        expected_inflow_fen: int,
        expected_outflow_fen: int,
        expected_date: date | None,
    ) -> None:
        try:
            rows = self._resolve_bank_transaction_references(event.org_id, references)
        except ValueError as exc:
            self._reject(str(exc))
        resolved_ids = [row.id for row in rows]
        if any(row.bank_account_code != bank_account_code for row in rows):
            self._reject("BANK_TRANSACTION_BANK_ACCOUNT_MISMATCH")
        if expected_date is not None and any(row.booking_date != expected_date for row in rows):
            self._reject("FIXED_ASSET_BANK_TRANSACTION_DATE_MISMATCH")
        inflow = sum(row.amount_fen for row in rows if row.amount_fen > 0)
        outflow = -sum(row.amount_fen for row in rows if row.amount_fen < 0)
        if inflow != expected_inflow_fen or outflow != expected_outflow_fen:
            self._reject("FIXED_ASSET_BANK_TRANSACTION_AMOUNT_MISMATCH")
        matches = self.session.scalars(
            select(BankTransactionMatch)
            .where(
                BankTransactionMatch.org_id == event.org_id,
                BankTransactionMatch.bank_transaction_id.in_(resolved_ids),
                BankTransactionMatch.invalidated_by_event_id.is_(None),
            )
            .order_by(BankTransactionMatch.bank_transaction_id)
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

    def _get_asset(
        self, org_id: uuid.UUID, asset_id: uuid.UUID | None, *, lock: bool = False
    ) -> FixedAsset | None:
        if asset_id is None:
            return None
        query = select(FixedAsset).where(FixedAsset.org_id == org_id, FixedAsset.id == asset_id)
        if lock:
            query = query.order_by(FixedAsset.id).with_for_update()
        return self.session.scalar(query)

    def _active_activation(self, asset_id: uuid.UUID) -> FixedAssetActivation | None:
        return self.session.scalar(
            select(FixedAssetActivation)
            .join(BusinessEvent, BusinessEvent.id == FixedAssetActivation.event_id)
            .where(
                FixedAssetActivation.asset_id == asset_id,
                BusinessEvent.status == "posted",
            )
        )

    def _active_disposal(self, asset_id: uuid.UUID) -> FixedAssetDisposal | None:
        return self.session.scalar(
            select(FixedAssetDisposal)
            .join(BusinessEvent, BusinessEvent.id == FixedAssetDisposal.event_id)
            .where(
                FixedAssetDisposal.asset_id == asset_id,
                BusinessEvent.status == "posted",
            )
        )

    def _active_used_fixed_asset_vat_rule(
        self, org_id: uuid.UUID, obligation_date: date
    ) -> TaxRule:
        organization = self.session.get(Organization, org_id)
        rule = self.session.scalar(
            select(TaxRule).where(
                TaxRule.code == USED_FIXED_ASSET_VAT_RULE_CODE,
                TaxRule.version == "2026.1",
                TaxRule.jurisdiction == organization.jurisdiction,
                TaxRule.effective_from <= obligation_date,
                (TaxRule.effective_to.is_(None) | (TaxRule.effective_to >= obligation_date)),
            )
        )
        if rule is None:
            self._reject("MODULE_NOT_ENABLED:used_fixed_asset_vat_rule")
        return rule

    def _asset_for_fixed_asset_event(self, event: BusinessEvent) -> FixedAsset | None:
        if event.event_type == "fixed_asset_acquisition":
            return self.session.scalar(
                select(FixedAsset).where(
                    FixedAsset.org_id == event.org_id,
                    FixedAsset.acquisition_event_id == event.id,
                )
            )
        model = {
            "fixed_asset_activation": FixedAssetActivation,
            "fixed_asset_depreciation": FixedAssetDepreciation,
            "fixed_asset_disposal": FixedAssetDisposal,
        }.get(event.event_type)
        if model is None:
            return None
        asset_id = self.session.scalar(
            select(model.asset_id).where(model.org_id == event.org_id, model.event_id == event.id)
        )
        return self._get_asset(event.org_id, asset_id)

    def fixed_asset_reversal_dependency_error(
        self, original: BusinessEvent, asset: FixedAsset
    ) -> str | None:
        """Return the stable dependency error for a locked fixed-asset source event.

        This helper is intentionally public to the service layer so the common
        reversal entry point can integrate it without duplicating the dependency
        graph when the base service is refactored.
        """

        if original.status != "posted" or original.reversed_by_event_id is not None:
            return None
        active_disposal = self._active_disposal(asset.id)
        active_activation = self._active_activation(asset.id)
        active_depreciations = self._active_depreciations(asset.id, lock=True)
        if original.event_type == "fixed_asset_disposal":
            return None
        if original.event_type == "fixed_asset_depreciation":
            source = self.session.scalar(
                select(FixedAssetDepreciation).where(
                    FixedAssetDepreciation.org_id == original.org_id,
                    FixedAssetDepreciation.event_id == original.id,
                )
            )
            if source is None:
                return "FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"
            if active_disposal is not None or any(
                item.period_start > source.period_start for item in active_depreciations
            ):
                return "FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"
            return None
        if original.event_type == "fixed_asset_activation":
            if active_disposal is not None or active_depreciations:
                return "FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"
            return None
        if original.event_type == "fixed_asset_acquisition":
            has_separate_activation = (
                active_activation is not None and active_activation.event_id != original.id
            )
            if has_separate_activation or active_disposal is not None or active_depreciations:
                return "FIXED_ASSET_OPEN_DEPENDENCIES_EXIST"
        return None

    @staticmethod
    def _disposal_entries(
        *,
        asset: FixedAsset,
        accumulated_depreciation_fen: int,
        book_value_fen: int,
        gross_proceeds_fen: int,
        vat_fen: int,
        clearance_cost_fen: int,
        gain_fen: int,
        loss_fen: int,
        settlement_method: str,
        bank_account_code: str | None,
        customer_id: uuid.UUID | None,
    ) -> list[Entry]:
        entries: list[Entry] = []
        if accumulated_depreciation_fen:
            entries.append(
                Entry(
                    account_role="accumulated_depreciation",
                    debit_fen=accumulated_depreciation_fen,
                )
            )
        if book_value_fen:
            entries.append(Entry(account_role="fixed_asset_clearance", debit_fen=book_value_fen))
        entries.append(Entry(account_role="fixed_asset_cost", credit_fen=asset.cost_fen))
        if gross_proceeds_fen:
            entries.append(
                Entry(
                    account_code=bank_account_code if settlement_method == "bank" else None,
                    account_role=(None if settlement_method == "bank" else "accounts_receivable"),
                    debit_fen=gross_proceeds_fen,
                    counterparty_id=customer_id if settlement_method == "receivable" else None,
                )
            )
            entries.append(
                Entry(
                    account_role="fixed_asset_clearance",
                    credit_fen=gross_proceeds_fen - vat_fen,
                )
            )
            if vat_fen:
                entries.append(Entry(account_role="vat_payable", credit_fen=vat_fen))
        if clearance_cost_fen:
            entries.extend(
                [
                    Entry(account_role="fixed_asset_clearance", debit_fen=clearance_cost_fen),
                    Entry(account_code=bank_account_code, credit_fen=clearance_cost_fen),
                ]
            )
        if gain_fen:
            entries.extend(
                [
                    Entry(account_role="fixed_asset_clearance", debit_fen=gain_fen),
                    Entry(account_role="fixed_asset_disposal_gain", credit_fen=gain_fen),
                ]
            )
        elif loss_fen:
            entries.extend(
                [
                    Entry(account_role="fixed_asset_disposal_loss", debit_fen=loss_fen),
                    Entry(account_role="fixed_asset_clearance", credit_fen=loss_fen),
                ]
            )
        return entries

    def _event_audit_projection(self, event_id: uuid.UUID) -> dict[str, Any] | None:
        event = self.session.get(BusinessEvent, event_id)
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
            "posting_date": event.posting_date.isoformat(),
            "rule_version": event.rule_version,
            "evidence_ids": [str(item) for item in evidence_ids],
            "voucher": (
                {
                    "id": str(voucher.id),
                    "voucher_number": voucher.voucher_number,
                    "status": voucher.status,
                    "reversal_of_voucher_id": (
                        str(voucher.reversal_of_voucher_id)
                        if voucher.reversal_of_voucher_id
                        else None
                    ),
                }
                if voucher is not None
                else None
            ),
            "trace": event.rule_trace,
        }

    @staticmethod
    def _activation_projection(activation: FixedAssetActivation | None) -> dict[str, Any] | None:
        if activation is None:
            return None
        return {
            "id": str(activation.id),
            "event_id": str(activation.event_id),
            "activation_date": activation.in_service_date.isoformat(),
            "depreciation_method": activation.depreciation_method,
            "useful_life_months": activation.useful_life_months,
            "residual_value_fen": activation.residual_value_fen,
            "benefit_area": activation.benefit_area,
        }

    @staticmethod
    def _disposal_projection(disposal: FixedAssetDisposal | None) -> dict[str, Any] | None:
        if disposal is None:
            return None
        return {
            "id": str(disposal.id),
            "event_id": str(disposal.event_id),
            "activation_id": str(disposal.activation_id),
            "disposal_date": disposal.disposal_date.isoformat(),
            "disposal_kind": disposal.disposal_kind,
            "settlement_method": disposal.settlement_method,
            "gross_proceeds_fen": disposal.gross_proceeds_fen,
            "vat_tax_sales_fen": disposal.vat_tax_sales_fen,
            "vat_fen": disposal.vat_fen,
            "clearance_cost_fen": disposal.clearance_cost_fen,
            "book_value_fen": disposal.book_value_fen,
            "gain_fen": disposal.gain_fen,
            "loss_fen": disposal.loss_fen,
        }

    @staticmethod
    def _accounting_rule_trace() -> dict[str, Any]:
        return {
            "stage": "rule_selected",
            "rule": "small_enterprise_fixed_asset_straight_line",
            "version": SMALL_ENTERPRISE_FIXED_ASSET_RULE_VERSION,
            "effective_from": "2013-01-01",
            "source_url": ACCOUNTING_RULE_SOURCE_URL,
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

    def _audit_posted(self, event: BusinessEvent, voucher: Voucher, asset_id: uuid.UUID) -> None:
        self.session.add(
            AuditLog(
                org_id=event.org_id,
                event_id=event.id,
                action="fixed_asset_event_posted",
                details={
                    "asset_id": str(asset_id),
                    "voucher_id": str(voucher.id),
                    "voucher_number": voucher.voucher_number,
                },
            )
        )

    def _finalize_fixed_asset_event(
        self,
        event: BusinessEvent,
        voucher: Voucher,
        asset_id: uuid.UUID,
        result_data: dict[str, Any],
    ) -> None:
        """Flush complete draft facts before the one-way final status transition."""

        event.facts = {
            **event.facts,
            "_result_data": result_data,
            "_result_calculation_hash": result_data.get("calculation_hash"),
        }
        self.session.flush()
        event.status = "posted"
        self._audit_posted(event, voucher, asset_id)
        self.session.flush()

    @staticmethod
    def _posted_result(
        asset_id: uuid.UUID,
        event: BusinessEvent,
        voucher: Voucher,
        *,
        data: dict[str, Any] | None = None,
    ) -> FixedAssetResult:
        result_data = data or {}
        return FixedAssetResult(
            status=FixedAssetResultStatus.POSTED,
            asset_id=asset_id,
            event_id=event.id,
            voucher_id=voucher.id,
            voucher_number=voucher.voucher_number,
            calculation_hash=result_data.get("calculation_hash"),
            trace=event.rule_trace,
            data=result_data,
        )

    @staticmethod
    def _reject(code: str) -> None:
        raise _FixedAssetDecision(FixedAssetResultStatus.REJECTED, code)
