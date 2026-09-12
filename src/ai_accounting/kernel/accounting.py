"""On-demand equivalence of frozen facts and results, never a stored outcome field.

The default is deliberately conservative. Only registered domain policies may
normalize proven provenance paths. Loading happens in the caller's read snapshot;
signature computation and domain callbacks operate exclusively on detached data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from .contracts import Calculation, FactVersion, KernelError, freeze
from .types import canonical, digest

ACCOUNTING_CONTRACT = "accounting-v1"


def compatibility(calculation_id, reason):
    return KernelError(
        "accounting_compatibility_required",
        "此项核算需要受控历史适配后重新预览；无需补造业务事实",
        calculation_id=calculation_id,
        reason=reason,
    )


@dataclass(frozen=True)
class AccountingRecord:
    calculation: Calculation
    version: FactVersion
    outcome: dict
    fact_ids: frozenset[str]
    calculation_ids: frozenset[str]


class AccountingReferences:
    """Exact recorded dependencies, not the current heads of their subjects."""

    def __init__(self, book, record):
        self.book, self.record = book, record

    def fact(self, fact_id, kind):
        if fact_id not in self.record.fact_ids:
            raise compatibility(self.record.calculation.id, "unrecorded_fact_dependency")
        result = self.book.facts.get(fact_id)
        if result is None or result.fact.kind != kind:
            raise compatibility(self.record.calculation.id, "frozen_fact_dependency_missing")
        return result

    def calculation(self, calculation_id, kind):
        if calculation_id not in self.record.calculation_ids:
            raise compatibility(self.record.calculation.id, "unrecorded_calculation_dependency")
        result = self.book.records.get(calculation_id)
        if result is None or result.calculation.kind != kind:
            raise compatibility(self.record.calculation.id, "frozen_calculation_dependency_missing")
        return result.calculation

    def signature(self, calculation_id):
        if calculation_id not in self.record.calculation_ids:
            raise compatibility(self.record.calculation.id, "unrecorded_calculation_dependency")
        return self.book.signature(calculation_id)


class AccountingBook:
    """Request-local frozen comparison inputs, errors and memoized signatures."""

    def __init__(self, registry):
        self.registry = registry
        self.records: dict[str, AccountingRecord] = {}
        self.facts: dict[str, FactVersion] = {}
        self.errors: dict[str, KernelError] = {}
        self.signatures: dict[str, dict] = {}
        self.active: set[str] = set()

    def add(self, calculation, version, outcome, fact_ids=(), calculation_ids=()):
        self.facts[version.id] = version
        self.records[calculation.id] = AccountingRecord(
            calculation, version, freeze(outcome),
            frozenset((*fact_ids, version.id)), frozenset(calculation_ids),
        )
        self.signatures.pop(calculation.id, None)
        self.errors.pop(calculation.id, None)

    def load(self, store, connection, calculation_ids):
        """Load only requested results and domain-declared exact comparison inputs.

        Unsupported historical shapes are retained as local comparison errors;
        merely loading one cannot make an unrelated query or calculation fail.
        """
        queue, visited = list(calculation_ids), set()
        while queue:
            calculation_id = queue.pop()
            if calculation_id in visited or calculation_id in self.errors:
                continue
            visited.add(calculation_id)
            try:
                if calculation_id not in self.records:
                    row = connection.execute(
                        "SELECT c.* FROM calculation c JOIN calculation_seal s "
                        "ON s.calculation_id=c.id WHERE c.id=?", (calculation_id,),
                    ).fetchone()
                    if row is None:
                        raise compatibility(calculation_id, "frozen_calculation_missing")
                    outcome = json.loads(row["outcome"])
                    if digest(outcome) != row["digest"]:
                        raise compatibility(calculation_id, "frozen_result_digest_mismatch")
                    version = self.facts.get(row["fact_id"])
                    if version is None:
                        version = store.fact(connection, row["fact_id"])
                    self.add(
                        store.calculation(row), version, outcome,
                        (r[0] for r in connection.execute(
                            "SELECT fact_id FROM dependency_fact WHERE calculation_id=?",
                            (calculation_id,),
                        )),
                        (r[0] for r in connection.execute(
                            "SELECT upstream_id FROM dependency_calculation WHERE calculation_id=?",
                            (calculation_id,),
                        )),
                    )
                record = self.records[calculation_id]
                _, requirements = self.registry.accounting_projectors.get(
                    record.calculation.kind, (None, None)
                )
                if requirements is None:
                    continue
                for read in requirements(record.version, record.outcome):
                    if not read.key.startswith("#"):
                        raise compatibility(calculation_id, "comparison_requires_exact_reference")
                    ident = read.key[1:]
                    if read.source == "fact":
                        if ident not in record.fact_ids:
                            raise compatibility(calculation_id, "unrecorded_fact_dependency")
                        if ident not in self.facts:
                            self.facts[ident] = store.fact(connection, ident)
                        if self.facts[ident].fact.kind != read.kind:
                            raise compatibility(calculation_id, "fact_dependency_kind_mismatch")
                    else:
                        if ident not in record.calculation_ids:
                            raise compatibility(calculation_id, "unrecorded_calculation_dependency")
                        queue.append(ident)
            except (KernelError, ValueError, KeyError, TypeError, AttributeError) as exc:
                self.errors[calculation_id] = (
                    exc if isinstance(exc, KernelError)
                    and exc.code == "accounting_compatibility_required"
                    else compatibility(calculation_id, "frozen_comparison_input_unavailable")
                )

    def signature(self, calculation_id, *, contract=ACCOUNTING_CONTRACT):
        if contract != ACCOUNTING_CONTRACT:
            raise compatibility(calculation_id, "unsupported_comparison_contract")
        if calculation_id in self.errors:
            raise self.errors[calculation_id]
        if calculation_id in self.signatures:
            return self.signatures[calculation_id]
        if calculation_id in self.active:
            raise compatibility(calculation_id, "comparison_dependency_cycle")
        record = self.records.get(calculation_id)
        if record is None:
            raise compatibility(calculation_id, "comparison_input_missing")
        self.active.add(calculation_id)
        try:
            calculation, version = record.calculation, record.version
            identity = (
                calculation.fact_id, calculation.subject_id, calculation.kind, calculation.period
            )
            if identity != (
                version.id, version.subject_id, version.fact.kind, version.fact.period
            ):
                raise compatibility(calculation_id, "frozen_fact_identity_mismatch")
            outcome = json.loads(canonical(record.outcome))
            if not isinstance(outcome.get("values"), dict) or any(
                not isinstance(outcome.get(key), list) for key in ("lines", "balances")
            ):
                raise compatibility(calculation_id, "required_outcome_structure_missing")
            projector, _ = self.registry.accounting_projectors.get(calculation.kind, (None, None))
            if projector is not None:
                outcome = projector(version, outcome, AccountingReferences(self, record))
            result = freeze({
                "contract": contract,
                "digest": digest({
                    "contract": contract,
                    "subject_id": version.subject_id,
                    "kind": version.fact.kind,
                    "period": str(version.fact.period),
                    "fact": version.fact.model_dump(mode="json"),
                    "outcome": outcome,
                }).hex(),
            })
            self.signatures[calculation_id] = result
            return result
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            if isinstance(exc, KernelError) and exc.code == "accounting_compatibility_required":
                raise
            raise compatibility(calculation_id, "frozen_comparison_shape_unsupported") from exc
        finally:
            self.active.remove(calculation_id)
