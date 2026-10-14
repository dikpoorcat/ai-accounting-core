"""Conservative accounting projections for confirmed remuneration and tax sources.

For payroll/payroll_bounded, validated monthly-plan or no-change confirmation
provenance is normalized to a stable confirmed marker; its exact facts remain
recorded by the calculation dependency graph. Actual-withholding fact IDs and
evidence are also normalized, including their copy in the
actual_withholding_adopted trace and the one matching source_versions entry.
The complete frozen withholding fact and stable business identity replace its
revision ID. Other source and rule versions, contribution traces, unknown fields
and tax states remain significant.

Wage and tax allocations in payment/cash_payment/platform_payment/
payroll_reserve_payment settlements normalize source_calculation, after checking
the typed allocation against its exact frozen source and obligation. Actual tax
sources are verified before excluding reference arithmetic and submission-only
metadata. Formal amounts, lines, balances and unknown fields remain significant.
Other calculation pointers and tax transfers remain unchanged.
The framework includes the owning fact payload; only explicitly declared tax
reference inputs receive a detached comparison view after source validation.
"""

from __future__ import annotations

from ..accounting import AccountingProjection
from ..contracts import FactVersion, KernelError, Read
from ..types import digest
from .money import SETTLEMENT_PAYMENT_KINDS

PAYROLL_KINDS = ("payroll", "payroll_bounded")
ACTUAL_KIND = "payroll_withholding_actual"
TAX_BASIS_KIND = "remuneration_tax_basis"


def _confirmed_tax_sections(outcome):
    values = _mapping(outcome.get("values"), "remuneration_values_missing")
    if "tax_basis" not in values:
        return ()
    basis = _mapping(values["tax_basis"], "remuneration_tax_basis_invalid")
    traces = [
        item
        for item in _sequence(outcome.get("explanation"), "remuneration_trace_missing")
        if isinstance(item, dict) and item.get("step") == "confirmed_remuneration_tax"
    ]
    if len(traces) != 1:
        _incompatible("remuneration_tax_trace_ambiguous")
    traced = _mapping(
        _mapping(traces[0].get("values"), "remuneration_tax_trace_invalid").get("tax_basis"),
        "remuneration_tax_trace_invalid",
    )
    return basis, traced


def confirmed_tax_references(version, outcome):
    sections = _confirmed_tax_sections(outcome)
    if not sections:
        return ()
    pointer = _pointer(sections[0].get("fact_id"), "remuneration_tax_fact_missing")
    if sections[1].get("fact_id") != pointer:
        _incompatible("remuneration_tax_trace_mismatch")
    return (Read("fact", TAX_BASIS_KIND, "#" + pointer),)


def project_confirmed_tax(version, outcome, refs):
    sections = _confirmed_tax_sections(outcome)
    if not sections:
        return outcome
    pointer = _pointer(sections[0].get("fact_id"), "remuneration_tax_fact_missing")
    source = refs.fact(pointer, TAX_BASIS_KIND)
    if (
        not source.evidence
        or source.fact.source_id != version.subject_id
        or (
            source.fact.source_kind != version.fact.kind
            or source.fact.period != version.fact.period
        )
    ):
        _incompatible("remuneration_tax_identity_mismatch")
    expected = source.fact.model_dump(mode="json")
    for section in sections:
        if (
            section.get("fact_id") != pointer
            or tuple(section.get("evidence", ())) != source.evidence
        ):
            _incompatible("remuneration_tax_evidence_mismatch")
        if (
            section.get("subject_id") != source.subject_id
            or section.get("revision") != source.revision
            or section.get("cash_payment_recorded") is not False
        ):
            _incompatible("remuneration_tax_provenance_mismatch")
        if any(section.get(key) != value for key, value in expected.items()):
            _incompatible("remuneration_tax_payload_mismatch")
    # The full source remains frozen in the original outcome and dependencies.
    # Updating its evidence or confirming that withholding subsequently occurred
    # does not change the monetary adoption or create a redundant voucher.
    monetary = {
        key: expected[key]
        for key in (
            "source_kind",
            "source_id",
            "period",
            "tax_period",
            "tax_amount_fen",
        )
    }
    identity = {
        "kind": TAX_BASIS_KIND,
        "subject_id": source.fact.source_id,
        "period": str(source.fact.period),
        "fact_digest": digest(monetary).hex(),
    }
    for section in sections:
        for key in (*expected, "subject_id", "revision", "evidence", "cash_payment_recorded"):
            if key not in monetary:
                section.pop(key, None)
        section.update(monetary | {"fact_id": dict(identity)})
    sources = _sequence(outcome["values"].get("source_versions"), "remuneration_sources_missing")
    if sum(item == pointer for item in sources) != 1:
        _incompatible("remuneration_tax_source_membership_mismatch")
    outcome["values"]["source_versions"] = [
        dict(identity) if item == pointer else item for item in sources
    ]
    return outcome


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
    if "tax_basis" in outcome.get("values", {}):
        return confirmed_tax_references(version, outcome)
    sections = _actual_sections(outcome)
    if not sections:
        return ()
    pointer = _pointer(sections[0].get("fact_id"), "actual_withholding_fact_id_missing")
    if sections[1].get("fact_id") != pointer:
        _incompatible("actual_withholding_trace_source_mismatch")
    return (Read("fact", ACTUAL_KIND, "#" + pointer),)


def project_payroll(version: FactVersion, outcome: dict, refs) -> dict:
    values = _mapping(outcome.get("values"), "payroll_values_missing")
    if (
        values.get("superseded") is True
        and isinstance(values.get("identity_correction"), str)
        and values.get("identity_correction")
        and values.get("obligations") == []
        and outcome.get("lines") == []
        and outcome.get("balances") == []
    ):
        return outcome
    confirmation = _mapping(values.get("payroll_confirmation"), "payroll_confirmation_missing")
    if confirmation.get("mode") not in {"monthly_plan", "explicit_no_change"}:
        _incompatible("payroll_confirmation_mode_invalid")
    for field in (
        "confirmation_fact_id",
        "confirmation_subject_id",
        "confirmation_revision",
        "profile_fact_id",
        "contribution_policy_fact_id",
        "income_tax_policy_fact_id",
    ):
        value = confirmation.get(field)
        if field == "income_tax_policy_fact_id" and value is None:
            continue
        if (field == "confirmation_revision" and (type(value) is not int or value < 1)) or (
            field != "confirmation_revision" and (not isinstance(value, str) or not value)
        ):
            _incompatible("payroll_confirmation_invalid")
    # Confirmation provenance is an indispensable publication gate and dependency,
    # while changing only that provenance does not change the accounting result.
    values["payroll_confirmation"] = {"confirmed": True}
    if "tax_basis" in values:
        return project_confirmed_tax(version, outcome, refs)
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


def _selected_settlements(version, outcome, kinds, label):
    allocations = [a for a in version.fact.allocations if a.source_kind in kinds]
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
            _incompatible(label + "_settlement_identity_mismatch")
        row = matching[0]
        if type(row.get("amount_fen")) is not int or row["amount_fen"] != allocation.amount_fen:
            _incompatible(label + "_settlement_amount_mismatch")
        _pointer(row.get("source_calculation"), label + "_settlement_source_missing")
        selected.append((allocation, row))
    return tuple(selected)


def _payroll_settlements(version, outcome):
    return _selected_settlements(version, outcome, PAYROLL_KINDS, "payroll")


def _tax_settlements(version, outcome):
    return _selected_settlements(
        version, outcome, ("tax_assessment", "income_tax_assessment"), "tax"
    )


def payment_references(version: FactVersion, outcome: dict) -> tuple[Read, ...]:
    return tuple(
        dict.fromkeys(
            Read("calculation", allocation.source_kind, "#" + row["source_calculation"])
            for allocation, row in (*_payroll_settlements(version, outcome),
                                    *_tax_settlements(version, outcome))
        )
    )


def payment_comparison_reads(version: FactVersion) -> tuple[Read, ...]:
    return tuple(
        dict.fromkeys(
            Read("calculation", allocation.source_kind, "@" + allocation.source_id)
            for allocation in version.fact.allocations
            if allocation.source_kind in (*PAYROLL_KINDS, "tax_assessment", "income_tax_assessment")
        )
    )


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
    for allocation, row in _tax_settlements(version, outcome):
        pointer = row["source_calculation"]
        source = refs.calculation(pointer, allocation.source_kind)
        if (source.subject_id != allocation.source_id or source.period > fact.period):
            _incompatible("tax_settlement_source_identity_mismatch")
        matching = [item for item in source.values.get("obligations", ())
                    if item.get("name") == allocation.obligation
                    and item.get("key") == row["obligation"]]
        if len(matching) != 1:
            _incompatible("tax_settlement_obligation_mismatch")
        obligation = matching[0]
        recipient = (allocation.recipient_id if fact.payment_method == "bank_batch"
                     else fact.counterparty_id)
        expected = "credit" if fact.direction == "outflow" else "debit"
        if (not recipient or obligation.get("counterparty_id") not in (None, recipient)
                or obligation.get("normal") != expected
                or "payment" not in obligation.get("settlement_modes", ("payment", "offset"))):
            _incompatible("tax_settlement_treatment_mismatch")
        row["source_calculation"] = {
            "kind": source.kind, "subject_id": source.subject_id,
            "period": str(source.period), "signature": dict(refs.signature(pointer)),
        }
    return outcome


_TAX_REFERENCE_VALUES = frozenset({
    "amount_source", "actual_declaration", "actual_fields", "calculated_assessment",
    "calculation_reference_issue", "declaration_adjustment_fen", "exemption_relief_confirmed",
    "vat_policy_version", "surtax_policy_version", "taxable_sales_fen", "exempt_sales_fen",
    "filing_fields", "filing_mapping_version", "filing_source_url", "filing_effective_from",
    "filing_issues", "prepaid_tax_fen", "tax_due_fen",
})


def tax_assessment_references(version, outcome):
    values = _mapping(outcome.get("values"), "tax_assessment_values_missing")
    basis = values.get("actual_declaration")
    if basis is None:
        if values.get("amount_source") == "actual":
            _incompatible("actual_tax_source_missing")
        return ()
    basis = _mapping(basis, "actual_tax_source_invalid")
    kind = (
        "vat_declaration_actual" if version.fact.kind == "tax_assessment"
        else "income_tax_declaration_actual"
    )
    return (Read("fact", kind, "#" + _pointer(basis.get("fact_id"), "actual_tax_fact_missing")),)


def project_tax_assessment(version, outcome, refs):
    """Reference arithmetic and submission metadata cannot change formal tax accounting."""
    values = _mapping(outcome.get("values"), "tax_assessment_values_missing")
    reads = tax_assessment_references(version, outcome)
    extra_basis = None
    if reads:
        source = refs.fact(reads[0].key[1:], reads[0].kind)
        basis = values["actual_declaration"]
        if (
            source.subject_id != basis.get("subject_id")
            or source.fact.assessment_id != version.subject_id
            or source.fact.filing_evidence != basis.get("filing_evidence")
            or source.fact.filing_evidence not in source.evidence
            or source.fact.basis_type != basis.get("basis_type")
            or values.get("amount_source") != "actual"
        ):
            _incompatible("actual_tax_source_identity_mismatch")
        if version.fact.kind == "tax_assessment":
            if (source.fact.period_start, source.fact.period_end) != (
                version.fact.period_start, version.fact.period_end
            ):
                _incompatible("actual_tax_source_period_mismatch")
            fields = {"assessed_vat_fen": "payable_vat_fen", "urban_tax_fen": "urban_tax_fen",
                      "education_tax_fen": "education_tax_fen",
                      "local_education_tax_fen": "local_education_tax_fen"}
        else:
            if (source.fact.year != version.fact.year
                    or str(source.fact.tax_period_end) != values.get("tax_period_end")):
                _incompatible("actual_tax_source_period_mismatch")
            fields = {"cumulative_assessed_fen": "cumulative_assessed_fen"}
        if any(type(getattr(source.fact, field)) is not int
               or getattr(source.fact, field) != values.get(target)
               for field, target in fields.items()):
            _incompatible("actual_tax_adopted_amount_mismatch")
        extra_basis = {key: value for key, value in basis.items()
                       if key not in {"subject_id", "fact_id", "filing_evidence", "basis_type"}}
    outcome["values"] = {key: value for key, value in values.items()
                         if key not in _TAX_REFERENCE_VALUES}
    if extra_basis:
        outcome["values"]["actual_declaration"] = extra_basis
    if version.fact.kind == "income_tax_assessment":
        outcome["values"].setdefault("year", version.fact.year)
        outcome["values"].setdefault(
            "tax_period_end", f"{version.fact.year:04d}-12"
            if version.fact.assessment_basis == "annual_settlement" else str(version.fact.period),
        )
    explanation = []
    for item in outcome["explanation"]:
        if isinstance(item, dict):
            item = {key: value for key, value in item.items()
                    if key not in {"vat_policy_id", "surtax_policy_id"}}
            if not item:
                continue
        explanation.append(item)
    outcome["explanation"] = explanation
    fact = version.fact.model_dump(mode="json")
    if version.fact.kind == "income_tax_assessment":
        fact["cumulative_assessed_fen"] = values["cumulative_assessed_fen"]
        fact.pop("policy_id", None)
        fact.pop("basis_id", None)
    else:
        # The exact policy references remain in facts and dependencies. Their
        # accounting effect is already represented by the adopted tax result.
        fact["vat_policy_id"] = "reference_only"
        fact["surtax_policy_id"] = "reference_only"
    return AccountingProjection(fact, outcome)


def register(registry):
    for kind in PAYROLL_KINDS:
        registry.register_accounting(kind, project_payroll, payroll_references)
    for kind in ("annual_bonus", "labor"):
        registry.register_accounting(kind, project_confirmed_tax, confirmed_tax_references)
    for kind in ("tax_assessment", "income_tax_assessment"):
        registry.register_accounting(kind, project_tax_assessment, tax_assessment_references)
    for kind in SETTLEMENT_PAYMENT_KINDS:
        registry.register_accounting(
            kind,
            project_payment,
            payment_references,
            compares_calculations=True,
            comparison_reads=payment_comparison_reads,
        )
