"""The fixed three-statement snapshot uses one company transaction and selection."""

from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    content_digest,
    parse_fen,
    schema_document,
    validate_contract,
)

from ._cashflow import cash
from ._financial_cells import Issues, add, scaled
from ._financial_positions import cash_before, positions, previous
from ._financial_profit import pretax_slices, profit
from ._financial_selection import Selection
from ._profit import pretax_basis_digest
from ._values import fail

MAPPING_V1 = "small-enterprise-financial-1"
CURRENT_MAPPING_VERSION = MAPPING_V1


def normalize(scope, context):
    validate_contract("core.schema.json", "LedgerScope", scope)
    first, last = scope["period_from"], scope["period_to"]
    if scope["company_id"] != context.company_id:
        fail("invalid_input", "报表范围与同一公司读取上下文不一致。")
    if "object_ids" in scope or first[:4] != last[:4] or first > last:
        fail("invalid_input", "三表只支持整家公司、同年单月或完整自然季度。")
    quarter = int(first[5:]) in (1, 4, 7, 10) and int(last[5:]) == int(first[5:]) + 2
    if first != last and not quarter:
        fail("invalid_input", "报表范围必须是单月或完整自然季度，不能自动补月。")
    if int(first[:4]) < 2013:
        fail("unsupported_business", "当前报表映射适用2013年起的小企业会计准则期间。")
    result = {key: value for key, value in scope.items() if key != "expected_head"}
    if scope["view"] == "as_posted":
        if scope.get("as_of", last) != last:
            fail("invalid_input", "历史采用报表的截止月必须与报告末月一致。")
        result["as_of"] = last
    elif "as_of" in scope:
        fail("invalid_input", "当前完整目标口径不接受历史采用截止月。")
    if last < context.start_period:
        raise ContractError(
            "needs_information",
            "接账前的独立月份缺少逐月正式来源。",
            fact_issues=[
                {
                    "code": "missing_prior_month",
                    "field": "/period_from",
                    "meaning": "接账前独立月份",
                    "message": "接续累计额不能均摊或反推为此前单月。",
                    "allowed_precision": ["month", "source"],
                    "reusable_sources": [],
                    "blocking_scope": "none",
                }
            ],
        )
    return result


def _check(code, left, right):
    difference = add(left, scaled(right, -1))
    if difference["state"] == "unestablished":
        return {"code": code, "status": "unknown", "issue_indexes": difference["issue_indexes"]}
    amount = difference["amount_fen"]
    return {
        "code": code,
        "status": "pass" if amount == "0" else "fail",
        "difference_fen": amount,
        "issue_indexes": [],
    }


def _table(definition, columns):
    schema = schema_document("reporting.schema.json")["$defs"][definition]["properties"]
    return {
        key: {
            "name": row["properties"]["name"]["const"],
            **{column: values[int(key)] for column, values in columns.items()},
        }
        for key, row in schema.items()
    }


def snapshot(scope, context, *, mapping_version=None):
    version = mapping_version or CURRENT_MAPPING_VERSION
    if version != MAPPING_V1:
        fail("unsupported_policy", "该历史报告映射版本尚未装配，不能改用另一版本。")
    return _snapshot_v1(scope, context)


def _snapshot_v1(scope, context):
    normalized = normalize(scope, context)
    repo = context.repository("ledger")
    first, last = normalized["period_from"], normalized["period_to"]
    year = last[:4] + "-01"
    issues = Issues()
    selection = Selection(repo, normalized["view"], last, context.start_period, issues)
    begin = positions(selection, previous(year), "beginning")
    ending = positions(selection, last, "ending")
    current = profit(selection, first, last, "current")
    annual = current if first == year else profit(selection, year, last, "year_to_date")
    current_cash = cash(selection, first, last, "current")
    annual_cash = (
        deepcopy(current_cash) if first == year else cash(selection, year, last, "year_to_date")
    )
    current_cash[21] = cash_before(selection, first, last, "current_cash_begin")
    annual_cash[21] = begin[1]
    current_cash[22] = annual_cash[22] = ending[1]
    checks = [
        _check("balance_beginning", begin[30], begin[53]),
        _check("balance_ending", ending[30], ending[53]),
        _check("profit_current", current[32], add(current[30], scaled(current[31], -1))),
        _check("profit_ytd", annual[32], add(annual[30], scaled(annual[31], -1))),
        _check("cash_current", current_cash[22], add(current_cash[21], current_cash[20])),
        _check("cash_ytd", annual_cash[22], add(annual_cash[21], annual_cash[20])),
        _check("cash_to_balance_current", current_cash[22], ending[1]),
        _check("cash_to_balance_ytd", annual_cash[22], ending[1]),
        _check(
            "cash_to_postings_current",
            current_cash[20],
            add(ending[1], scaled(current_cash[21], -1)),
        ),
        _check(
            "cash_to_postings_ytd", annual_cash[20], add(ending[1], scaled(annual_cash[21], -1))
        ),
    ]
    prior = selection.prior("profit", year, last)
    proven = all(prior[line]["state"] == "known" for line in (1, 2, 30))
    prior_source = (
        (selection.continuation or selection.opening)
        if proven and year < context.start_period
        else None
    )
    prior_kind = (
        ("year_to_date_fen" if selection.continuation else "new_company_zero")
        if prior_source
        else None
    )
    pretax_digest = pretax_basis_digest(
        year,
        last,
        context.start_period,
        pretax_slices(selection, year, last),
        tuple(parse_fen(prior[line]["amount_fen"]) for line in (1, 2, 30)) if proven else None,
        prior_source,
        prior_kind,
        view=selection.view,
    )
    pre_tax = {
        "period_from": year,
        "period_to": last,
        "basis_digest": pretax_digest,
        **{
            name: annual[line]
            for name, line in (
                ("operating_revenue", 1),
                ("operating_cost", 2),
                ("profit_before_tax", 30),
            )
        },
    }
    result = {
        "format": "ai-accounting-financial-snapshot/1",
        "company_id": context.company_id,
        "database_id": context.database_id,
        "head": context.head,
        "scope": normalized,
        "accounting_standard": "small_enterprise",
        "mapping_version": MAPPING_V1,
        "status": "partial"
        if issues.values or any(check["status"] != "pass" for check in checks)
        else "ready",
        "period_bounds": {
            "current_from": first,
            "current_to": last,
            "year_from": year,
            "year_to": last,
            "current_cash_begin_before": previous(first),
            "balance_begin_before": previous(year),
        },
        "balance_sheet": _table("BalanceSheet", {"beginning": begin, "ending": ending}),
        "profit_statement": _table("ProfitStatement", {"current": current, "year_to_date": annual}),
        "cash_flow_statement": _table(
            "CashFlowStatement", {"current": current_cash, "year_to_date": annual_cash}
        ),
        "pre_tax": pre_tax,
        "checks": checks,
        "issues": issues.values,
    }
    selected_sources = sorted(
        (selection.source(selection.heads[key]) for key in selection.used),
        key=lambda source: (source["business_id"], source["result_slot"], source["result_id"]),
    )
    digest = content_digest(
        {
            "snapshot": {key: value for key, value in result.items() if key != "head"},
            "sources": selected_sources,
        }
    )
    selected = {
        "selection_digest": digest,
        "closed_periods": repo.closed_periods(year, last, start_period=context.start_period),
        "supplement_periods": sorted(
            {
                selection.heads[key]["posting_period"]
                for key in selection.used
                if selection.heads[key].get("baseline_result_id")
                and year <= selection.heads[key]["posting_period"] <= last
            }
        ),
    }
    for name in ("opening", "continuation"):
        head = getattr(selection, name)
        if head:
            selected[name + "_business_id"] = head["business_id"]
    result["selection"] = selected
    if scope.get("expected_head", digest) != digest:
        fail("snapshot_changed", "同一报表范围采用的正式结果已变化，请重新读取。")
    validate_contract("reporting.schema.json", "FinancialSnapshot", result)
    return result
