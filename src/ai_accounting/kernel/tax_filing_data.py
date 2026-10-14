"""Read filing fields from adopted accounting results in one company snapshot."""

from __future__ import annotations

from collections import Counter
from typing import Literal

from .business_queries import BusinessQueries
from .contracts import Context, KernelError, NeedsInformation
from .query_reads import QueryReads
from .report_snapshot import snapshot_basis
from .reporting_fields import PRE_TAX_PROFIT_FIELDS, filing_field, statement_amount
from .reports import Reports, _periods
from .tax_settlement_reconciliation import reconcile
from .types import YearMonth, canonical, digest, sum_fen

CIT_MAPPING = {
    "form": "A200000",
    "version": "sta-2025-17",
    "effective_from": "2025-10-01",
    "source_url": (
        "https://tianjin.chinatax.gov.cn/11200000000/0300/030004/03000418/"
        "20250714094006730.shtml"
    ),
}
CIT_FILING_ROWS = {"operating_revenue": 1, "operating_cost": 2, "profit_before_tax": 18}
VAT_FIELDS = (
    "net_sales_fen", "taxable_sales_fen", "exempt_sales_fen", "accrued_vat_fen",
    "relief_fen", "payable_vat_fen", "urban_tax_fen", "education_tax_fen",
    "local_education_tax_fen", "surtax_fen", "row_1_fen", "row_9_fen",
    "row_10_fen", "row_15_fen", "row_16_fen", "row_17_fen", "row_18_fen",
    "row_20_fen", "row_21_fen", "row_22_fen", "row_23_fen", "row_24_fen", "row_25_fen",
)


def _issue(field, message, **details):
    return {"field": field, "message": message, **details}


def _references(items):
    """Public references use the existing closed type/value reference grammar."""
    return [
        {"type": kind, "value": value}
        for kind, value in sorted({(kind, value) for kind, value in items if value is not None})
    ]


def _report_references(basis):
    items = [("report_basis_digest", digest(basis).hex())]
    for event in basis["accounting_sources"]:
        for key in ("fact_id", "calculation_id", "result_digest", "voucher_version_id"):
            if event.get(key):
                items.append((key, event[key]))
    for fact in basis["report_facts"]:
        items.extend((("fact_id", fact["fact_id"]), ("fact_digest", fact["fact_digest"])))
    return _references(items)


def _calculation_references(reads, calculations):
    identifiers = {item["id"] for item in calculations}
    reads.prime_calculations(identifiers, ancestors=True)
    closure, queue = set(identifiers), list(identifiers)
    while queue:
        for parent in reads.parents(queue.pop()):
            if parent not in closure:
                closure.add(parent)
                queue.append(parent)
    complete = reads.calculations(closure)
    items = [(key, calculation[key]) for calculation in complete.values()
             for key in ("fact_id", "publication_id", "result_digest", "posting_period")
             if calculation.get(key)]
    items.extend(("calculation_id", ident) for ident in closure)
    facts = {
        row[0] for row in reads.connection.execute(
            "SELECT DISTINCT fact_id FROM dependency_fact "
            "WHERE calculation_id IN (SELECT value FROM json_each(?))",
            (canonical(sorted(closure)),),
        )
    }
    for version in reads.fact_versions(facts).values():
        items.extend((("fact_id", version.id), (version.fact.kind + "_fact_id", version.id)))
    return _references(items)


class TaxFilingData:
    def __init__(self, engine):
        self.engine, self.store = engine, engine.store

    def query(
        self, year: int, quarter: int, *, source: Literal["open", "closed"] = "open"
    ):
        start, end, year_start = _periods(year, quarter)
        if source not in ("open", "closed"):
            raise ValueError("source must be open or closed")
        with QueryReads.snapshot(self.engine) as reads:
            report = Reports(self.engine)._report(
                year, quarter, source=source, connection=reads.connection, reads=reads,
                _pre_tax_fields_only=True,
            )
            fields, mappings = self._cit_fields(reads, report, start, end, year_start)
            vat_fields, vat_mappings, vat_settlements = self._vat_fields(
                reads, start, end, year_start, source
            )
            fields.extend(vat_fields)
            mappings.extend(vat_mappings)
            income_taxes = self._income_tax_assessments(reads, start, end, source)
            issues = list({canonical(item): item for field in fields
                           for item in field["fact_issues"]}.values())
            issues.extend(item for assessment in income_taxes for item in assessment["fact_issues"])
            result = {
                "status": "needs_information" if issues else "ready",
                "company_id": self.store.company_id,
                "database_id": self.store.database_id,
                "year": year,
                "quarter": quarter,
                "source": source,
                "mapping_versions": list({canonical(item): item for item in mappings}.values()),
                "fields": fields,
                "income_tax_assessments": income_taxes,
                "vat_settlement_reconciliations": vat_settlements,
                "fact_issues": issues,
            }
            return result | {"source_basis_digest": digest(result).hex()}

    def _income_tax_assessments(self, reads, start, end, source):
        queries = BusinessQueries(self.engine, reads=reads)
        pending = set()
        if source == "open":
            rows = reads.connection.execute(
                "SELECT c.subject_id,EXISTS(SELECT 1 FROM pending p "
                "WHERE p.subject_id=c.subject_id) pending FROM fact_scope f "
                "JOIN fact_current c ON c.fact_id=f.fact_id "
                "WHERE f.kind='income_tax_assessment' AND f.scope_key=?",
                (f"income-tax:{int(end[:4])}",),
            ).fetchall()
            pending = {row["subject_id"] for row in rows if row["pending"]}
            heads = queries._current_accounting_heads(
                reads.connection, {row["subject_id"] for row in rows}, cutoff=None,
                current_heads=True,
            )
            calculations = reads.calculations({row["id"] for row in heads}).values()
        else:
            selected = queries._selected_accounting(
                reads.connection, None, str(end), kinds={"income_tax_assessment"},
                include_lines=False,
            )["through_period"]
            events = (*selected["voucher_events"], *selected["state_results"])
            directions = Counter()
            for event in events:
                if event["selection_source"] == "close_manifest":
                    directions[event["calculation_id"]] += event.get("direction", 1)
            calculations = reads.calculations({ident for ident, direction in directions.items()
                                               if direction > 0}).values()
        result = []
        for calculation in calculations:
            values = calculation["outcome"]["values"]
            fact = calculation["fact_data"]
            coverage = values.get("tax_period_end") or (
                f"{fact['year']:04d}-12" if fact["assessment_basis"] == "annual_settlement"
                else fact["period"]
            )
            if (fact["assessment_basis"] == "annual_settlement"
                    or int(fact["year"]) != int(end[:4]) or not start <= coverage <= end):
                continue
            issues = ([_issue("income_tax_assessment", "所得税实际来源或原事实变化，须复核后采用",
                              subject_id=calculation["subject_id"])]
                      if calculation["subject_id"] in pending else [])
            reference = values.get("calculated_assessment") or values
            result.append({
                "assessment_id": calculation["subject_id"], "calculation_id": calculation["id"],
                "year": fact["year"], "tax_period_end": str(coverage),
                "status": "needs_information" if issues else "ready",
                "cumulative_assessed_fen": None if issues else values["cumulative_assessed_fen"],
                "current_change_fen": None if issues else values["change_fen"],
                "prepaid_tax_fen": None if issues else values.get("prepaid_tax_fen"),
                "tax_due_fen": None if issues else values.get("tax_due_fen"),
                "amount_source": values.get("amount_source", "provisional"),
                "reference_cumulative_assessed_fen": reference.get("cumulative_assessed_fen"),
                "reference_change_fen": reference.get("change_fen"),
                "source_basis": _calculation_references(reads, [calculation]),
                "settlement_reconciliation": reconcile(reads, calculation)
                if not issues and source == "open" and values.get("actual_declaration") else None,
                "fact_issues": issues,
            })
        return sorted(result, key=lambda item: (item["tax_period_end"], item["assessment_id"]))

    def _cit_fields(self, reads, report, start, end, year_start):
        issues = list(report["fact_issues"])
        mapping_issues = []
        if int(end[:4]) < 2026:
            mapping_issues.append(_issue(
                "cit_mapping_scope", "当前取数支持2026年企业所得税预缴表，历史表式须明确适用版本"
            ))
        try:
            basis = snapshot_basis(self.engine, reads, report)
            sources = _report_references(basis)
        except KernelError as error:
            if error.code != "report_basis_unestablished":
                raise
            sources = []
            issues.append(_issue("report_basis", str(error)))
        sources = _references((item["type"], item["value"]) for item in sources)
        fields = []
        for dimension, first in (("current", start), ("year_to_date", year_start)):
            for name, line in PRE_TAX_PROFIT_FIELDS.items():
                problems = list(issues) + (mapping_issues if dimension == "year_to_date" else [])
                value = None
                if not problems:
                    try:
                        value = statement_amount(report["statements"], line, dimension)
                    except KernelError as error:
                        problems.append(_issue(name, str(error)))
                field_sources = sources + [{
                    "type": "report_field", "value": f"profit_statement.{line}.{dimension}_fen",
                }]
                section = "cit" if dimension == "year_to_date" else "financial_statement"
                if section == "cit":
                    field_sources.extend((
                        {"type": "tax_form_field", "value": f"A200000.{CIT_FILING_ROWS[name]}"},
                        {"type": "mapping_version", "value": CIT_MAPPING["version"]},
                    ))
                fields.append(filing_field(
                    section + "." + name, section, dimension, first, end, value,
                    field_sources, problems,
                ))
        return fields, [dict(CIT_MAPPING)]

    def _tax_calculations(self, reads, end, source, candidates):
        if source == "open":
            subjects = {item["row"]["subject_id"] for item in candidates}
            heads = BusinessQueries(self.engine, reads=reads)._current_accounting_heads(
                reads.connection, subjects, cutoff=None, current_heads=True
            )
            return sorted(
                reads.calculations({row["id"] for row in heads}).values(),
                key=lambda item: item["id"],
            ), []
        selected = BusinessQueries(self.engine, reads=reads)._selected_accounting(
            reads.connection, None, str(end), kinds={"tax_assessment"}, include_lines=False,
        )["through_period"]
        issues = (
            [_issue("tax_assessment", "相关税期存在尚未建立完整采用的核算来源")]
            if selected["unestablished_state_selections"] else []
        )
        events = (*selected["voucher_events"], *selected["state_results"])
        events = tuple(item for item in events if item["selection_source"] == "close_manifest")
        directions = Counter()
        for event in events:
            directions[event["calculation_id"]] += event.get("direction", 1)
        identifiers = {ident for ident, direction in directions.items() if direction > 0}
        calculations = reads.calculations(identifiers)
        if not calculations:
            return [], issues
        publications = {
            row["calculation_id"]: row["sequence"]
            for row in reads.connection.execute(
                "SELECT calculation_id,sequence FROM calculation_publication "
                "WHERE calculation_id IN (SELECT value FROM json_each(?))",
                (canonical(sorted(identifiers)),),
            )
        }
        latest = {}
        for calculation in calculations.values():
            subject = calculation["subject_id"]
            previous = latest.get(subject)
            if previous is None or publications[calculation["id"]] > publications[previous["id"]]:
                latest[subject] = calculation
        return sorted(latest.values(), key=lambda item: item["id"]), issues

    def _vat_fields(self, reads, start, end, year_start, source):
        # The assessment's versioned filing projection supplies every tax formula.
        # This consumer only verifies scope, adoption and exact field presence.
        ranges = tuple(dict.fromkeys(((start, end), (year_start, end))))
        candidates = self._tax_fact_candidates(reads, year_start, end) if source == "open" else ()
        calculations, selection_issues = self._tax_calculations(reads, end, source, candidates)
        pending = self._pending_vat_sources(reads, ranges) if source == "open" else {}
        range_results, references = {}, {}
        fields, mappings, settlements = [], [], []
        for dimension, first in (("current", start), ("year_to_date", year_start)):
            key = first, end
            if key not in range_results:
                range_results[key] = self._vat_range(
                    reads, calculations, first, end, source,
                    selection_issues=selection_issues, candidates=candidates,
                    pending_issues=pending.get(key, ()), references=references,
                )
            amounts, sources, issues, selected, field_issues = range_results[key]
            for calculation in selected:
                values = calculation["outcome"]["values"]
                if dimension == "current" and values.get("actual_declaration"):
                    for name in ("vat", "surtax"):
                        settlements.append({
                            "assessment_id": calculation["subject_id"],
                            "calculation_id": calculation["id"], "tax_name": name,
                            "source_basis": _calculation_references(reads, [calculation]),
                            "reconciliation": reconcile(reads, calculation, tax_name=name)
                            if not issues and source == "open" else None,
                        })
                if all(values.get(key) for key in (
                    "filing_mapping_version", "filing_source_url", "filing_effective_from"
                )):
                    mappings.append({
                        "form": "增值税及附加税费申报表（小规模纳税人适用）",
                        "version": values["filing_mapping_version"],
                        "effective_from": values["filing_effective_from"],
                        "source_url": values["filing_source_url"],
                    })
            for name in VAT_FIELDS:
                field = filing_field(
                    "vat_and_surtax." + name, "vat_and_surtax", dimension, first, end,
                    amounts.get(name), sources, issues + field_issues.get(name, []),
                )
                modes, reference_amounts = set(), []
                for calculation in selected:
                    values = calculation["outcome"]["values"]
                    modes.add("actual" if name in values.get("actual_fields", ()) else "calculated")
                    reference = values.get("calculated_assessment")
                    if reference is None and values.get("amount_source") != "actual":
                        reference = values
                    reference = reference or {}
                    amount = reference.get("filing_fields", {}).get(name, reference.get(name))
                    reference_amounts.append(amount)
                field["value_source"] = (
                    "accounting" if name in {"net_sales_fen", "accrued_vat_fen"}
                    else "actual" if modes == {"actual"} else "mixed" if len(modes) > 1
                    else "calculated"
                )
                field["calculated_value_fen"] = (
                    sum_fen(reference_amounts) if reference_amounts
                    and all(type(amount) is int for amount in reference_amounts)
                    else None
                )
                field["source_basis_digest"] = digest({key: value for key, value in field.items()
                                                      if key != "source_basis_digest"}).hex()
                fields.append(field)
        return fields, mappings, settlements

    def _tax_fact_candidates(self, reads, start, end):
        scopes = [f"tax-assessment:{YearMonth.from_ordinal(month)}"
                  for month in range(start.ordinal, end.ordinal + 1)]
        candidates = {}
        for row in reads.connection.execute(
            "SELECT f.fact_id id,c.subject_id,a.calculation_id current_calculation_id,"
            "f.scope_key,EXISTS(SELECT 1 FROM pending p WHERE p.subject_id=c.subject_id) pending "
            "FROM fact_scope f JOIN json_each(?) scopes ON scopes.value=f.scope_key "
            "JOIN fact_current c ON c.fact_id=f.fact_id "
            "LEFT JOIN calculation_current a ON a.subject_id=c.subject_id "
            "WHERE f.kind='tax_assessment'",
            (canonical(scopes),),
        ):
            candidate = candidates.setdefault(row["id"], {"row": row, "periods": set()})
            candidate["periods"].add(YearMonth(row["scope_key"].split(":", 1)[1]).ordinal)
        reads.fact_versions(candidates)
        return tuple(candidates.values())

    def _vat_range(
        self, reads, calculations, start, end, source, *, selection_issues, candidates,
        pending_issues, references,
    ):
        expected = set(range(start.ordinal, end.ordinal + 1))
        covered, selected = set(), []
        issues = list(selection_issues)
        for calculation in calculations:
            fact = calculation["fact_data"]
            first, last = YearMonth(fact["period_start"][:7]), YearMonth(fact["period_end"][:7])
            overlap = expected & set(range(first.ordinal, last.ordinal + 1))
            if not overlap:
                continue
            if first < start or last > end:
                issues.append(_issue(
                    "tax_period_scope", "已采用税期跨越取数范围，不能按月份拆分已确认税额",
                    subject_id=calculation["subject_id"], calculation_id=calculation["id"],
                ))
                continue
            if int(first[5:]) not in (1, 4, 7, 10) or last.ordinal != first.ordinal + 2:
                issues.append(_issue(
                    "tax_period_scope",
                    "季度申报须采用同覆盖期的季度核算，不能拼接月度或跨季度片段",
                    subject_id=calculation["subject_id"], calculation_id=calculation["id"],
                ))
                continue
            if covered & overlap:
                issues.append(_issue("tax_period_scope", "本次取数范围存在重复采用的税期"))
            covered.update(overlap)
            selected.append(calculation)
        if expected != covered:
            issues.append(_issue(
                "tax_assessment", "税期尚缺完整采用的增值税及附加核算，不推定缺少月份为零"
            ))
        if source == "open":
            adopted = {item["subject_id"]: item for item in selected}
            for candidate in candidates:
                if not expected & candidate["periods"]:
                    continue
                row = candidate["row"]
                version = reads.fact_version(row["id"])
                old = adopted.get(row["subject_id"])
                if (row["pending"] or old is None or old["fact_id"] != version.id
                        or old["id"] != row["current_calculation_id"]):
                    issues.append(_issue(
                        "tax_assessment", "相关税期事实尚未正式采用或需要重算",
                        subject_id=row["subject_id"], fact_id=version.id,
                    ))
            issues.extend(pending_issues)
        amounts, field_issues = {}, {}
        for calculation in selected:
            values = calculation["outcome"]["values"]
            filing = values.get("filing_fields")
            if not isinstance(filing, dict) or not filing:
                issues.append(_issue(
                    "filing_fields", "已采用税期结果缺少当前受支持表式的字段依据，需先复核该来源",
                    subject_id=calculation["subject_id"], calculation_id=calculation["id"],
                ))
                continue
            for problem in values.get("filing_issues", ()):
                name = problem.get("amount_field", problem["field"])
                if name in VAT_FIELDS:
                    field_issues.setdefault(name, []).append(dict(problem))
                else:
                    issues.append(dict(problem))
            if not all(values.get(key) for key in (
                "filing_mapping_version", "filing_source_url", "filing_effective_from"
            )):
                issues.append(_issue("filing_mapping", "已采用税期结果缺少版本化申报表依据"))
            for name in VAT_FIELDS:
                if (name == "relief_fen" and values.get("amount_source") == "actual"
                        and name not in values.get("actual_fields", ())):
                    field_issues.setdefault(name, []).append(_issue(
                        "vat_declaration_actual.exemption_relief_fen",
                        "实际免税转回金额未建立，不把税额调整推定为免税",
                        amount_field=name, subject_id=calculation["subject_id"],
                    ))
                value = filing.get(name, values.get(name))
                if type(value) is not int:
                    message = (
                        "该申报栏缺少已预缴的完整依据，不以零额填报" if name == "row_21_fen" else
                        "应补退申报栏尚缺完整缴款及抵减依据，不能用应纳计提额替代"
                        if name in {"row_22_fen", "row_23_fen", "row_24_fen", "row_25_fen"} else
                        "该申报字段没有明确的整数分金额"
                    )
                    field_issues.setdefault(name, []).append(_issue(
                        name, message, amount_field=name,
                        subject_id=calculation["subject_id"], calculation_id=calculation["id"],
                    ))
                    continue
                amounts[name] = sum_fen((amounts.get(name, 0), value))
        reference_key = frozenset(item["id"] for item in selected)
        if reference_key not in references:
            references[reference_key] = _calculation_references(reads, selected)
        return amounts, references[reference_key], issues, selected, field_issues

    def _pending_vat_sources(self, reads, ranges):
        from .domains.taxes import VAT_CALCULATION_KINDS

        candidates = reads.connection.execute(
            "SELECT c.fact_id,c.subject_id FROM fact_current c "
            "JOIN subject s ON s.id=c.subject_id "
            "LEFT JOIN calculation_current a ON a.subject_id=c.subject_id "
            "LEFT JOIN calculation result ON result.id=a.calculation_id "
            "WHERE s.kind IN (SELECT value FROM json_each(?)) AND "
            "(result.id IS NULL OR result.fact_id<>c.fact_id OR EXISTS("
            "SELECT 1 FROM pending p WHERE p.subject_id=c.subject_id))",
            (canonical(VAT_CALCULATION_KINDS),),
        ).fetchall()
        issues = {key: [] for key in ranges}
        versions = reads.fact_versions(row["fact_id"] for row in candidates)
        end = max(last for _, last in ranges)
        for row in candidates:
            version = versions[row["fact_id"]]
            periods = self._pending_source_periods(reads, version, end)
            for start, last in ranges:
                if periods is None or any(start <= period <= last for period in periods):
                    issues[start, last].append(_issue(
                        "vat_source", "该税期业务或税点依据尚未正式采用，不能沿用原税期金额",
                        subject_id=version.subject_id, fact_id=version.id,
                    ))
        return issues

    def _pending_source_periods(self, reads, version, end):
        """Known intervals, or None when the source cannot yet rule out a range."""
        from .tax_point_basis import (
            resolve_business_tax_point,
            resolve_tax_point,
            tax_basis_reads,
            tax_basis_source_reads,
        )

        fact = version.fact
        if fact.kind in {"service_sale", "advance", "advance_fulfillment", "asset_disposal"}:
            if getattr(fact, "side", None) == "supplier":
                return ()
            gross_field = {
                "service_sale": "gross_fen", "asset_disposal": "gross_proceeds_fen",
                "advance": "amount_fen", "advance_fulfillment": "fulfilled_gross_fen",
            }[fact.kind]
            gross = getattr(fact, gross_field, None)
            if gross == 0 and getattr(fact, "disposal_kind", None) == "scrap":
                return ()
            if gross is None:
                return None
            read = tax_basis_reads(fact.kind, version.subject_id)[0]
            selections = {read: self.store.select(reads.connection, read)}
            dependencies = tax_basis_source_reads(
                Context(selections), fact.kind, version.subject_id
            )
            selections.update(self.store.select_many(reads.connection, dependencies))
            try:
                context = Context(selections)
                if fact.kind == "asset_disposal":
                    resolution = resolve_tax_point(
                        context, fact.kind, version.subject_id, gross, max(end, fact.period)
                    )
                else:
                    resolution = resolve_business_tax_point(
                        version, context, gross, as_of=max(end, fact.period)
                    )
            except NeedsInformation:
                # An unresolved tax point is not proof that a pending source is irrelevant.
                return None
            if resolution is None:
                tax_period = fact.tax_obligation_period
                return (tax_period,) if tax_period is not None else None
            return tuple(interval.period for interval in resolution.intervals)
        if hasattr(fact, "allocations") and fact.kind in {
            "payment", "cash_payment", "platform_payment", "payroll_reserve_payment"
        } and not any(item.source_kind == "service_sale" for item in fact.allocations):
            return ()
        return (fact.period,)
