"""Typed source corrections and dependency-ordered open-period amendments."""

from __future__ import annotations

import uuid
from copy import deepcopy

from sqlalchemy import event, select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from . import models as m
from .accounting_periods import canonical_sha256
from .component_schemas import RecordEventRequest
from .enterprise_income_tax import lock_income_tax
from .event_amendment_schemas import (
    AmendEventRequest,
    ConfirmCorrectionRequest,
    PreviewCorrectionRequest,
)
from .event_amendments import (
    OWNERS,
    AmendmentRejected,
    EventAmendmentService,
    _dependencies,
    _graph,
    _json,
    database_failure,
)
from .ledger import assert_period_open
from .schemas import PreviewPayrollRequest, RegisterPayrollContributionActualRequest


def accounting_projection(value):
    """Correction hashes exclude management notes, never amounts or source versions."""
    if hasattr(value, "model_dump"):
        value = value.model_dump(mode="json")
    if isinstance(value, dict):
        return {
            key: accounting_projection(item)
            for key, item in value.items()
            if key
            not in {
                "metadata",
                "reason",
                "description",
                "declaration_date",
                "reason_code",
                "reason_description",
                "confirmation_description",
                "confirmation_note",
                "tax_reporting_difference_reason",
            }
        }
    if isinstance(value, list):
        return [accounting_projection(item) for item in value]
    return value


def source_batches(session, change, *, statuses=("posted",)):
    """Include policy-only consumers, which have no actual/treatment use edge yet."""
    from .service import FinanceService

    rows = session.execute(
        select(m.PayrollBatch, m.PayrollLine)
        .join(m.PayrollLine, m.PayrollLine.payroll_batch_id == m.PayrollBatch.id)
        .where(
            m.PayrollBatch.org_id == change.org_id,
            m.PayrollLine.employee_id == change.employee_id,
            m.PayrollBatch.status.in_(statuses),
            m.PayrollBatch.reversal_of_batch_id.is_(None),
        )
        .order_by(m.PayrollBatch.id)
        .with_for_update()
    ).all()
    common = FinanceService(session)
    batches = {}
    for batch, line in rows:
        if isinstance(change, RegisterPayrollContributionActualRequest):
            affected = (
                batch.batch_kind == "regular" and batch.payroll_period == change.contribution_period
            )
        else:
            affected = (
                common._line_uses_cumulative_tax_state(batch, line)
                and common._batch_tax_period(batch).year == change.tax_year
            )
        if affected:
            batches[batch.id] = batch
    return list(batches.values())


def source_change_gate(session, change):
    lock_income_tax(session, change.org_id)
    batches = source_batches(session, change)
    if not batches:
        return None
    return {
        "status": "rejected",
        "errors": ["SOURCE_CHANGE_REQUIRES_CORRECTION"],
        "blocking_payroll_batch_ids": sorted(str(batch.id) for batch in batches),
        "data": {
            "failure_kind": "linked_correction_required",
            "next_action": "finance_preview_correction",
        },
    }


def _owner_event(session, table_name, key):
    """Follow declared ownership only; arbitrary foreign keys never grant ownership."""
    while table_name in OWNERS:
        table = m.Base.metadata.tables[table_name]
        row = (
            session.execute(
                select(table).where(
                    *(
                        table.c[name]
                        == (uuid.UUID(value) if isinstance(table.c[name].type, m.Uuid) else value)
                        for name, value in key.items()
                    )
                )
            )
            .mappings()
            .first()
        )
        if row is None:
            return None
        column, parent = OWNERS[table_name]
        if row[column] is None:
            return None
        key, table_name = {"id": str(row[column])}, parent
    return uuid.UUID(key["id"]) if table_name == "business_events" else None


def discover_scope(session, org_id, roots):
    graphs, edges, blockers = {}, set(), []
    pending = list(sorted(roots, key=str))
    while pending:
        event_id = pending.pop(0)
        if event_id in graphs:
            continue
        source = session.scalar(
            select(m.BusinessEvent)
            .where(m.BusinessEvent.org_id == org_id, m.BusinessEvent.id == event_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if source is None or source.status != "posted" or source.reversed_by_event_id:
            raise ValueError("CORRECTION_SOURCE_NOT_ACTIVE")
        graph = _graph(session, source)
        graphs[event_id] = graph
        for dependency in _dependencies(session, source, graph):
            key = dependency.get("key") or {"id": dependency["id"]}
            child_id = _owner_event(session, dependency["table"], key)
            child = session.get(m.BusinessEvent, child_id) if child_id else None
            if child is not None and child.org_id == org_id and child.status == "posted":
                if child_id != event_id:
                    edges.add((event_id, child_id))
                    pending.append(child_id)
            else:
                blockers.append(dependency)
    ordered = []
    remaining = set(graphs)
    while remaining:
        ready = sorted(
            (
                key
                for key in remaining
                if not any(parent in remaining and child == key for parent, child in edges)
            ),
            key=lambda key: (graphs[key]["business_events"][0]["posting_date"], str(key)),
        )
        if not ready:
            raise ValueError("CORRECTION_DEPENDENCY_CYCLE")
        ordered.extend(ready)
        remaining.difference_update(ready)
    return graphs, edges, ordered, blockers


def correction_route(session, org_id, event_id):
    graphs, _, _, blockers = discover_scope(session, org_id, {event_id})
    closed = []
    if session.get_bind().dialect.name == "postgresql":
        closed = [
            {"event_id": str(row.event_id), "period": row.period_month}
            for row in session.execute(
                text("SELECT * FROM finance_correction_closed_dependencies(:org_id,:event_id)"),
                {"org_id": org_id, "event_id": event_id},
            )
        ]
        return {
            "route": "linked_reversal" if closed else "amend",
            "closed_records": closed,
            "blocking_records": blockers,
        }
    for key, graph in graphs.items():
        day = graph["business_events"][0]["posting_date"]
        period = session.scalar(
            select(m.AccountingPeriod).where(
                m.AccountingPeriod.org_id == org_id,
                m.AccountingPeriod.calendar_year == day.year,
                m.AccountingPeriod.calendar_month == day.month,
            )
        )
        if period is not None and period.status == "closed":
            closed.append({"event_id": str(key), "period": day.strftime("%Y-%m")})
    return {
        "route": "linked_reversal" if closed else "amend",
        "closed_records": closed,
        "blocking_records": blockers,
    }


@event.listens_for(Session, "before_attach")
def _source_identity(session, instance):
    seed = session.info.get("correction_source_identity")
    if seed is None or getattr(instance, "id", None) is not None:
        return
    if isinstance(instance, m.PayrollContributionActualSet):
        suffix = "set"
    elif isinstance(instance, m.PayrollContributionActualItem):
        suffix = f"item:{instance.contribution_group}:{instance.insurance_kind}"
    elif isinstance(instance, m.PayrollFirstWageTaxTreatment):
        suffix = "treatment"
    else:
        return
    instance.id = uuid.uuid5(seed, suffix)


def rebuild_payroll_components(session, state, envelope):
    """Recalculate stored payroll inputs before compiling their containing event."""
    from .service import FinanceService

    replacement = envelope.replacement
    if not isinstance(replacement, RecordEventRequest):
        return envelope
    originals = {row["id"]: row for row in state["before"]["payroll_batches"]}
    original_components = {
        row["key"]: row["facts"] for row in state["before"]["business_event_components"]
    }

    def original_batch(component):
        facts = original_components.get(component.key, {})
        batch_id = facts.get("batch_id")
        return originals.get(uuid.UUID(str(batch_id))) if batch_id else None

    components = list(replacement.components)
    payroll = sorted(
        (c for c in components if c.kind == "payroll_accrual"),
        key=lambda c: (original_batch(c) or {}).get("batch_kind", "") != "regular",
    )
    for component in payroll:
        batch = original_batch(component)
        if batch is None:
            continue
        calculation_input = batch["calculation_input"]
        if component.batch_id != batch["id"]:
            revised = session.get(m.PayrollBatch, component.batch_id)
            if (
                revised is None
                or revised.org_id != envelope.org_id
                or revised.status != "calculated"
                or revised.calculation_hash != component.calculation_hash
            ):
                raise ValueError("CORRECTION_REPLACEMENT_PAYROLL_PREVIEW_STALE")
            calculation_input = revised.calculation_input
        request = PreviewPayrollRequest.model_validate(calculation_input["request"])
        request = request.model_copy(
            update={
                "idempotency_key": f"correction-payroll:{state['identity_namespace']}:{batch['id']}"
            }
        )
        context = session.info["event_amendment"]
        context["rebuilding_payroll_id"] = batch["id"]
        try:
            result = FinanceService(session).preview_payroll(request)
        finally:
            context.pop("rebuilding_payroll_id", None)
        if result.status != "calculated":
            raise AmendmentRejected(result.model_dump(mode="json"))
        components[components.index(component)] = component.model_copy(
            update={"batch_id": result.batch_id, "calculation_hash": result.calculation_hash}
        )
    return envelope.model_copy(
        update={"replacement": replacement.model_copy(update={"components": components})}
    )


def _financial_snapshot(graph):
    """Stable review dimensions; row and audit creation times are not accounting facts."""
    result = {}
    dimensions = {
        "voucher_lines": ("line_number", "account_id", "debit_fen", "credit_fen"),
        "open_items": ("id", "account_id", "original_amount_fen", "settled_amount_fen", "status"),
        "settlements": ("open_item_id", "amount_fen"),
        "component_cash_flow_allocations": ("component_key", "category", "amount_fen"),
        "payroll_lines": (
            "employee_id",
            "gross_salary_fen",
            "net_salary_fen",
            "individual_income_tax_fen",
            "employee_social_insurance_fen",
            "employer_social_insurance_fen",
            "employee_housing_fund_fen",
            "employer_housing_fund_fen",
        ),
        "bank_transaction_matches": ("bank_transaction_id", "amount_fen"),
    }
    for table, keys in dimensions.items():
        result[table] = sorted(
            ({key: row[key] for key in keys if key in row} for row in graph[table]),
            key=lambda row: canonical_sha256(_json(row)),
        )
    return _json(result)


def _stale_plan():
    return {
        "status": "rejected",
        "errors": ["CORRECTION_PLAN_STALE"],
        "data": {"failure_kind": "version_conflict", "next_action": "finance_preview_correction"},
    }


class CorrectionService:
    def __init__(self, session):
        self.session = session

    def preview(self, request: PreviewCorrectionRequest):
        transaction = self.session.begin_nested()
        try:
            return self._execute(request, preview=True)
        except AmendmentRejected as exc:
            return exc.result
        except DBAPIError as exc:
            return database_failure(exc)
        except ValueError as exc:
            return {"status": "rejected", "errors": [str(exc)]}
        finally:
            if transaction.is_active:
                transaction.rollback()
            self._clear()

    def confirm(self, request: ConfirmCorrectionRequest):
        try:
            with self.session.begin_nested():
                return self._execute(request, preview=False)
        except AmendmentRejected as exc:
            return exc.result
        except DBAPIError as exc:
            return database_failure(exc)
        except ValueError as exc:
            if str(exc) in {
                "AMENDMENT_FACTS_STALE",
                "CORRECTION_REPLACEMENT_PAYROLL_PREVIEW_STALE",
            }:
                return _stale_plan()
            return {"status": "rejected", "errors": [str(exc)]}
        finally:
            self._clear()

    def _clear(self):
        for key in (
            "event_amendment",
            "preserve_accrual_batch_ids",
            "correction_source_identity",
            "correction_payroll_batch_ids",
        ):
            self.session.info.pop(key, None)
        # Owned objects are expunged and recreated with their original IDs.
        # A rolled-back preview must not leave a recreated object's temporary
        # balance in the identity map of a long-lived operator session.
        self.session.expire_all()

    def _execute(self, request, *, preview):
        from .service import FinanceService

        session = self.session
        lock_income_tax(session, request.org_id)
        payload = accounting_projection(
            PreviewCorrectionRequest.model_validate(
                request.model_dump(exclude={"idempotency_key", "calculation_hash"})
            )
        )
        request_hash = canonical_sha256(payload)
        if not preview:
            existing = session.scalar(
                select(m.BusinessCorrection).where(
                    m.BusinessCorrection.org_id == request.org_id,
                    m.BusinessCorrection.idempotency_key == request.idempotency_key,
                )
            )
            if existing:
                if (
                    existing.request_hash != request_hash
                    or existing.calculation_hash != request.calculation_hash
                ):
                    raise ValueError("CORRECTION_IDEMPOTENCY_PAYLOAD_MISMATCH")
                return deepcopy(existing.result) | {"idempotent_replay": True}
        roots = {entry.event_id for entry in request.event_replacements}
        source_snapshots = []
        for change in request.source_changes:
            roots.update(batch.business_event_id for batch in source_batches(session, change))
            common = FinanceService(session)
            if isinstance(change, RegisterPayrollContributionActualRequest):
                rows = common._active_contribution_actual_items(
                    change.org_id, change.employee_id, change.contribution_period
                )
            else:
                current = common._active_first_wage_tax_treatment(
                    change.org_id, change.employee_id, change.tax_year
                )
                rows = [current] if current else []
            source_snapshots.append(
                _json(
                    [
                        {column.name: getattr(row, column.name) for column in row.__table__.columns}
                        for row in rows
                    ]
                )
            )
        graphs, edges, ordered, blockers = discover_scope(session, request.org_id, roots)
        closed = []
        for key in ordered:
            source = graphs[key]["business_events"][0]
            try:
                assert_period_open(session, request.org_id, source["posting_date"])
            except ValueError as exc:
                if str(exc) != "ACCOUNTING_PERIOD_CLOSED":
                    raise
                closed.append(
                    {
                        "event_id": str(key),
                        "period": source["posting_date"].strftime("%Y-%m"),
                        "code": str(exc),
                    }
                )
        if closed or blockers:
            raise AmendmentRejected(
                {
                    "status": "rejected",
                    "errors": [
                        "CORRECTION_CLOSED_DEPENDENCY"
                        if closed
                        else "CORRECTION_UNSUPPORTED_DEPENDENCY"
                    ],
                    "blocking_records": closed + blockers,
                    "data": {
                        "route": "linked_reversal" if closed else "amend",
                        "next_action": "inspect_closed_correction"
                        if closed
                        else "inspect_dependency",
                    },
                }
            )
        overrides = {entry.event_id: entry for entry in request.event_replacements}
        envelopes = {}
        for key in ordered:
            source = graphs[key]["business_events"][0]
            entry = overrides.get(key)
            if entry and canonical_sha256(source["facts"]) != entry.expected_facts_hash:
                raise ValueError("AMENDMENT_FACTS_STALE")
            replacement = (
                entry.replacement if entry else RecordEventRequest.model_validate(source["facts"])
            )
            # Rebuild derived allocations only from explicit replacement facts;
            # an automatic consumer retains every recorded monetary input.
            envelopes[key] = AmendEventRequest(
                org_id=request.org_id,
                event_id=key,
                idempotency_key=f"correction:{uuid.uuid4()}",
                expected_facts_hash=canonical_sha256(source["facts"]),
                replacement=replacement,
                reason=request.reason,
            )
        merged = {table: [] for table in ("business_events", *OWNERS)}
        for graph in graphs.values():
            for table, rows in graph.items():
                merged[table].extend(rows)
        correction = m.BusinessCorrection(
            org_id=request.org_id,
            idempotency_key=f"preview:{uuid.uuid4()}" if preview else request.idempotency_key,
            request_hash=request_hash,
            calculation_hash="0" * 64 if preview else request.calculation_hash,
            reason=request.reason,
            before_state={
                "event_ids": [str(key) for key in ordered],
                "tables": _json(merged),
                "source_changes": payload["source_changes"],
                "sources": source_snapshots,
            },
            execution_attribution_id=session.info.get(m.EXECUTION_ATTRIBUTION_SESSION_KEY),
        )
        session.add(correction)
        session.flush()
        executor = EventAmendmentService(session)
        states = {}
        for key in ordered:
            states[key] = executor.prepare(
                envelopes[key], correction_id=correction.id, check_dependencies=False
            )
            # Only payroll calculations consume these source changes. Retain
            # unrelated confirmed labor components in a mixed voucher.
            states[key]["preserved"] = {"labor": states[key]["preserved"].get("labor", set())}
            states[key]["rebuild_payroll"] = True
            states[key]["identity_namespace"] = uuid.uuid5(key, request_hash)
        session.info["correction_payroll_batch_ids"] = {
            row["id"] for graph in graphs.values() for row in graph["payroll_batches"]
        }
        for key in reversed(ordered):
            executor.unpost(states[key])
        source_results = []
        for change in request.source_changes:
            session.info["correction_source_identity"] = uuid.uuid5(
                request.org_id, canonical_sha256(accounting_projection(change))
            )
            method = (
                "register_payroll_contribution_actual"
                if isinstance(change, RegisterPayrollContributionActualRequest)
                else "register_payroll_first_wage_tax_treatment"
            )
            result = getattr(FinanceService(session), method)(change)
            if result.get("status") != "registered":
                if not preview and set(result.get("errors", [])) & {
                    "INVALID_FIRST_WAGE_SUPERSEDES",
                    "FIRST_WAGE_TREATMENT_CORRECTION_REQUIRES_SUPERSEDES",
                    "INVALID_CONTRIBUTION_ACTUAL_SUPERSEDES",
                    "CONTRIBUTION_ACTUAL_CORRECTION_REQUIRES_SUPERSEDES",
                }:
                    raise AmendmentRejected(_stale_plan())
                raise AmendmentRejected(result)
            source_results.append(result)
        session.info.pop("correction_source_identity", None)
        results, changes, calculations = [], [], {}
        for key in ordered:
            try:
                result = executor.complete(states[key])
            except AmendmentRejected as exc:
                if key not in overrides and any(
                    error == "SETTLEMENT_EXCEEDS_OPEN_BALANCE"
                    or error == "COMPONENT_SOURCE_AMOUNT_EXCEEDED"
                    or "withholding exceeds the payroll-line entitlement" in error
                    or error.startswith("final salary payment must explicitly account")
                    or error.startswith("allocation exceeds open amount")
                    for error in exc.result.get("errors", [])
                ):
                    from .fact_requirements import AccountingFactError, AccountingFactIssue

                    raise AmendmentRejected(
                        AccountingFactError(
                            AccountingFactIssue(
                                code="CORRECTION_PAYMENT_DISPOSITION_REQUIRED",
                                kind="missing_accounting_fact",
                                fields=["event_replacements"],
                                context={"event_id": str(key)},
                                actual_values={
                                    "recorded_payment": graphs[key]["business_events"][0]["facts"]
                                },
                                expected={
                                    "typed_disposition": (
                                        "明确原付款、扣缴与更正后债务差额的处理事实"
                                    )
                                },
                                message=(
                                    "更正后债务与原实际付款或扣缴不能按原分配核销。"
                                    "先核对来源，提供明确分配或差额处理业务；"
                                    "不得改造付款日期或自动生成退款、补款、员工应收。"
                                ),
                            ),
                            ["event_replacements"],
                        ).result()
                    ) from exc
                raise
            results.append(result)
            after = _graph(session, states[key]["source"])
            calculations[str(key)] = accounting_projection(
                _json(
                    {
                        table: [
                            {
                                column: row[column]
                                for column in ("calculation_hash", "policy_snapshot")
                                if column in row
                            }
                            for row in after[table]
                        ]
                        for table in (
                            "payroll_batches",
                            "labor_remuneration_batches",
                            "tax_periods",
                        )
                    }
                )
            )
            changes.append(
                {
                    "event_id": str(key),
                    "voucher_number": result["voucher_number"],
                    "posting_date": str(states[key]["source"].posting_date),
                    "period_status": "open",
                    "before": _financial_snapshot(graphs[key]),
                    "after": _financial_snapshot(after),
                }
            )
        calculated_hash = canonical_sha256(
            {
                "version": "atomic-corrections-v1",
                "request": payload,
                "before": {
                    str(key): canonical_sha256(graphs[key]["business_events"][0]["facts"])
                    for key in ordered
                },
                "changes": changes,
                "sources": accounting_projection(source_snapshots),
                "calculations": calculations,
                "rules": {
                    str(key): accounting_projection(states[key]["source"].rule_trace)
                    for key in ordered
                },
            }
        )
        if not preview and calculated_hash != request.calculation_hash:
            raise AmendmentRejected(_stale_plan())
        result = {
            "status": "calculated" if preview else "posted",
            "calculation_hash": calculated_hash,
            "data": {
                "route": "amend",
                "changes": changes,
                "dependencies": sorted([str(a), str(b)] for a, b in edges),
                "source_versions": source_snapshots,
            },
            "source_results": source_results,
            "events": results,
        }
        if not preview:
            result["correction_id"] = str(correction.id)
        correction.after_state = {"source_results": source_results, "changes": changes}
        correction.result = result
        session.flush()
        if not preview and session.get_bind().dialect.name == "postgresql":
            # Surface deferred business guards inside our rollback boundary,
            # rather than letting the MCP transaction turn them into a generic error.
            session.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            session.execute(text("SET CONSTRAINTS ALL DEFERRED"))
        if preview:
            result = {
                key: value
                for key, value in result.items()
                if key not in {"source_results", "events"}
            }
        return result
