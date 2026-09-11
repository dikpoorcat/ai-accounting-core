"""Compile deterministic reserve cost claims through the shared registration path."""

from .build import calculator_build_id
from .contracts import Context, FactVersion, KernelError
from .domains.managed_reserve import (
    CostClaim,
    ManagedReserveObligationSettlement,
    ReserveSettlementInput,
    adopted_cost,
    calculate_reserve_settlement,
    scope_result,
    source_reads,
)
from .domains.transactions import _through_month
from .types import canonical, digest, sum_fen


class Reserves:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def _prepare(self, subject_id, data, evidence):
        command = ReserveSettlementInput.model_validate_json(canonical(data))
        if not evidence:
            raise KernelError("reserve_evidence_required", "需要实际结清及费用范围的明确依据")
        with self.store.connection(read_only=True) as connection:
            connection.execute("BEGIN")
            epochs = self.store.epochs(connection)
            reads = source_reads("managed_reserve_scope", command.scope_id)
            context = Context({read: self.store.select(connection, read) for read in reads})
            scope = scope_result(context, command.scope_id, command.period)
            costs = sorted(
                (x for x in scope.values["cost_sources"] if x["period"] <= command.period),
                key=lambda x: (x["period"], x["source_id"]),
            )
            remaining, claims = command.amount_fen, []
            for cost in costs:
                kind, sid = cost["source_kind"], cost["source_id"]
                read = _through_month("fact", "*", "expense-return:" + sid, command.period)
                keys = (*source_reads(kind, sid), read)
                selected = {key: self.store.select(connection, key) for key in keys}
                source, amount = adopted_cost(
                    Context(selected), kind, sid, cost.get("adopting_scope_id", command.scope_id)
                )
                used = sum_fen(
                    claim.amount
                    for row in selected[read]
                    if row.subject_id != subject_id
                    for claim in row.fact.claims()
                    if claim.key == read.key
                )
                available = amount - used
                if available < 0:
                    raise KernelError("excess_expense_recovery", "范围中原成本已被超额使用")
                allocated = min(remaining, available)
                if allocated:
                    claims.append(CostClaim(source_kind=kind, source_id=sid, amount_fen=allocated))
                    remaining -= allocated
                if remaining == 0:
                    break
            if remaining:
                raise KernelError(
                    "insufficient_reserve_capacity",
                    "已费用化备用金扣除回收及既有结清后容量不足",
                    missing_fen=remaining,
                )
            fact = ManagedReserveObligationSettlement(
                **command.model_dump(), cost_claims=tuple(claims)
            )
            version = FactVersion("preview:" + subject_id, subject_id, 0, fact, tuple(evidence))
            selected = {read: self.store.select(connection, read) for read in fact.reads()}
            for read, rows in tuple(selected.items()):
                if read.source == "fact" and read.kind == "*" and read.key in fact.scopes():
                    selected[read] = tuple(row for row in rows if row.subject_id != subject_id) + (
                        version,
                    )
            outcome = calculate_reserve_settlement(version, Context(selected))
            old = connection.execute(
                "SELECT fact_id FROM fact_current WHERE subject_id=?", (subject_id,)
            ).fetchone()
            expected_revision = self.store.fact(connection, old[0]).revision if old else 0
            connection.commit()
        result = dict(
            status="ready",
            subject_id=subject_id,
            epochs=epochs,
            expected_revision=expected_revision,
            amount_fen=command.amount_fen,
            cost_claims=[item.model_dump(mode="json") for item in claims],
            attribution="accounting_capacity_only_not_actual_payment_source",
            calculation=dict(lines=[vars(line) for line in outcome.lines], values=outcome.values),
        )
        result["digest"] = digest(
            [
                {**result, "epochs": {"accounting": epochs["accounting"]}},
                fact.model_dump(mode="json"),
                sorted(evidence),
                calculator_build_id(),
            ]
        ).hex()
        return result, fact

    def preview_settlement(
        self, subject_id: str, data: ReserveSettlementInput, *, evidence: tuple[str, ...]
    ):
        raw = data.model_dump(mode="json") if isinstance(data, ReserveSettlementInput) else data
        return self._prepare(subject_id, raw, evidence)[0]

    def confirm_settlement(
        self,
        subject_id: str,
        data: ReserveSettlementInput,
        *,
        evidence: tuple[str, ...],
        preview_digest: str,
        epochs: dict,
        expected_revision: int,
        request_id: str,
        recording_error_confirmed: bool = False,
    ):
        if type(recording_error_confirmed) is not bool:
            raise KernelError("invalid_confirmation", "录入更正确认必须明确为布尔值")
        raw = data.model_dump(mode="json") if isinstance(data, ReserveSettlementInput) else data
        command = ReserveSettlementInput.model_validate_json(canonical(raw))
        raw = command.model_dump(mode="json")
        request_hash = digest(
            [
                "reserve_settlement",
                subject_id,
                raw,
                sorted(evidence),
                preview_digest,
                epochs,
                expected_revision,
                recording_error_confirmed,
            ]
        )
        cached = self.engine._cached(request_id, request_hash)
        if cached is not None:
            return cached
        preview, fact = self._prepare(subject_id, raw, evidence)
        if preview["digest"] != preview_digest or preview["expected_revision"] != expected_revision:
            raise KernelError("preview_expired", "费用范围、原债或占用已变化，请重新预览")
        _, operation = self.engine._registration(
            recording_error_confirmed,
            fact.kind,
            subject_id,
            fact.model_dump(mode="json"),
            evidence=evidence,
            expected_revision=expected_revision,
        )
        return self.engine._write(
            request_id,
            request_hash,
            epochs,
            ("accounting",),
            "managed_reserve_settlement",
            operation,
        )
