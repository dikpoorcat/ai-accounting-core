"""Append-only external CIT results, reconciled to accruals and cash settlements."""

from __future__ import annotations

import calendar
import hashlib
import json
import uuid
from datetime import date
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .enterprise_income_tax_schemas import (
    ConfirmEnterpriseIncomeTaxResultRequest,
    IncomeTaxSourceAllocation,
    LinkEnterpriseIncomeTaxPaymentRequest,
    PreviewEnterpriseIncomeTaxResultRequest,
    QueryEnterpriseIncomeTaxRequest,
)
from .ledger import (
    Entry,
    assert_period_open,
    build_business_event,
    create_voucher,
    posting_period_error_code,
)
from .models import (
    AuditLog,
    BusinessEvent,
    EnterpriseIncomeTaxQuarterConfirmation,
    EnterpriseIncomeTaxResult,
    EnterpriseIncomeTaxSettlement,
    EnterpriseIncomeTaxSettlementLine,
    Organization,
)

RULE = {
    "version": "small-enterprise-cit-result-2013-v1",
    "effective_from": "2013-01-01",
    "source_url": "https://kjs.mof.gov.cn/zhengcefabu/201111/P020111118325852734144.pdf",
    "annual_refund_rule": {
        "version": "cit-annual-refund-2021-34",
        "effective_from": "2021-01-01",
        "source_url": "https://www.chinatax.gov.cn/chinatax/n810341/n810760/c5171847/content.html",
    },
}


def digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def lock_income_tax(session: Session, org_id: uuid.UUID) -> None:
    # Shared by first confirmations, revisions, cash writes and reversals.
    if session.get_bind().dialect.name == "postgresql":
        session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended('cit:' || :org_id, 0))"),
            {"org_id": str(org_id)},
        )


def event_effective(session: Session, event: BusinessEvent, as_of: date | None) -> bool:
    if as_of is not None and event.posting_date > as_of:
        return False
    if event.status == "posted":
        return True
    if event.status == "reversed" and event.reversed_by_event_id and as_of:
        reversal = session.get(BusinessEvent, event.reversed_by_event_id)
        return reversal is not None and reversal.posting_date > as_of
    return False


def confirmation_effective(
    session: Session, root: EnterpriseIncomeTaxQuarterConfirmation, as_of: date
) -> bool:
    revision = session.scalar(
        select(EnterpriseIncomeTaxResult)
        .where(
            EnterpriseIncomeTaxResult.org_id == root.org_id,
            EnterpriseIncomeTaxResult.original_confirmation_id == root.id,
            EnterpriseIncomeTaxResult.posting_date <= as_of,
        )
        .order_by(EnterpriseIncomeTaxResult.revision.desc())
        .limit(1)
    )
    event_id = revision.business_event_id if revision else root.business_event_id
    if event_id is None:
        return True
    event = session.get(BusinessEvent, event_id)
    return event is not None and event_effective(session, event, as_of)


class EnterpriseIncomeTaxService:
    def __init__(self, session: Session):
        self.session = session

    @staticmethod
    def missing(*fields: str) -> dict[str, Any]:
        return {"status": "needs_information", "missing_information": list(fields)}

    @staticmethod
    def rejected(code: str) -> dict[str, Any]:
        return {"status": "rejected", "errors": [code]}

    def _roots(self, org_id: uuid.UUID) -> list[EnterpriseIncomeTaxQuarterConfirmation]:
        return list(
            self.session.scalars(
                select(EnterpriseIncomeTaxQuarterConfirmation)
                .where(EnterpriseIncomeTaxQuarterConfirmation.org_id == org_id)
                .order_by(
                    EnterpriseIncomeTaxQuarterConfirmation.calendar_year,
                    EnterpriseIncomeTaxQuarterConfirmation.calendar_quarter,
                )
            )
        )

    def _results(self, org_id: uuid.UUID) -> list[EnterpriseIncomeTaxResult]:
        return list(
            self.session.scalars(
                select(EnterpriseIncomeTaxResult)
                .where(EnterpriseIncomeTaxResult.org_id == org_id)
                .order_by(
                    EnterpriseIncomeTaxResult.calendar_year,
                    EnterpriseIncomeTaxResult.calendar_quarter,
                    EnterpriseIncomeTaxResult.revision,
                )
            )
        )

    def query(self, request: QueryEnterpriseIncomeTaxRequest) -> dict[str, Any]:
        if self.session.get(Organization, request.org_id) is None:
            return self.rejected("ORGANIZATION_NOT_FOUND")
        states: dict[tuple[int, int], dict[str, Any]] = {}
        for root in self._roots(request.org_id):
            if request.year is not None and root.calendar_year != request.year:
                continue
            end_month = root.calendar_quarter * 3
            end = date(
                root.calendar_year, end_month, calendar.monthrange(root.calendar_year, end_month)[1]
            )
            if request.as_of and (root.posting_date or end) > request.as_of:
                continue
            states[root.calendar_year, root.calendar_quarter] = {
                "year": root.calendar_year,
                "quarter": root.calendar_quarter,
                "source_id": str(root.id),
                "original_confirmation_id": str(root.id),
                "result_id": None,
                "event_id": str(root.business_event_id) if root.business_event_id else None,
                "recognized_tax_fen": root.amount_fen * (-1 if root.treatment == "reduce" else 1),
                "contribution_fen": root.amount_fen * (-1 if root.treatment == "reduce" else 1),
                "revision": 0,
                "posting_date": (root.posting_date or end).isoformat(),
            }
        history = []
        for row in self._results(request.org_id):
            if request.year is not None and row.calendar_year != request.year:
                continue
            if request.as_of and row.posting_date > request.as_of:
                continue
            item = {
                "year": row.calendar_year,
                "quarter": row.calendar_quarter,
                "source_id": str(row.id),
                "result_id": str(row.id),
                "original_confirmation_id": str(row.original_confirmation_id)
                if row.original_confirmation_id
                else None,
                "event_id": str(row.business_event_id) if row.business_event_id else None,
                "recognized_tax_fen": row.target_tax_fen,
                "contribution_fen": row.contribution_fen,
                "revision": row.revision,
                "posting_date": row.posting_date.isoformat(),
                "declaration_date": row.declaration_date.isoformat(),
                "external_declaration_status": "confirmed",
            }
            states[row.calendar_year, row.calendar_quarter] = item
            history.append(
                item.copy()
                | {
                    "expense_adjustment_fen": row.expense_adjustment_fen,
                    "calculation_hash": row.calculation_hash,
                    "previous_result_id": str(row.previous_result_id)
                    if row.previous_result_id
                    else None,
                    "reversal_event_id": str(row.reversal_event_id)
                    if row.reversal_event_id
                    else None,
                    "declaration_reference": row.input_facts["declaration_reference"],
                    "evidence_references": row.input_facts["evidence_references"],
                    "rule": row.calculation["rule"],
                }
            )
        paid: dict[tuple[int, int], int] = {}
        cash_snapshot = []
        lines = self.session.execute(
            select(EnterpriseIncomeTaxSettlementLine, BusinessEvent)
            .join(
                EnterpriseIncomeTaxSettlement,
                EnterpriseIncomeTaxSettlement.id == EnterpriseIncomeTaxSettlementLine.settlement_id,
            )
            .join(BusinessEvent, BusinessEvent.id == EnterpriseIncomeTaxSettlement.event_id)
            .where(EnterpriseIncomeTaxSettlementLine.org_id == request.org_id)
            .order_by(EnterpriseIncomeTaxSettlementLine.id)
        ).all()
        for line, event in lines:
            if not event_effective(self.session, event, request.as_of):
                continue
            key = line.calendar_year, line.calendar_quarter
            sign = -1 if event.event_type == "enterprise_income_tax_refund" else 1
            paid[key] = paid.get(key, 0) + sign * line.amount_fen
            cash_snapshot.append(
                {
                    "id": str(line.id),
                    "event_id": str(event.id),
                    "year": key[0],
                    "quarter": key[1],
                    "amount_fen": sign * line.amount_fen,
                }
            )
        active = []
        for (year, quarter), item in sorted(states.items()):
            if quarter and (year, 0) in states:
                continue
            settled = (
                sum(v for k, v in paid.items() if k[0] == year)
                if quarter == 0
                else paid.get((year, quarter), 0)
            )
            balance = item["recognized_tax_fen"] - settled
            active.append(
                item
                | {
                    "net_paid_fen": settled,
                    "balance_fen": balance,
                    "payable_fen": max(balance, 0),
                    "refundable_fen": max(-balance, 0),
                    "settlement_status": "settled" if balance == 0 else "pending",
                }
            )
        linked = select(EnterpriseIncomeTaxSettlement.event_id).where(
            EnterpriseIncomeTaxSettlement.org_id == request.org_id
        )
        unallocated = []
        for event in self.session.scalars(
            select(BusinessEvent)
            .where(
                BusinessEvent.org_id == request.org_id,
                BusinessEvent.event_type == "tax_payment",
                BusinessEvent.id.not_in(linked),
            )
            .order_by(BusinessEvent.id)
        ):
            if event.facts.get("details", {}).get(
                "tax_type"
            ) == "enterprise_income_tax" and event_effective(self.session, event, request.as_of):
                unallocated.append(str(event.id))
        return {
            "status": "calculated",
            "data": {
                "sources": active,
                "quarter_states": [v for k, v in sorted(states.items()) if k[1]],
                "history": history,
                "cash_snapshot": cash_snapshot,
                "unallocated_payment_event_ids": unallocated,
            },
        }

    def preview(self, request: PreviewEnterpriseIncomeTaxResultRequest) -> dict[str, Any]:
        from .financial_statements import FinancialStatementService

        if self.session.get(Organization, request.org_id) is None:
            return self.rejected("ORGANIZATION_NOT_FOUND")
        if not request.evidence_references:
            return self.missing("evidence_references")
        if error := FinancialStatementService(self.session)._validate_evidence(
            request.org_id, request.evidence_references
        ):
            return self.rejected(error)
        if error := posting_period_error_code(self.session, request.org_id, request.posting_date):
            return self.rejected(error)
        month = request.quarter * 3 if request.quarter else 12
        period_end = date(request.year, month, calendar.monthrange(request.year, month)[1])
        if request.declaration_date < period_end or request.posting_date < request.declaration_date:
            return self.rejected("CIT_RESULT_DATE_ORDER_INVALID")
        state = self.query(
            QueryEnterpriseIncomeTaxRequest(org_id=request.org_id, year=request.year)
        )
        data = state["data"]
        if data["unallocated_payment_event_ids"]:
            return self.missing("link_historical_income_tax_payments") | {"data": data}
        quarters = {v["quarter"]: v for v in data["quarter_states"]}
        annual = next((v for v in data["sources"] if v["quarter"] == 0), None)
        if request.quarter and annual:
            return self.missing("annual_correction_including_changed_quarter")
        current = quarters.get(request.quarter) if request.quarter else annual
        if request.quarter and current is None:
            return self.missing("original_enterprise_income_tax_quarter_confirmation")
        if not request.quarter and set(quarters) != {1, 2, 3, 4}:
            # Never infer omitted quarters as zero, including pre-establishment quarters.
            return self.missing("quarter_confirmations_1_to_4_including_not_applicable")
        if current and request.posting_date < date.fromisoformat(current["posting_date"]):
            return self.rejected("CIT_RESULT_CANNOT_PRECEDE_PREVIOUS_REVISION")
        basis_sources = (
            quarters.values()
            if not request.quarter
            else [v for q, v in quarters.items() if q < request.quarter]
            if request.amount_basis == "year_to_date"
            else []
        )
        if any(
            date.fromisoformat(item["posting_date"]) > request.posting_date
            for item in basis_sources
        ):
            return self.missing("posting_date_not_before_referenced_quarter_results")
        expected_previous = current["result_id"] if current else None
        if (
            str(request.previous_result_id) if request.previous_result_id else None
        ) != expected_previous:
            return self.missing("current_previous_result_id") | {
                "data": {"expected": expected_previous}
            }
        if request.quarter and (
            str(request.original_confirmation_id) != current["original_confirmation_id"]
        ):
            return self.missing("original_confirmation_id")
        if not request.quarter and request.original_confirmation_id is not None:
            return self.rejected("CIT_ANNUAL_CANNOT_REFERENCE_SINGLE_QUARTER")
        for item in quarters.values():
            if item["event_id"]:
                event = self.session.get(BusinessEvent, uuid.UUID(item["event_id"]))
                amendment = self.session.info.get("event_amendment")
                replaced_result = (
                    amendment["original_tables"].get("enterprise_income_tax_results", [])
                    if amendment else []
                )
                already_reversed_predecessor = bool(
                    event is not None and replaced_result
                    and event.reversed_by_event_id == replaced_result[0]["reversal_event_id"]
                    and event.reversed_by_event_id is not None
                )
                if event is None or (event.status != "posted" and not already_reversed_predecessor):
                    return self.missing("active_quarter_income_tax_confirmation")
        quarter_total = sum(v["recognized_tax_fen"] for v in quarters.values())
        before = current["recognized_tax_fen"] if current else quarter_total
        if request.amount_basis == "adjustment_notice":
            if request.adjustment_fen is None or request.previously_recognized_fen is None:
                return self.missing("adjustment_fen", "previously_recognized_fen")
            if request.previously_recognized_fen != before:
                return self.missing("previously_recognized_fen_matches_ledger")
            target = before + request.adjustment_fen
        else:
            if request.declared_tax_fen is None:
                return self.missing("declared_tax_fen")
            target = request.declared_tax_fen
            if request.amount_basis == "year_to_date":
                if not set(range(1, request.quarter)).issubset(quarters):
                    return self.missing("previous_quarter_confirmations")
                target -= sum(
                    v["recognized_tax_fen"] for q, v in quarters.items() if q < request.quarter
                )
        if not request.quarter and target < 0:
            return self.rejected("CIT_ANNUAL_TAX_CANNOT_BE_NEGATIVE")
        if request.quarter and target < 0:
            return self.missing("negative_quarter_result_requires_prior_period_attribution")
        contribution = target if request.quarter else target - quarter_total
        original_contribution = current["contribution_fen"] if current else 0
        # Prior quarterly accruals are the annual base, not a journal to reverse.
        expense_delta = contribution - original_contribution
        cash_paid = (
            current.get("net_paid_fen", 0)
            if not request.quarter and current
            else sum(
                v["amount_fen"]
                for v in data["cash_snapshot"]
                if v["year"] == request.year
                and (not request.quarter or v["quarter"] == request.quarter)
            )
        )
        calculation = {
            "input": request.model_dump(mode="json"),
            "state": data,
            "rule": RULE,
            "previously_recognized_fen": before,
            "target_tax_fen": target,
            "contribution_fen": contribution,
            "expense_adjustment_fen": expense_delta,
            "net_paid_fen": cash_paid,
            "payable_fen": max(0, target - cash_paid),
            "refundable_fen": max(0, cash_paid - target),
            "previous_event_id": current["event_id"] if current else None,
            "revision": (current["revision"] if current else 0) + 1,
        }
        return {
            "status": "calculated",
            "calculation_hash": digest(calculation),
            "data": calculation,
        }

    def confirm(self, request: ConfirmEnterpriseIncomeTaxResultRequest) -> dict[str, Any]:
        from .financial_statements import FinancialStatementService
        from .schemas import ReverseEventRequest
        from .service import FinanceService

        lock_income_tax(self.session, request.org_id)
        payload = request.model_dump(mode="json")
        existing = self.session.scalar(
            select(EnterpriseIncomeTaxResult).where(
                EnterpriseIncomeTaxResult.org_id == request.org_id,
                EnterpriseIncomeTaxResult.idempotency_key == request.idempotency_key,
            )
        )
        if existing:
            return (
                self._result(existing, True)
                if existing.request_hash == digest(payload)
                else self.rejected("CIT_IDEMPOTENCY_MISMATCH")
            )
        preview_request = PreviewEnterpriseIncomeTaxResultRequest.model_validate(
            request.model_dump(exclude={"calculation_hash", "idempotency_key"})
        )
        preview = self.preview(preview_request)
        if preview["status"] != "calculated":
            return preview
        if preview["calculation_hash"] != request.calculation_hash:
            return self.rejected("CIT_CALCULATION_STALE")
        calc = preview["data"]
        try:
            with self.session.begin_nested():
                assert_period_open(self.session, request.org_id, request.posting_date)
                event_id = calc["previous_event_id"]
                reversal_id = None
                amendment = self.session.info.get("event_amendment")
                if amendment:
                    old_result = amendment["original_tables"]["enterprise_income_tax_results"][0]
                    if (
                        old_result["calendar_year"] != request.year
                        or old_result["calendar_quarter"] != request.quarter
                        or old_result["previous_result_id"] != request.previous_result_id
                        or old_result["original_confirmation_id"]
                        != request.original_confirmation_id
                    ):
                        raise ValueError("AMENDMENT_TAX_SOURCE_CHANGE_NOT_ALLOWED")
                    reversal_id = old_result["reversal_event_id"]
                if calc["expense_adjustment_fen"] != 0 or amendment:
                    if event_id and not amendment:
                        # This private guard only authorizes an atomic replacement; it is
                        # never a public request field or a bypass for cash reversals.
                        self.session.info["cit_replacement_event_id"] = uuid.UUID(event_id)
                        try:
                            reversed_result = FinanceService(self.session).reverse_event(
                                ReverseEventRequest(
                                    org_id=request.org_id,
                                    event_id=event_id,
                                    posting_date=request.posting_date,
                                    idempotency_key=request.idempotency_key + ":reverse",
                                    reason=request.confirmation_note,
                                )
                            )
                        finally:
                            self.session.info.pop("cit_replacement_event_id", None)
                        if reversed_result.status != "posted":
                            raise ValueError(
                                "CIT_REPLACEMENT_REVERSAL_FAILED:"
                                + ",".join(reversed_result.errors)
                            )
                        reversal_id = reversed_result.event_id
                    event_id = None
                    amount = calc["contribution_fen"]
                    if amount:
                        event = build_business_event(
                            self.session,
                            org_id=request.org_id,
                            idempotency_key=request.idempotency_key + ":assessment",
                            request_payload_hash=digest(payload),
                            event_type="enterprise_income_tax_result",
                            status="draft",
                            description=f"{request.year}年企业所得税申报结果调整",
                            facts=payload,
                            business_date=request.declaration_date,
                            tax_obligation_date=date(request.year, request.quarter * 3 or 12, 1),
                            posting_date=request.posting_date,
                            rule_trace=[RULE],
                            rule_version=RULE["version"],
                        )
                        self.session.add(event)
                        self.session.flush()
                        FinancialStatementService(self.session)._attach_evidence(
                            event, request.evidence_references
                        )
                        positive = amount > 0
                        create_voucher(
                            self.session,
                            event=event,
                            posting_date=request.posting_date,
                            description=event.description,
                            entries=[
                                Entry(
                                    account_role="enterprise_income_tax_expense",
                                    debit_fen=abs(amount) if positive else 0,
                                    credit_fen=0 if positive else abs(amount),
                                ),
                                Entry(
                                    account_role="enterprise_income_tax_payable",
                                    credit_fen=abs(amount) if positive else 0,
                                    debit_fen=0 if positive else abs(amount),
                                ),
                            ],
                        )
                        event.status = "posted"
                        self.session.flush()
                        event_id = event.id
                row = EnterpriseIncomeTaxResult(
                    org_id=request.org_id,
                    calendar_year=request.year,
                    calendar_quarter=request.quarter,
                    revision=calc["revision"],
                    previous_result_id=request.previous_result_id,
                    original_confirmation_id=request.original_confirmation_id,
                    declaration_date=request.declaration_date,
                    posting_date=request.posting_date,
                    target_tax_fen=calc["target_tax_fen"],
                    contribution_fen=calc["contribution_fen"],
                    expense_adjustment_fen=calc["expense_adjustment_fen"],
                    business_event_id=uuid.UUID(str(event_id)) if event_id else None,
                    reversal_event_id=reversal_id,
                    idempotency_key=request.idempotency_key,
                    request_hash=digest(payload),
                    calculation_hash=request.calculation_hash,
                    input_facts=payload,
                    calculation=calc,
                )
                self.session.add(row)
                self.session.flush()
                self.session.add(
                    AuditLog(
                        org_id=request.org_id,
                        event_id=row.business_event_id,
                        action="enterprise_income_tax_result_confirmed",
                        details={
                            "result_id": str(row.id),
                            "calculation_hash": row.calculation_hash,
                        },
                    )
                )
            return self._result(row)
        except (ValueError, IntegrityError) as exc:
            return self.rejected(
                str(exc) if isinstance(exc, ValueError) else "CIT_CONCURRENT_CONFLICT"
            )

    @staticmethod
    def _result(row: EnterpriseIncomeTaxResult, replay: bool = False) -> dict[str, Any]:
        return {
            "status": "posted",
            "result_id": str(row.id),
            "event_id": str(row.business_event_id) if row.business_event_id else None,
            "reversal_event_id": str(row.reversal_event_id) if row.reversal_event_id else None,
            "calculation_hash": row.calculation_hash,
            "data": row.calculation | {"idempotent_replay": replay},
        }

    def _source(self, org_id: uuid.UUID, source_id: uuid.UUID) -> tuple[int, int, bool]:
        row = self.session.get(EnterpriseIncomeTaxResult, source_id)
        if row is not None and row.org_id == org_id:
            return row.calendar_year, row.calendar_quarter, True
        root = self.session.get(EnterpriseIncomeTaxQuarterConfirmation, source_id)
        if root is not None and root.org_id == org_id:
            return root.calendar_year, root.calendar_quarter, False
        raise ValueError("CIT_SOURCE_NOT_FOUND_OR_ORGANIZATION_MISMATCH")

    def validate_payment(self, request: Any) -> None:
        data = self.query(QueryEnterpriseIncomeTaxRequest(org_id=request.org_id))["data"]
        if data["unallocated_payment_event_ids"]:
            raise ValueError("CIT_LINK_HISTORICAL_PAYMENTS_REQUIRED")
        sources = {v["source_id"]: v for v in data["sources"]}
        allocations = request.income_tax_allocations
        if not allocations or sum(a.amount_fen for a in allocations) != request.amounts.amount_fen:
            raise ValueError("CIT_ALLOCATIONS_MUST_EQUAL_PAYMENT")
        if len({a.source_id for a in allocations}) != len(allocations):
            raise ValueError("CIT_DUPLICATE_SOURCE_ALLOCATION")
        refund = request.event_type == "enterprise_income_tax_refund"
        for allocation in allocations:
            source = sources.get(str(allocation.source_id))
            if source is None:
                raise ValueError("CIT_CURRENT_SOURCE_REQUIRED")
            if source["recognized_tax_fen"] < 0:
                raise ValueError("CIT_CUMULATIVE_CREDIT_REQUIRES_ATTRIBUTION")
            if source["event_id"]:
                source_event = self.session.get(BusinessEvent, uuid.UUID(source["event_id"]))
                if source_event is None or source_event.status != "posted":
                    raise ValueError("CIT_ACTIVE_SOURCE_REQUIRED")
            if date.fromisoformat(source["posting_date"]) > request.business_dates.posting_date:
                raise ValueError("CIT_SETTLEMENT_PRECEDES_SOURCE")
            if allocation.amount_fen > source["refundable_fen" if refund else "payable_fen"]:
                raise ValueError("CIT_SETTLEMENT_EXCEEDS_SOURCE_BALANCE")

    def attach_payment(
        self,
        event: BusinessEvent,
        allocations: list[IncomeTaxSourceAllocation],
        input_facts: dict[str, Any],
        key: str,
    ) -> EnterpriseIncomeTaxSettlement:
        row = EnterpriseIncomeTaxSettlement(
            org_id=event.org_id,
            event_id=event.id,
            idempotency_key=key,
            request_hash=digest(input_facts),
            input_facts=input_facts,
        )
        self.session.add(row)
        self.session.flush()
        for a in allocations:
            year, quarter, is_result = self._source(event.org_id, a.source_id)
            self.session.add(
                EnterpriseIncomeTaxSettlementLine(
                    org_id=event.org_id,
                    settlement_id=row.id,
                    result_id=a.source_id if is_result else None,
                    original_confirmation_id=None if is_result else a.source_id,
                    calendar_year=year,
                    calendar_quarter=quarter,
                    amount_fen=a.amount_fen,
                )
            )
        self.session.flush()
        return row

    def link_payment(self, request: LinkEnterpriseIncomeTaxPaymentRequest) -> dict[str, Any]:
        from .financial_statements import FinancialStatementService

        lock_income_tax(self.session, request.org_id)
        payload = request.model_dump(mode="json")
        existing = self.session.scalar(
            select(EnterpriseIncomeTaxSettlement).where(
                EnterpriseIncomeTaxSettlement.org_id == request.org_id,
                EnterpriseIncomeTaxSettlement.idempotency_key == request.idempotency_key,
            )
        )
        if existing:
            return (
                {"status": "posted", "settlement_id": str(existing.id)}
                if existing.request_hash == digest(payload)
                else self.rejected("CIT_IDEMPOTENCY_MISMATCH")
            )
        event = self.session.get(BusinessEvent, request.event_id)
        if (
            event is None
            or event.org_id != request.org_id
            or event.status != "posted"
            or event.event_type != "tax_payment"
            or event.facts.get("details", {}).get("tax_type") != "enterprise_income_tax"
        ):
            return self.rejected("CIT_POSTED_PAYMENT_REQUIRED")
        if FinancialStatementService(self.session)._validate_evidence(
            request.org_id, request.evidence_references
        ):
            return self.missing("valid_payment_attribution_evidence")
        if sum(a.amount_fen for a in request.allocations) != event.facts["amounts"]["amount_fen"]:
            return self.rejected("CIT_ALLOCATIONS_MUST_EQUAL_PAYMENT")
        if len({a.source_id for a in request.allocations}) != len(request.allocations):
            return self.rejected("CIT_DUPLICATE_SOURCE_ALLOCATION")
        try:
            with self.session.begin_nested():
                row = self.attach_payment(
                    event, request.allocations, payload, request.idempotency_key
                )
                self.session.add(
                    AuditLog(
                        org_id=request.org_id,
                        event_id=event.id,
                        action="enterprise_income_tax_payment_linked",
                        details={"settlement_id": str(row.id)},
                    )
                )
            return {"status": "posted", "settlement_id": str(row.id)}
        except (ValueError, IntegrityError) as exc:
            return self.rejected(
                str(exc) if isinstance(exc, ValueError) else "CIT_PAYMENT_ALREADY_LINKED"
            )

    def reversal_error(self, event: BusinessEvent) -> str | None:
        if self.session.info.get("cit_replacement_event_id") == event.id:
            return None
        managed = self.session.scalar(
            select(EnterpriseIncomeTaxResult.id).where(
                EnterpriseIncomeTaxResult.org_id == event.org_id,
                (EnterpriseIncomeTaxResult.business_event_id == event.id)
                | (EnterpriseIncomeTaxResult.reversal_event_id == event.id),
            )
        )
        if managed:
            return "CIT_RESULT_REQUIRES_SPECIALIZED_CORRECTION"
        root = self.session.scalar(
            select(EnterpriseIncomeTaxQuarterConfirmation).where(
                EnterpriseIncomeTaxQuarterConfirmation.business_event_id == event.id
            )
        )
        if root:
            return "CIT_QUARTER_REQUIRES_SPECIALIZED_CORRECTION"
        settlement = self.session.scalar(
            select(EnterpriseIncomeTaxSettlement).where(
                EnterpriseIncomeTaxSettlement.event_id == event.id
            )
        )
        if settlement:
            # Cash history is a dependency of every subsequent confirmed calculation.
            for result in self._results(event.org_id):
                if any(
                    c["event_id"] == str(event.id)
                    for c in result.calculation["state"]["cash_snapshot"]
                ):
                    return "CIT_SETTLEMENT_USED_BY_LATER_RESULT"
        return None
