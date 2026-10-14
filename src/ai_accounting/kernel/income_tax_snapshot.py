"""Transaction-owned tax-before-profit inputs; calculators never read SQLite."""

from __future__ import annotations

from collections import Counter

from .account_definitions import PROFIT_ACCOUNTS
from .contracts import Read
from .types import YearMonth, canonical, digest


def build_basis(reads, year, quarter, rows, facts, references, statements, *, opening_source):
    """Use the exact rows which the shared financial report just calculated.

    Cash settlement and income tax cannot be dependencies of their own provision.
    Close metadata is deliberately absent: freezing unchanged rows is equivalent.
    """
    first = YearMonth(f"{year:04d}-01").ordinal
    last = YearMonth(f"{year:04d}-{quarter * 3:02d}")
    fields = (
        "version_id", "period", "calculation_id", "reverses_id", "line_no",
        "account", "debit", "credit", "fact_id", "calculation_period",
    )
    selected = [
        {key: row[key] for key in fields}
        for row in rows
        if first <= row["period"] <= last.ordinal
        and row["account"] in PROFIT_ACCOUNTS and row["account"] != "5801"
        and row.get("kind") != "income_tax_assessment"
    ]
    vouchers = {row["reverses_id"] or row["version_id"] for row in selected}
    classifications = {
        row[0] for row in reads.connection.execute(
            "SELECT revision_id FROM fact_report_classification WHERE voucher_version_id "
            "IN (SELECT value FROM json_each(?)) AND revision_id "
            "IN (SELECT value FROM json_each(?))",
            (canonical(sorted(vouchers)), canonical(sorted(references))),
        )
    }
    fact_ids = classifications | {
        item.id for item in facts
        if item.fact.kind in {"report_profile", "continuation_report_profile", "report_carry_forward"}
    }
    if opening_source is not None:
        fact_ids.add(opening_source["fact_id"])
    calculation_ids = sorted({row["calculation_id"] for row in selected})
    calculations = reads.calculations(calculation_ids)
    fact_sources = [
        {"fact_id": row[0], "digest": row[1].hex()}
        for row in reads.connection.execute(
            "SELECT id,digest FROM fact_revision WHERE id IN (SELECT value FROM json_each(?))",
            (canonical(sorted(fact_ids)),),
        )
    ]
    basis = {
        "contract": "income-tax-pretax-basis/1",
        "company_id": reads.store.company_id,
        "database_id": reads.store.database_id,
        "year": year, "quarter": quarter, "tax_period_end": str(last),
        "profit_before_tax_fen": statements["profit_statement"]["30"]["year_to_date_fen"],
        "rows": sorted(selected, key=canonical),
        "facts": sorted(fact_sources, key=canonical),
        "calculations": [
            {"calculation_id": ident, "result_digest": calculations[ident]["result_digest"]}
            for ident in calculation_ids
        ],
    }
    return basis | {
        "basis_digest": digest(basis).hex(),
        "calculation_ids": calculation_ids,
        "fact_ids": sorted(fact_ids),
        "source_basis": [
            *({"type": "calculation_id", "value": ident} for ident in calculation_ids),
            *({"type": "fact_id", "value": ident} for ident in sorted(fact_ids)),
            {"type": "report_field", "value": "profit_statement.30.year_to_date_fen"},
        ],
    }


def tax_basis(engine, reads, year, quarter, *, source="open"):
    from .reports import Reports

    key = ("income_tax_basis", year, quarter, source)
    cache = reads._report_snapshot_cache
    if key not in cache:
        report = Reports(engine)._report(
            year, quarter, source=source, connection=reads.connection, reads=reads,
            _pre_tax_fields_only=True, _income_tax_basis=True,
        )
        cache[key] = report["income_tax_snapshot"]
    return cache[key]


def dependency_reads(snapshot):
    return (
        *(Read("calculation", "*", "#" + ident) for ident in snapshot["calculation_ids"]),
        *(Read("fact", "*", "#" + ident) for ident in snapshot["fact_ids"]),
    )


def prepare_snapshots(engine, connection, versions):
    from .query_reads import QueryReads

    selected = [version for version in versions
                if version.fact.kind == "income_tax_assessment"
                and version.fact.assessment_basis == "computed_provision"]
    if not selected:
        return {}
    with QueryReads.attached_snapshot(engine, connection) as reads:
        return {
            version.id: tax_basis(
                engine, reads, version.fact.year, int(version.fact.period[5:]) // 3,
            )
            for version in selected
        }


def adopted_assessments(engine, reads, year, end, source):
    """Use the same mixed frozen/current publication selection as the report."""
    from .business_queries import BusinessQueries

    if reads.connection.execute(
        "SELECT 1 FROM subject WHERE kind='income_tax_assessment' LIMIT 1",
    ).fetchone() is None:
        return {}, []
    selection = BusinessQueries(engine, reads=reads)._selected_accounting(
        reads.connection, None, str(end), kinds={"income_tax_assessment"}, include_lines=False,
    )["through_period"]
    issues = ([{
        "field": "income_tax_assessment", "reason": "state_selection_unavailable",
        "message": "所得税无凭证结果的正式采用依据尚未建立，不能按零额确认替代",
        "period": str(end),
    }] if selection["unestablished_state_selections"] else [])
    directions = Counter()
    for event in (*selection["voucher_events"], *selection["state_results"]):
        if source == "closed" and event["selection_source"] != "close_manifest":
            continue
        directions[event["calculation_id"]] += event.get("direction", 1)
    calculations = reads.calculations({ident for ident, direction in directions.items()
                                      if direction > 0})
    result = {}
    for item in calculations.values():
        values, fact = item["outcome"]["values"], item["fact_data"]
        coverage = values.get("tax_period_end", fact["period"])
        if fact["year"] != year or fact["assessment_basis"] == "annual_settlement":
            continue
        if coverage <= str(end):
            result.setdefault(coverage, []).append(item)
    return result, issues


def assessment_issues(engine, reads, calculation, *, source):
    values, fact = calculation["outcome"]["values"], calculation["fact_data"]
    if fact["assessment_basis"] != "computed_provision" or values.get("amount_source") == "actual":
        return []
    coverage = values["tax_period_end"]
    current = tax_basis(engine, reads, fact["year"], int(coverage[5:]) // 3, source=source)
    if current["fact_issues"]:
        return list(current["fact_issues"])
    if values.get("calculated_assessment", {}).get("basis_digest") != current["basis_digest"]:
        return [{"field": "income_tax_assessment", "reason": "needs_recalculation",
                 "message": "税前损益来源已变化，需要重新计算并采用季度所得税暂计",
                 "subject_id": calculation["subject_id"], "period": coverage}]
    return []
