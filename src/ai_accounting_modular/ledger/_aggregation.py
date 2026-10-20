"""Current complete targets supply tax sales and the shared profit calculation."""

import json

from ai_accounting_modular.contracts.public import (
    PretaxView,
    TaxSaleView,
    format_fen,
    parse_fen,
    validate_contract,
)
from ai_accounting_modular.ledger.opening.public import is_zero_origin

from ._balances import sum_amounts
from ._formal_output import target_vouchers
from ._profit import has_unmapped_line, pretax_basis_digest, pretax_values, profit_rows
from ._publications import month_range
from ._values import fail


def tax_sales(repo, period_from, period_to):
    month_range(period_from, period_to)
    result = []
    for row in sorted(
        repo.tax_sales(period_from, period_to),
        key=lambda row: (
            row["tax_period"],
            row["source_business_id"],
            row["business_id"],
            row["result_slot"],
            row["item_no"],
        ),
    ):
        sale = {
            key: row[key]
            for key in (
                "source_business_id",
                "tax_period",
                "nominal_rate",
                "tax_base_rate",
                "payable_rate",
                "invoice_kind",
                "filing_bucket",
            )
        }
        sale.update(
            net_sales_fen=format_fen(row["net_sales_fen"]),
            vat_fen=format_fen(row["vat_fen"]),
            exemption_eligible=bool(row["exemption_eligible"]),
            ranges=json.loads(row["ranges_json"]),
        )
        result.append(TaxSaleView(sale, repo.source(row["result_id"])))
    return tuple(result)


def _basis(head, plans):
    key = head["business_id"], head["result_slot"]
    if key in plans:
        return plans[key]["target"]["kind"], plans[key]["basis"]
    kind = json.loads(head["target_json"])["kind"]
    if kind not in ("opening.package", "reports.continuation"):
        return kind, None
    basis = json.loads(head["basis_json"])
    validate_contract("business-facts.schema.json", "AdoptedBasis", basis)
    return kind, basis


def _prior_profit(heads, plans, period_from, period_to, start_period):
    """Only a proven new-company zero or an exact continuation span supplies prior flow."""
    if period_from >= start_period:
        return (0, 0, 0), None, None
    openings, continuations = [], []
    for head in heads:
        kind, basis = _basis(head, plans)
        if kind == "opening.package" and head["business_period"] == start_period:
            openings.append((head, basis))
        if kind == "reports.continuation" and basis["input"]["bookkeeping_start"] == start_period:
            continuations.append((head, basis))
    if len(openings) > 1 or len(continuations) > 1:
        fail("dependency_conflict", "税前利润的接账期初或接续依据不唯一。")
    if period_to >= start_period and openings:
        opening, basis = openings[0]
        if continuations:
            continuation, adopted = continuations[0]
            data = adopted["input"]
            if data["opening_package_id"] != opening["business_id"]:
                fail("dependency_conflict", "报表接续引用的期初与正式接账起点不一致。")
            quarter_start = f"{start_period[:4]}-{((int(start_period[5:]) - 1) // 3) * 3 + 1:02d}"
            column = (
                "year_to_date_fen"
                if period_from == start_period[:4] + "-01"
                else ("quarter_to_date_fen" if period_from == quarter_start else None)
            )
            if column:
                prior = data["prior_profit"]
                if len(prior) != 32 or {row["line"] for row in prior} != set(range(1, 33)):
                    fail("storage_corrupt", "正式接续利润表行号不完整或重复。")
                rows = {row["line"]: parse_fen(row[column]) for row in prior}
                values = pretax_values(rows)
                if (
                    rows[21] != sum_amounts((values[2], -rows[22], rows[24]))
                    or rows[30] != values[2]
                    or rows[32] != sum_amounts((rows[30], -rows[31]))
                ):
                    fail("storage_corrupt", "正式接续利润表的损益勾稽不一致。")
                return values, continuation, column
        elif period_from[:4] == start_period[:4]:
            key = opening["business_id"], opening["result_slot"]
            target = plans[key]["target"] if key in plans else json.loads(opening["target_json"])
            if is_zero_origin(target, basis):
                return (0, 0, 0), opening, "new_company_zero"
    return None, None, None


def pre_tax(repo, plans, period_from, period_to, start_period):
    month_range(period_from, period_to)
    if period_from[:4] != period_to[:4]:
        fail("invalid_input", "税前利润取数范围必须在同一年内。")
    validate_contract("core.schema.json", "Month", start_period)
    # Include the cutover basis only when this request actually crosses it.
    heads = repo.period_heads(period_from, period_to)
    persisted = [head for head in heads if head["result_id"] not in repo.heads]
    journals = target_vouchers(repo.base, persisted)
    totals = [0, 0, 0]
    slices, sources = [], {}
    for head in heads:
        key = head["business_id"], head["result_slot"]
        lines = (
            plans[key]["lines"]
            if head["result_id"] in repo.heads
            else (journals[head["result_id"]] or {}).get("lines", [])
        )
        if has_unmapped_line(lines):
            fail("storage_corrupt", "正式发生包含未映射的非零科目，不能将其作为零损益取数。")
        rows = profit_rows(lines)
        if not any(value for line, value in rows.items() if line != 31):
            continue
        values = pretax_values(rows)
        totals = [sum_amounts((old, amount)) for old, amount in zip(totals, values, strict=True)]
        source = repo.source(head["result_id"])
        sources[key] = source
        slices.append(
            {
                "business_id": key[0],
                "result_slot": key[1],
                "period": head["business_period"],
                "values": list(values),
            }
        )
    prior, basis_head, prior_kind = _prior_profit(
        heads, plans, period_from, period_to, start_period
    )
    issues = ()
    if prior is None:
        issues = (
            {
                "code": "missing_continuation_profit",
                "field": "/data/prior_profit",
                "meaning": "所查询范围内接账前的损益",
                "message": (
                    f"账面从{start_period}开始，缺少覆盖{period_from}至接账前的正式利润接续依据。"
                ),
                "allowed_precision": ["month", "fen", "source"],
                "reusable_sources": [],
                "blocking_scope": "none",
            },
        )
    else:
        totals = [sum_amounts((old, amount)) for old, amount in zip(totals, prior, strict=True)]
    if basis_head is not None:
        key = basis_head["business_id"], basis_head["result_slot"]
        sources[key] = repo.source(basis_head["result_id"])
    digest = pretax_basis_digest(
        period_from, period_to, start_period, slices, prior, basis_head, prior_kind
    )
    fields = {"period_from": period_from, "period_to": period_to, "basis_digest": digest}
    for name, amount in zip(
        ("operating_revenue", "operating_cost", "profit_before_tax"), totals, strict=True
    ):
        fields[name] = (
            {"state": "unestablished", "issue_indexes": [0]}
            if issues
            else {"state": "known", "amount_fen": format_fen(amount)}
        )
    return PretaxView(fields, tuple(sources[key] for key in sorted(sources)), issues)
