"""Conservative accounting projections for the explicitly reviewed T1 paths.

Only payroll/payroll_bounded actual-withholding fact IDs and evidence are
normalized, including their copy in the actual_withholding_adopted trace and
the one matching source_versions entry. The complete frozen withholding fact
and stable business identity replace its revision ID. Other source and rule
versions, contribution traces, unknown fields and tax states remain significant.

Only wage allocations in payment/cash_payment/platform_payment/
payroll_reserve_payment settlements normalize source_calculation, after checking
the typed allocation against the exact frozen wage and obligation. No other
calculation pointers, evidence paths, tax transfers or domain outputs are exempt.
The framework separately includes the complete owning fact payload.
"""

from __future__ import annotations

from ..contracts import FactVersion, KernelError, Read
from ..types import digest

PAYROLL_KINDS = ("payroll", "payroll_bounded")
PAYMENT_KINDS = ("payment", "cash_payment", "platform_payment", "payroll_reserve_payment")
ACTUAL_KIND = "payroll_withholding_actual"


def _incompatible(reason):
    raise KernelError(
        "accounting_compatibility_required",
        "冻结核算依据不能安全适配，需保留原结果并核查兼容性",
        reason=reason,
    )


def _mapping(value, reason):
    if not isinstance(value, dict):
        _incompatible(reason)
    return value


def _sequence(value, reason):
    if not isinstance(value, (list, tuple)):
        _incompatible(reason)
    return value


def _pointer(value, reason):
    if not isinstance(value, str) or not value:
        _incompatible(reason)
    return value


def _actual_sections(outcome):
    values = _mapping(outcome.get("values"), "payroll_values_missing")
    explanation = _sequence(outcome.get("explanation"), "payroll_explanation_missing")
    adopted = [
        item
        for item in explanation
        if isinstance(item, dict) and item.get("step") == "actual_withholding_adopted"
    ]
    if "actual_withholding" not in values:
        if adopted:
            _incompatible("actual_withholding_values_missing")
        return ()
    actual = _mapping(values["actual_withholding"], "actual_withholding_invalid")
    if len(adopted) != 1:
        _incompatible("actual_withholding_trace_ambiguous")
    trace_values = _mapping(adopted[0].get("values"), "actual_withholding_trace_invalid")
    trace_actual = _mapping(
        trace_values.get("actual_withholding"), "actual_withholding_trace_invalid"
    )
    return actual, trace_actual


def payroll_references(version: FactVersion, outcome: dict) -> tuple[Read, ...]:
    sections = _actual_sections(outcome)
    if not sections:
        return ()
    pointer = _pointer(sections[0].get("fact_id"), "actual_withholding_fact_id_missing")
    if sections[1].get("fact_id") != pointer:
        _incompatible("actual_withholding_trace_source_mismatch")
    return (Read("fact", ACTUAL_KIND, "#" + pointer),)


def project_payroll(version: FactVersion, outcome: dict, refs) -> dict:
    sections = _actual_sections(outcome)
    if not sections:
        return outcome
    pointer = _pointer(sections[0].get("fact_id"), "actual_withholding_fact_id_missing")
    source = refs.fact(pointer, ACTUAL_KIND)
    if (
        source.id != pointer
        or source.fact.kind != ACTUAL_KIND
        or source.fact.employee_id != version.fact.employee_id
        or source.fact.period != version.fact.period
        or source.fact.withholding_confirmed is not True
    ):
        _incompatible("actual_withholding_identity_mismatch")
    if not source.evidence:
        _incompatible("actual_withholding_evidence_missing")
    for actual in sections:
        if actual.get("fact_id") != pointer:
            _incompatible("actual_withholding_trace_source_mismatch")
        evidence = _sequence(actual.get("evidence"), "actual_withholding_evidence_missing")
        if tuple(evidence) != source.evidence:
            _incompatible("actual_withholding_evidence_mismatch")
        reported = actual.get("reported_cumulative_standard_deduction_fen")
        if (
            type(actual.get("withheld_tax_fen")) is not int
            or actual["withheld_tax_fen"] != source.fact.withheld_tax_fen
            or "reported_cumulative_standard_deduction_fen" not in actual
            or (reported is not None and type(reported) is not int)
            or reported != source.fact.reported_cumulative_standard_deduction_fen
        ):
            _incompatible("actual_withholding_payload_mismatch")
    sources = _sequence(outcome["values"].get("source_versions"), "payroll_sources_missing")
    if sum(item == pointer for item in sources) != 1:
        _incompatible("actual_withholding_source_membership_mismatch")
    identity = {
        "kind": source.fact.kind,
        "subject_id": source.subject_id,
        "period": str(source.fact.period),
        "fact_digest": digest(source.fact.model_dump(mode="json")).hex(),
    }
    for actual in sections:
        actual["fact_id"] = dict(identity)
        del actual["evidence"]
    outcome["values"]["source_versions"] = [
        dict(identity) if item == pointer else item for item in sources
    ]
    return outcome


def _payroll_settlements(version, outcome):
    allocations = [a for a in version.fact.allocations if a.source_kind in PAYROLL_KINDS]
    if not allocations:
        return ()
    values = _mapping(outcome.get("values"), "payment_values_missing")
    settlements = _sequence(values.get("settlements"), "payment_settlements_missing")
    selected = []
    for allocation in allocations:
        key = f"{allocation.source_kind}:{allocation.source_id}:{allocation.obligation}"
        matching = [
            row for row in settlements if isinstance(row, dict) and row.get("obligation") == key
        ]
        if len(matching) != 1:
            _incompatible("payroll_settlement_identity_mismatch")
        row = matching[0]
        if type(row.get("amount_fen")) is not int or row["amount_fen"] != allocation.amount_fen:
            _incompatible("payroll_settlement_amount_mismatch")
        _pointer(row.get("source_calculation"), "payroll_settlement_source_missing")
        selected.append((allocation, row))
    return tuple(selected)


def payment_references(version: FactVersion, outcome: dict) -> tuple[Read, ...]:
    return tuple(
        dict.fromkeys(
            Read("calculation", allocation.source_kind, "#" + row["source_calculation"])
            for allocation, row in _payroll_settlements(version, outcome)
        )
    )


def payment_comparison_reads(version: FactVersion) -> tuple[Read, ...]:
    return tuple(dict.fromkeys(
        Read("calculation", allocation.source_kind, "@" + allocation.source_id)
        for allocation in version.fact.allocations if allocation.source_kind in PAYROLL_KINDS
    ))


def project_payment(version: FactVersion, outcome: dict, refs) -> dict:
    fact = version.fact
    replacements = []
    for allocation, row in _payroll_settlements(version, outcome):
        pointer = row["source_calculation"]
        source = refs.calculation(pointer, allocation.source_kind)
        if (
            source.id != pointer
            or source.kind != allocation.source_kind
            or source.subject_id != allocation.source_id
            or source.period > fact.period
        ):
            _incompatible("payroll_settlement_source_identity_mismatch")
        obligations = _sequence(
            source.values.get("obligations"), "payroll_settlement_obligations_missing"
        )
        matching = [
            item
            for item in obligations
            if isinstance(item, dict) and item.get("name") == allocation.obligation
        ]
        if len(matching) != 1 or matching[0].get("key") != row["obligation"]:
            _incompatible("payroll_settlement_obligation_mismatch")
        obligation = matching[0]
        recipient = (
            allocation.recipient_id if fact.payment_method == "bank_batch" else fact.counterparty_id
        )
        if (
            not recipient
            or obligation.get("counterparty_id") not in (None, recipient)
            or (
                fact.payment_method == "individual"
                and allocation.recipient_id not in (None, recipient)
            )
            or (
                allocation.obligation == "net"
                and (
                    obligation.get("counterparty_id") != source.values.get("employee_id")
                    or recipient != source.values.get("employee_id")
                )
            )
        ):
            _incompatible("payroll_settlement_recipient_mismatch")
        if (
            fact.direction != "outflow"
            or obligation.get("normal") != "credit"
            or obligation.get("category") != "payable"
            or "payment" not in obligation.get("settlement_modes", ("payment", "offset"))
        ):
            _incompatible("payroll_settlement_treatment_mismatch")
        signature = _mapping(refs.signature(pointer), "payroll_settlement_signature_missing")
        if "contract" not in signature or "digest" not in signature:
            _incompatible("payroll_settlement_signature_missing")
        replacements.append(
            (
                row,
                {
                    "kind": source.kind,
                    "subject_id": source.subject_id,
                    "period": str(source.period),
                    "signature": dict(signature),
                },
            )
        )
    for row, identity in replacements:
        row["source_calculation"] = identity
    return outcome


def register(registry):
    for kind in PAYROLL_KINDS:
        registry.register_accounting(kind, project_payroll, payroll_references)
    for kind in PAYMENT_KINDS:
        registry.register_accounting(
            kind, project_payment, payment_references, compares_calculations=True,
            comparison_reads=payment_comparison_reads,
        )
