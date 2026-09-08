"""Atomic, audited replacement through the existing deterministic workflows.

Only the target's owned facts are rebuilt. Foreign references outside that graph
are dependencies, never a license to rewrite another business event.
"""

from __future__ import annotations

import json
import uuid
from copy import deepcopy
from typing import Any

from pydantic import BaseModel
from sqlalchemy import and_, delete, event, func, or_, select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from . import models as m
from .accounting_periods import canonical_sha256
from .enterprise_income_tax import lock_income_tax
from .event_amendment_schemas import AmendEventRequest, DeleteEventRequest
from .ledger import assert_period_open

# Each edge names ownership, not merely a foreign-key reference. Other incoming
# references (payments, later depreciation, tax snapshots, reports) block editing.
OWNERS = {
    "vouchers": ("event_id", "business_events"),
    "voucher_lines": ("voucher_id", "vouchers"),
    "event_evidence": ("event_id", "business_events"),
    "invoices": ("event_id", "business_events"),
    "open_items": ("source_event_id", "business_events"),
    "settlements": ("payment_event_id", "business_events"),
    "bank_transaction_matches": ("event_id", "business_events"),
    "business_event_dependencies": ("child_event_id", "business_events"),
    "deferred_output_vat_transfers": ("transfer_event_id", "business_events"),
    "fixed_assets": ("acquisition_event_id", "business_events"),
    "fixed_asset_activations": ("event_id", "business_events"),
    "fixed_asset_cost_sources": ("event_id", "business_events"),
    "fixed_asset_depreciations": ("event_id", "business_events"),
    "fixed_asset_depreciation_batches": ("event_id", "business_events"),
    "fixed_asset_disposals": ("event_id", "business_events"),
    "intangible_assets": ("acquisition_event_id", "business_events"),
    "intangible_asset_amortizations": ("event_id", "business_events"),
    "intangible_asset_retirements": ("event_id", "business_events"),
    "borrowings": ("drawdown_event_id", "business_events"),
    "borrowing_interest_accruals": ("event_id", "business_events"),
    "borrowing_payments": ("event_id", "business_events"),
    "payroll_batches": ("business_event_id", "business_events"),
    "payroll_lines": ("payroll_batch_id", "payroll_batches"),
    "payroll_batch_evidence": ("payroll_batch_id", "payroll_batches"),
    "payroll_event_links": ("event_id", "business_events"),
    "payroll_tax_state_slots": ("final_batch_id", "payroll_batches"),
    "annual_bonus_usages": ("payroll_batch_id", "payroll_batches"),
    "payroll_first_wage_tax_treatment_uses": ("payroll_batch_id", "payroll_batches"),
    "payroll_contribution_actual_uses": ("payroll_batch_id", "payroll_batches"),
    "payroll_withholding_entitlements": ("payroll_line_id", "payroll_lines"),
    "payroll_withholding_allocations": ("payment_event_id", "business_events"),
    "payroll_withholding_payment_allocations": ("payment_event_id", "business_events"),
    "payroll_salary_actual_deduction_allocations": ("payment_event_id", "business_events"),
    "payroll_contribution_supplements": ("event_id", "business_events"),
    "payroll_contribution_supplement_items": ("supplement_id", "payroll_contribution_supplements"),
    "labor_remuneration_batches": ("business_event_id", "business_events"),
    "labor_remuneration_lines": ("batch_id", "labor_remuneration_batches"),
    "labor_remuneration_batch_evidence": ("batch_id", "labor_remuneration_batches"),
    "labor_remuneration_event_links": ("event_id", "business_events"),
    "labor_withholding_entitlements": ("labor_line_id", "labor_remuneration_lines"),
    "labor_withholding_open_item_sources": ("payment_event_id", "business_events"),
    "labor_withholding_tax_payment_allocations": ("payment_event_id", "business_events"),
    "unified_payout_runs": ("business_event_id", "business_events"),
    "unified_payout_run_items": ("payout_run_id", "unified_payout_runs"),
    "unified_payout_run_evidence": ("payout_run_id", "unified_payout_runs"),
    "unified_payout_run_bank_transactions": ("payout_run_id", "unified_payout_runs"),
    "tax_periods": ("adjustment_event_id", "business_events"),
    "tax_period_sources": ("tax_period_id", "tax_periods"),
    "enterprise_income_tax_quarter_confirmations": ("business_event_id", "business_events"),
    "enterprise_income_tax_results": ("business_event_id", "business_events"),
    "enterprise_income_tax_settlements": ("event_id", "business_events"),
    "enterprise_income_tax_settlement_lines": (
        "settlement_id",
        "enterprise_income_tax_settlements",
    ),
}


class AmendmentRejected(ValueError):
    def __init__(self, result: dict[str, Any]):
        self.result = result
        super().__init__(str(result.get("errors", [])))


def _json(value: Any) -> Any:
    return json.loads(json.dumps(value, default=str, ensure_ascii=False))


def _predicate(table: Any, rows: list[dict[str, Any]]) -> Any:
    return or_(
        *(and_(*(column == row[column.name] for column in table.primary_key)) for row in rows)
    )


def _graph(session: Session, source: m.BusinessEvent) -> dict[str, list[dict[str, Any]]]:
    tables = m.Base.metadata.tables
    root = (
        session.execute(
            select(tables["business_events"]).where(tables["business_events"].c.id == source.id)
        )
        .mappings()
        .one()
    )
    graph = {"business_events": [dict(root)]}
    pending = dict(OWNERS)
    while pending:
        progressed = False
        for name, (column, parent) in list(pending.items()):
            if parent not in graph:
                continue
            table = tables[name]
            ids = [row["id"] for row in graph[parent]]
            graph[name] = (
                [
                    dict(row)
                    for row in session.execute(
                        select(table)
                        .where(table.c.org_id == source.org_id, table.c[column].in_(ids))
                        .order_by(*table.primary_key.columns)
                        .with_for_update()
                    ).mappings()
                ]
                if ids
                else []
            )
            del pending[name]
            progressed = True
        if not progressed:
            raise RuntimeError("invalid amendment ownership map")
    return graph


def _dependencies(session: Session, source: m.BusinessEvent, graph: dict) -> list[dict]:
    blockers = []
    for table in m.Base.metadata.sorted_tables:
        if table.name in {"audit_logs", "business_event_amendments", "bank_transactions"}:
            continue
        conditions = []
        for fk in table.foreign_keys:
            if fk.column.name != "id" or fk.column.table.name not in graph:
                continue
            rows = graph[fk.column.table.name]
            ids = [row["id"] for row in rows if "id" in row]
            if ids:
                conditions.append(fk.parent.in_(ids))
        if not conditions:
            continue
        query = select(table).where(or_(*conditions))
        if "org_id" in table.c:
            query = query.where(table.c.org_id == source.org_id)
        if graph.get(table.name):
            query = query.where(~_predicate(table, graph[table.name]))
        for row in session.execute(query.with_for_update()).mappings():
            blockers.append({"table": table.name, "id": str(row.get("id", ""))})
    # Cumulative payroll snapshots also hold earlier inputs in JSON, rather than
    # a foreign key. A later final batch must be handled first.
    for batch in graph.get("payroll_batches", []):
        if session.scalar(
            select(m.PayrollBatch.id)
            .where(
                m.PayrollBatch.org_id == source.org_id,
                m.PayrollBatch.status == "posted",
                m.PayrollBatch.payroll_period > batch["payroll_period"],
            )
            .limit(1)
        ):
            blockers.append({"table": "payroll_batches", "reason": "later_payroll"})
    return blockers


@event.listens_for(Session, "before_attach")
def _reuse_owned_identity(session: Session, instance: object) -> None:
    context = session.info.get("event_amendment")
    if context is None or not isinstance(instance, m.Base):
        return
    table = instance.__table__
    if table.name not in OWNERS or "id" not in table.c or getattr(instance, "id", None):
        return
    candidates = context["identities"].get(table.name, [])
    # Stable dimensions preserve employee, asset and open-item identities even
    # when the caller changes the order of a multi-line replacement.
    dimensions = [
        key
        for key in (
            "employee_id",
            "labor_person_id",
            "counterparty_id",
            "asset_id",
            "line_number",
            "payable_category",
            "payable_agency_code",
            "pass_through_key",
            "insurance_kind",
            "source_key",
        )
        if key in table.c
    ]
    chosen = next(
        (
            row
            for row in candidates
            if all(row[key] == getattr(instance, key, None) for key in dimensions)
        ),
        None,
    )
    if chosen is None and not dimensions and candidates:
        chosen = candidates[0]
    if chosen is not None:
        instance.id = chosen["id"]
        candidates.remove(chosen)


class EventAmendmentService:
    def __init__(self, session: Session):
        self.session = session

    def amend(self, request: AmendEventRequest | DeleteEventRequest) -> dict[str, Any]:
        try:
            with self.session.begin_nested():
                return self._write(request)
        except AmendmentRejected as exc:
            return exc.result
        except ValueError as exc:
            return {"status": "rejected", "errors": [str(exc)]}
        except DBAPIError:
            return {"status": "rejected", "errors": ["AMENDMENT_DATABASE_CONFLICT"]}
        finally:
            self.session.info.pop("event_amendment", None)

    def _write(self, request: AmendEventRequest | DeleteEventRequest) -> dict[str, Any]:
        session = self.session
        lock_income_tax(session, request.org_id)
        deleting = isinstance(request, DeleteEventRequest)
        request_hash = canonical_sha256(request.model_dump(mode="json"))
        existing = session.scalar(
            select(m.BusinessEventAmendment).where(
                m.BusinessEventAmendment.org_id == request.org_id,
                m.BusinessEventAmendment.idempotency_key == request.idempotency_key,
            )
        )
        if existing:
            if existing.request_hash != request_hash:
                raise ValueError("AMENDMENT_IDEMPOTENCY_PAYLOAD_MISMATCH")
            return deepcopy(existing.result) | {"idempotent_replay": True}
        source = session.scalar(
            select(m.BusinessEvent)
            .where(
                m.BusinessEvent.org_id == request.org_id,
                m.BusinessEvent.id == request.event_id,
            )
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if source is None:
            raise ValueError("EVENT_NOT_FOUND")
        assert_period_open(session, source.org_id, source.posting_date)
        if (
            source.status != "posted"
            or source.reversed_by_event_id
            or source.event_type == "reversal"
        ):
            raise ValueError("EVENT_IS_NOT_AMENDABLE")
        if canonical_sha256(source.facts) != request.expected_facts_hash:
            raise ValueError("AMENDMENT_FACTS_STALE")
        before = _graph(session, source)
        if deleting and any(
            row["reversal_event_id"] is not None for row in before["enterprise_income_tax_results"]
        ):
            raise AmendmentRejected(
                {
                    "status": "rejected",
                    "errors": ["DELETION_LINKED_REVERSAL_EXISTS"],
                    "blocking_records": [
                        {"table": "business_events", "id": str(row["reversal_event_id"])}
                        for row in before["enterprise_income_tax_results"]
                        if row["reversal_event_id"]
                    ],
                }
            )
        if blockers := _dependencies(session, source, before):
            raise AmendmentRejected(
                {
                    "status": "rejected",
                    "errors": ["AMENDMENT_DEPENDENT_FACTS_EXIST"],
                    "blocking_records": blockers,
                }
            )
        voucher = session.scalar(select(m.Voucher).where(m.Voucher.event_id == source.id))
        if voucher is None or voucher.reversal_of_voucher_id is not None:
            raise ValueError("EVENT_IS_NOT_AMENDABLE")
        revision = (
            session.scalar(
                select(func.max(m.BusinessEventAmendment.revision)).where(
                    m.BusinessEventAmendment.org_id == source.org_id,
                    m.BusinessEventAmendment.event_id == source.id,
                )
            )
            or 0
        ) + 1
        amendment = m.BusinessEventAmendment(
            org_id=source.org_id,
            event_id=source.id,
            revision=revision,
            operation="delete" if deleting else "amend",
            idempotency_key=request.idempotency_key,
            request_hash=request_hash,
            reason=request.reason,
            before_state={"tables": _json(before)},
            execution_attribution_id=session.info.get(m.EXECUTION_ATTRIBUTION_SESSION_KEY),
        )
        session.add(amendment)
        session.flush()
        source.status = "draft"
        session.flush()
        voucher.status = "draft"
        session.flush()
        self._remove_owned_facts(source, voucher, before)
        session.expire(voucher, ["lines"])
        session.expire(source, ["evidence"])
        session.info["event_amendment"] = {
            "event": source,
            "voucher": voucher,
            "identities": deepcopy(before),
            "original_tables": before,
        }
        if deleting:
            session.delete(voucher)
            source.status = "deleted"
            session.flush()
            result = {"status": "deleted"}
        else:
            result = self._repost(request, amendment.id)
        session.flush()
        if result.get("status") != ("deleted" if deleting else "posted"):
            raise AmendmentRejected(result)
        if not deleting and (source.status != "posted" or voucher.status != "posted"):
            raise ValueError("AMENDMENT_REQUIRES_POSTED_REPLACEMENT")
        if source.posting_date.strftime("%Y-%m") != before["business_events"][0][
            "posting_date"
        ].strftime("%Y-%m"):
            raise ValueError("AMENDMENT_MUST_REMAIN_IN_ORIGINAL_MONTH")
        assert_period_open(session, source.org_id, source.posting_date)
        result |= {
            "event_id": str(source.id),
            "voucher_id": str(voucher.id),
            "voucher_number": voucher.voucher_number,
            "amendment_id": str(amendment.id),
            "revision": revision,
            "facts_hash": canonical_sha256(source.facts),
        }
        amendment.after_state = {"tables": _json(_graph(session, source))}
        amendment.result = result
        session.add(
            m.AuditLog(
                org_id=source.org_id,
                event_id=source.id,
                action="event_deleted" if deleting else "event_amended",
                details={
                    "amendment_id": str(amendment.id),
                    "revision": revision,
                    "reason": request.reason,
                },
            )
        )
        session.flush()
        session.expire(source, ["vouchers", "evidence"])
        if not deleting:
            session.expire(voucher, ["lines"])
        return result

    def _remove_owned_facts(
        self, source: m.BusinessEvent, voucher: m.Voucher, before: dict
    ) -> None:
        session = self.session
        # Restore balances consumed by the old payment before deriving the new
        # allocation; all changes remain inside the amendment savepoint.
        for row in before["settlements"]:
            if row["reversed"]:
                raise ValueError("AMENDMENT_REVERSED_SETTLEMENT")
            item = session.scalar(
                select(m.OpenItem)
                .where(
                    m.OpenItem.org_id == source.org_id,
                    m.OpenItem.id == row["open_item_id"],
                )
                .with_for_update()
            )
            item.settled_amount_fen -= row["amount_fen"]
            item.status = "open" if item.settled_amount_fen == 0 else "partial"
        for row in before["bank_transaction_matches"]:
            if row["invalidated_by_event_id"] is not None:
                raise ValueError("AMENDMENT_HISTORICAL_BANK_MATCH")
            transaction = session.get(m.BankTransaction, row["bank_transaction_id"])
            transaction.matched_event_id = None
        for row in before["payroll_tax_state_slots"]:
            if row["regular_batch_id"] != row["final_batch_id"]:
                slot = session.get(m.PayrollTaxStateSlot, row["id"])
                slot.final_batch_id = slot.regular_batch_id
        session.flush()
        for obj in list(session.identity_map.values()):
            name = obj.__table__.name
            if obj not in (source, voucher) and name in OWNERS:
                if any(
                    all(getattr(obj, c.name) == row[c.name] for c in obj.__table__.primary_key)
                    for row in before.get(name, [])
                ):
                    session.expunge(obj)
        for table in reversed(m.Base.metadata.sorted_tables):
            if table.name in {"business_events", "vouchers"} or not before.get(table.name):
                continue
            rows = before[table.name]
            if table.name == "payroll_tax_state_slots":
                rows = [row for row in rows if row["regular_batch_id"] == row["final_batch_id"]]
            if rows:
                session.execute(delete(table).where(_predicate(table, rows)))

    def _repost(self, envelope: AmendEventRequest, amendment_id: uuid.UUID) -> dict[str, Any]:
        from . import borrowing_schemas as bs
        from . import intangible_asset_schemas as ins
        from . import labor_remuneration_schemas as ls
        from . import schemas as s
        from .borrowing_service import BorrowingService
        from .enterprise_income_tax import EnterpriseIncomeTaxService
        from .enterprise_income_tax_schemas import (
            ConfirmEnterpriseIncomeTaxResultRequest,
            PreviewEnterpriseIncomeTaxResultRequest,
        )
        from .financial_statement_schemas import ConfirmEnterpriseIncomeTaxQuarterRequest
        from .financial_statements import FinancialStatementService
        from .fixed_asset_service import FixedAssetService
        from .intangible_asset_service import IntangibleAssetService
        from .labor_remuneration_service import LaborRemunerationService
        from .service import FinanceService

        request = envelope.replacement
        key = f"amend:{amendment_id}"
        if "idempotency_key" in type(request).model_fields:
            request = request.model_copy(update={"idempotency_key": key})

        def data(result: Any) -> dict:
            return result.model_dump(mode="json") if isinstance(result, BaseModel) else result

        direct = {
            s.RecordEventRequest: (FinanceService, "record_event"),
            s.RecordPayrollContributionSupplementRequest: (
                FinanceService,
                "record_payroll_contribution_supplement",
            ),
            s.AcquireFixedAssetRequest: (FixedAssetService, "acquire_fixed_asset"),
            s.ActivateFixedAssetRequest: (FixedAssetService, "activate_fixed_asset"),
            s.DisposeFixedAssetRequest: (FixedAssetService, "dispose_fixed_asset"),
            ins.AcquireIntangibleAssetRequest: (IntangibleAssetService, "acquire_intangible_asset"),
            ins.RetireIntangibleAssetRequest: (IntangibleAssetService, "retire_intangible_asset"),
            bs.DrawBorrowingRequest: (BorrowingService, "draw_borrowing"),
            bs.PayBorrowingInterestRequest: (BorrowingService, "pay_borrowing_interest"),
            bs.RepayBorrowingPrincipalRequest: (BorrowingService, "repay_borrowing_principal"),
            ls.PayLaborWithholdingTaxRequest: (LaborRemunerationService, "pay_withholding_tax"),
            ConfirmEnterpriseIncomeTaxQuarterRequest: (
                FinancialStatementService,
                "confirm_enterprise_income_tax",
            ),
        }
        if type(request) in direct:
            cls, method = direct[type(request)]
            return data(getattr(cls(self.session), method)(request))
        previews = {
            s.PreviewPayrollRequest: (
                FinanceService,
                "preview_payroll",
                "confirm_payroll",
                s.ConfirmPayrollRequest,
                "batch_id",
            ),
            ls.PreviewLaborRemunerationBatchRequest: (
                LaborRemunerationService,
                "preview_batch",
                "confirm_batch",
                ls.ConfirmLaborRemunerationBatchRequest,
                "batch_id",
            ),
            ls.PreviewUnifiedPayoutRunRequest: (
                LaborRemunerationService,
                "preview_payout",
                "confirm_payout",
                ls.ConfirmUnifiedPayoutRunRequest,
                "payout_run_id",
            ),
            s.PreviewFixedAssetDepreciationRequest: (
                FixedAssetService,
                "preview_fixed_asset_depreciation",
                "confirm_fixed_asset_depreciation",
                s.ConfirmFixedAssetDepreciationRequest,
                None,
            ),
            s.PreviewFixedAssetDepreciationBatchRequest: (
                FixedAssetService,
                "preview_fixed_asset_depreciation_batch",
                "confirm_fixed_asset_depreciation_batch",
                s.ConfirmFixedAssetDepreciationBatchRequest,
                None,
            ),
            ins.PreviewIntangibleAssetAmortizationRequest: (
                IntangibleAssetService,
                "preview_intangible_asset_amortization",
                "confirm_intangible_asset_amortization",
                ins.ConfirmIntangibleAssetAmortizationRequest,
                None,
            ),
            bs.PreviewBorrowingInterestRequest: (
                BorrowingService,
                "preview_borrowing_interest",
                "confirm_borrowing_interest",
                bs.ConfirmBorrowingInterestRequest,
                None,
            ),
            s.TaxPeriodPreviewRequest: (
                FinanceService,
                "preview_tax_period",
                "confirm_tax_period",
                s.TaxPeriodConfirmRequest,
                None,
            ),
            PreviewEnterpriseIncomeTaxResultRequest: (
                EnterpriseIncomeTaxService,
                "preview",
                "confirm",
                ConfirmEnterpriseIncomeTaxResultRequest,
                None,
            ),
        }
        cls, preview_method, confirm_method, schema, identity = previews[type(request)]
        service = cls(self.session)
        preview = data(getattr(service, preview_method)(request))
        if not preview.get("calculation_hash") or preview.get("status") in {
            "rejected",
            "needs_information",
        }:
            return preview
        payload = (
            {"org_id": request.org_id, identity: preview[identity]}
            if identity
            else request.model_dump()
        )
        payload.update(
            calculation_hash=preview["calculation_hash"], idempotency_key=key + ":confirm"
        )
        if "confirmation_note" in schema.model_fields and "confirmation_note" not in payload:
            payload["confirmation_note"] = envelope.reason
        return data(getattr(service, confirm_method)(schema.model_validate(payload)))
