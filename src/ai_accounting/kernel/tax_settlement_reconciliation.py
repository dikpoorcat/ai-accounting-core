"""Compare actual tax settlement fields with formally adopted obligations and settlements."""

from __future__ import annotations

import calendar

from .business_queries import BusinessQueries
from .contracts import KernelError, Read
from .types import YearMonth, sum_fen

ACCOUNTS = {"tax": "222106", "vat": "222101", "surtax": "222102"}


def _values(calculation):
    return calculation["outcome"]["values"]


def _timing(fact, posting_period, cutoff, *, settlement):
    """Month-end includes a whole month; an intermediate day requires a real event date."""
    posting = YearMonth(posting_period)
    if posting > cutoff.period:
        return False
    if settlement:
        actual = getattr(fact, "actual_date", None) or getattr(
            fact, "actual_creditor_payment_date", None
        )
        if actual is not None:
            if actual > cutoff:
                return False
            if posting == actual.period:
                return True
    if posting < cutoff.period:
        return True
    last = calendar.monthrange(int(cutoff[:4]), int(cutoff[5:7]))[1]
    return True if int(cutoff[-2:]) == last else None


def _source_subjects(reads, calculation):
    subjects = {calculation["subject_id"]}
    if calculation["kind"] != "income_tax_assessment":
        return subjects
    year = calculation["fact_data"]["year"]
    pending, visited = [calculation["id"]], set()
    while pending:
        ident = pending.pop()
        if ident in visited:
            continue
        visited.add(ident)
        parents = reads.parents(ident)
        for parent in reads.calculations(parents).values():
            if parent["kind"] == "income_tax_assessment" and parent["fact_data"]["year"] == year:
                subjects.add(parent["subject_id"])
                pending.append(parent["id"])
    return subjects


def _obligation(calculation, key, account):
    candidates = [
        item for item in _values(calculation).get("obligations", ())
        if item["key"] == key and item["account"] == account
    ]
    if len(candidates) != 1:
        return None
    return candidates[0]


def reconcile(reads, calculation, *, tax_name="tax"):
    """Read formal ledger evidence in the caller's snapshot without creating tax/payment cycles."""
    if not reads._snapshot_active or not reads.connection.in_transaction:
        raise ValueError("tax reconciliation requires the caller's active read snapshot")
    account = ACCOUNTS[tax_name]
    expected_kind = "income_tax_assessment" if tax_name == "tax" else "tax_assessment"
    if calculation["kind"] != expected_kind:
        raise ValueError("tax component does not belong to this assessment")
    basis = _values(calculation).get("actual_declaration")
    result = {
        "status": "unestablished", "actual_due_fen": None, "book_due_fen": None,
        "book_settled_fen": None, "settlement_as_of": None, "settlement_fact_ids": [],
    }
    if not basis:
        return result
    reads.verify_fact_versions((basis["fact_id"],))
    source = reads.fact_version(basis["fact_id"])
    fact = source.fact
    source_kind = "income_tax_declaration_actual" if tax_name == "tax" else "vat_declaration_actual"
    if (
        fact.kind != source_kind or fact.assessment_id != calculation["subject_id"]
        or source.subject_id != basis["subject_id"]
        or fact.filing_evidence != basis["filing_evidence"]
        or fact.filing_evidence not in source.evidence
    ):
        raise KernelError("content_integrity_failed", "实际税款结算来源与精确采用版本不符")
    if tax_name == "surtax":
        dues = [fact.urban_due_fen, fact.education_due_fen, fact.local_education_due_fen]
        actual_due = sum_fen(dues) if all(type(value) is int for value in dues) else None
    else:
        actual_due = fact.tax_due_fen if tax_name == "tax" else fact.vat_due_fen
    cutoff = fact.settlement_as_of
    result.update(actual_due_fen=actual_due, settlement_as_of=str(cutoff) if cutoff else None)
    if actual_due is None or cutoff is None:
        return result
    if tax_name == "tax":
        matches = (
            fact.year == calculation["fact_data"]["year"]
            and str(fact.tax_period_end) == _values(calculation)["tax_period_end"]
        )
    else:
        matches = (
            fact.period_start == calculation["fact_data"]["period_start"]
            and fact.period_end == calculation["fact_data"]["period_end"]
        )
    if not matches:
        raise KernelError("content_integrity_failed", "实际税款结算来源与核算覆盖期不符")

    subjects = _source_subjects(reads, calculation)
    queries = BusinessQueries(reads.engine, reads=reads)
    projection = queries.settlements(
        reads.connection, str(cutoff.period), subject_ids=subjects,
    )
    obligations = {
        item["key"]: item for item in projection["obligations"]
        if item["account"] == account
        and item["source_business"]["subject_id"] in subjects
    }
    movements = [
        item for item in projection["movements"] if item.get("obligation_key") in obligations
    ]
    identifiers = {
        event["calculation_id"]
        for item in obligations.values() for event in item["source_events"]
    } | {
        item["settlement_calculation_id"] for item in movements
    } | {
        item["source_calculation_id"] for item in movements
        if item.get("source_calculation_id")
    }
    calculations = reads.calculations(identifiers)
    facts = reads.fact_versions({item["fact_id"] for item in calculations.values()})
    assessed, settled, adopted_directions = 0, 0, 0
    unknown, settled_facts, published_facts = False, set(), set()
    for key, item in obligations.items():
        for event in item["source_events"]:
            source_calculation = calculations[event["calculation_id"]]
            included = _timing(
                facts[source_calculation["fact_id"]].fact, event["posting_period"], cutoff,
                settlement=False,
            )
            if included is None:
                unknown = True
            if not included:
                continue
            obligation = _obligation(source_calculation, key, account)
            if obligation is None or obligation["normal"] not in {"credit", "debit"}:
                unknown = True
                continue
            sign = 1 if obligation["normal"] == "credit" else -1
            assessed = sum_fen((assessed, event["direction"] * sign * obligation["amount_fen"]))
            if source_calculation["id"] == calculation["id"]:
                adopted_directions += event["direction"]
    for movement in movements:
        item = calculations[movement["settlement_calculation_id"]]
        included = _timing(
            facts[item["fact_id"]].fact, movement["posting_period"], cutoff, settlement=True,
        )
        if included is None:
            unknown = True
        if not included:
            continue
        published_facts.add(item["fact_id"])
        source_calculation = calculations.get(movement.get("source_calculation_id"))
        obligation = (
            _obligation(source_calculation, movement["obligation_key"], account)
            if source_calculation is not None else None
        )
        amount = movement.get("signed_amount_fen")
        if movement["state"] != "resolved" or obligation is None or type(amount) is not int:
            unknown = True
            continue
        sign = 1 if obligation["normal"] == "credit" else -1
        settled = sum_fen((settled, sign * amount))
        settled_facts.add(item["fact_id"])

    # Relevant observed funds without a formal adoption cannot prove that the
    # ledger and the actual declaration are fully reconciled.
    after = (
        YearMonth.from_ordinal(cutoff.period.ordinal + 1) if cutoff.period != "9999-12" else None
    )
    for key in obligations:
        for observed in reads.select(Read("fact", "*", "payment:" + key, after)):
            included = _timing(observed.fact, str(observed.fact.period), cutoff, settlement=True)
            if (
                included is not False and observed.id not in published_facts
                and observed.fact.claims()
            ):
                unknown = True
    result["settlement_fact_ids"] = sorted(settled_facts)
    if unknown or adopted_directions <= 0:
        return result
    book_due = sum_fen((assessed, -settled))
    return result | {
        "status": "matched" if book_due == actual_due else "different",
        "book_due_fen": book_due, "book_settled_fen": settled,
    }
