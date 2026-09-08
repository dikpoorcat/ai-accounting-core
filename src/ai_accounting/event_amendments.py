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
    "business_event_components": ("event_id", "business_events"),
    "component_cash_flow_allocations": ("event_id", "business_events"),
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


def _dependencies(
    session: Session, source: m.BusinessEvent, graph: dict, *, deleting: bool = False
) -> list[dict]:
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
        if table.name == "business_event_dependencies" and not deleting:
            query = query.join(
                m.BusinessEvent,
                (m.BusinessEvent.org_id == table.c.org_id)
                & (m.BusinessEvent.id == table.c.child_event_id),
            ).where(m.BusinessEvent.status == "posted")
        if "org_id" in table.c:
            query = query.where(table.c.org_id == source.org_id)
        if graph.get(table.name):
            query = query.where(~_predicate(table, graph[table.name]))
        for row in session.execute(query.with_for_update()).mappings():
            blockers.append({"table": table.name, "id": str(row.get("id", ""))})
    # Cumulative tax consumers have the same employee and tax year. Apply the
    # same dependency rule as reversal, excluding every batch replaced together.
    from .service import FinanceService

    payroll = FinanceService(session)
    batch_ids = {row["id"] for row in graph.get("payroll_batches", [])}
    for batch_id in batch_ids:
        batch = session.get(m.PayrollBatch, batch_id)
        lines = list(
            session.scalars(select(m.PayrollLine).where(m.PayrollLine.payroll_batch_id == batch_id))
        )
        for dependent_id in payroll._payroll_tax_dependent_batch_ids(
            batch, lines, excluded_batch_ids=batch_ids
        ):
            blockers.append(
                {"table": "payroll_batches", "id": str(dependent_id), "reason": "later_payroll"}
            )
    return blockers


def component_fact_identity(
    session: Session, table_name: str, component_key: str
) -> uuid.UUID:
    """Allocate a planned identity, retaining the fact owned by a replaced component."""
    context = session.info.get("event_amendment")
    if context is not None:
        tables = context["original_tables"]
        component_ids = {
            row["id"]
            for row in tables.get("business_event_components", [])
            if row["key"] == component_key
        }
        candidates = [
            row
            for row in tables.get(table_name, [])
            if row.get("component_id") in component_ids
        ]
        if len(candidates) > 1:
            raise ValueError("AMENDMENT_COMPONENT_FACT_IDENTITY_AMBIGUOUS")
        if candidates:
            return candidates[0]["id"]
    return uuid.uuid4()


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
            "key",
            "kind",
            "component_key",
            "source_component_id",
            "payment_component_id",
            "component_id",
            "parent_component_id",
            "child_component_id",
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
            self.session.info.pop("preserve_accrual_batch_ids", None)

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
        if not deleting:
            from .component_schemas import RecordEventRequest

            if isinstance(request.replacement, RecordEventRequest):
                session.info["preserve_accrual_batch_ids"] = {
                    "payroll": {
                        component.batch_id
                        for component in request.replacement.components
                        if component.kind == "payroll_accrual"
                        and component.batch_id
                        in {row["id"] for row in before["payroll_batches"]}
                    },
                    "labor": {
                        component.batch_id
                        for component in request.replacement.components
                        if component.kind == "labor_remuneration_accrual"
                        and component.batch_id
                        in {row["id"] for row in before["labor_remuneration_batches"]}
                    },
                }
        if blockers := _dependencies(session, source, before, deleting=deleting):
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
        if not deleting:
            required_ids = set(
                session.scalars(
                    select(m.BusinessEventDependency.parent_component_id).where(
                        m.BusinessEventDependency.parent_event_id == source.id
                    )
                )
            )
            current_ids = set(
                session.scalars(
                    select(m.BusinessEventComponent.id).where(
                        m.BusinessEventComponent.event_id == source.id
                    )
                )
            )
            if not required_ids <= current_ids:
                raise ValueError("AMENDMENT_REFERENCED_COMPONENT_IDENTITY_REQUIRED")
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
        preserved = session.info.get("preserve_accrual_batch_ids", {})
        payroll_batch_ids = preserved.get("payroll", set())
        labor_batch_ids = preserved.get("labor", set())
        owned_payroll_batch_ids = {row["id"] for row in before["payroll_batches"]}
        for batch_id in payroll_batch_ids:
            batch = session.get(m.PayrollBatch, batch_id)
            if batch is not None:
                batch.status = "calculated"
                batch.business_event_id = None
                batch.confirmed_by = None
                batch.confirmation_note = None
                batch.confirmed_at = None
        for batch_id in labor_batch_ids:
            batch = session.get(m.LaborRemunerationBatch, batch_id)
            if batch is not None:
                batch.status = "calculated"
                batch.business_event_id = None
                batch.confirmation_note = None
                batch.confirmed_at = None
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
            if table.name in {
                "payroll_batches",
                "payroll_lines",
                "payroll_batch_evidence",
                "payroll_first_wage_tax_treatment_uses",
                "payroll_contribution_actual_uses",
            }:
                key = "id" if table.name == "payroll_batches" else "payroll_batch_id"
                rows = [row for row in rows if row.get(key) not in payroll_batch_ids]
            elif table.name in {
                "labor_remuneration_batches",
                "labor_remuneration_lines",
                "labor_remuneration_batch_evidence",
            }:
                key = "id" if table.name == "labor_remuneration_batches" else "batch_id"
                rows = [row for row in rows if row.get(key) not in labor_batch_ids]
            if table.name == "payroll_tax_state_slots":
                rows = [
                    row
                    for row in rows
                    if row["regular_batch_id"] in owned_payroll_batch_ids
                    and row["regular_batch_id"] not in payroll_batch_ids
                ]
            if rows:
                session.execute(delete(table).where(_predicate(table, rows)))

    def _repost(self, envelope: AmendEventRequest, amendment_id: uuid.UUID) -> dict[str, Any]:
        from . import borrowing_schemas as bs
        from . import intangible_asset_schemas as ins
        from . import labor_remuneration_schemas as ls
        from . import schemas as s
        from .borrowing_service import BorrowingService
        from .component_schemas import RecordEventRequest
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

        if isinstance(request, RecordEventRequest):
            # Recalculate the complete replacement against the graph after its
            # owned facts were removed, just like the individual domain previews
            # below. The preview rolls back its temporary draft and reservations;
            # keep identity reuse state separate for the one formal submission.
            context = self.session.info["event_amendment"]
            self.session.info["event_amendment"] = {
                **context,
                "identities": deepcopy(context["identities"]),
            }
            try:
                preview = FinanceService(self.session).preview_event(request)
            finally:
                self.session.info["event_amendment"] = context
            if preview.status != "calculated":
                return data(preview)
            reviewed = RecordEventRequest.model_validate(preview.data["reviewed_request"])
            return data(FinanceService(self.session).record_event(reviewed))

        direct = {
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
