"""Small financial reads share the statement selection and balance cube."""

from collections.abc import Sequence

from ai_accounting_modular.contracts.public import (
    ReadInputs,
    content_digest,
    parse_fen,
    query_selection,
    validate_contract,
)

from . import _financial
from ._balance_cube import balance_cube, previous
from ._balances import sum_amounts
from ._closing import closed_report
from ._financial_cells import Issues, add, known
from ._financial_positions import positions
from ._financial_profit import profit
from ._financial_selection import Selection
from ._read_support import json_value
from ._types import AccountBalance, AccountBalanceKey, FinancialSummary
from ._values import fail

_ACCOUNT_FIELDS = (
    ("bank", ("1002",), 1),
    ("cash", ("1001",), 1),
    ("payment_platform", ("1012",), 1),
    ("fixed_cost", ("1601",), 1),
    ("fixed_accumulated", ("1602",), -1),
    ("fixed_net", ("1601", "1602"), 1),
    ("intangible_cost", ("1701",), 1),
    ("intangible_accumulated", ("1702",), -1),
    ("intangible_net", ("1701", "1702"), 1),
    ("long_term_cost", ("1601", "1701", "1604", "189901", "4301"), 1),
    ("long_term_accumulated", ("1602", "1702"), -1),
    ("long_term_net", ("1601", "1602", "1701", "1702", "1604", "189901", "4301"), 1),
)


def summary_inputs(names):
    known = {name: codes for name, codes, _ in _ACCOUNT_FIELDS}
    if any(name not in known for name in names):
        fail("invalid_input", "所选财务余额字段没有明确的科目读取声明。")
    return ReadInputs(
        result_kinds=("opening.package",),
        account_codes=tuple(sorted({code for name in names for code in known[name]})),
    )


def _sources(selection):
    return tuple(
        sorted(
            (selection.source(selection.heads[key]) for key in selection.used),
            key=lambda ref: (ref["business_id"], ref["result_slot"], ref["result_id"]),
        )
    )


def _amount(selection, cutoff, codes, *, object_id=None, sign=1, field):
    cube = balance_cube(selection, cutoff)
    if cube is None:
        return selection.issues.unknown(
            field, "正式期初及指定月末余额", "缺少该比较时点的正式期初，不能把余额推定为零。"
        )
    return known(
        sign
        * sum_amounts(
            amount
            for (code, _, obj), amount in cube.items()
            if code in codes and (object_id is None or obj == object_id)
        )
    )


def financial_summary(scope, read):
    normalized = _financial.normalize(scope, read)
    repo = read.repository("ledger")
    first, last = normalized["period_from"], normalized["period_to"]
    issues = Issues()
    selection = Selection(repo, normalized["view"], last, read.start_period, issues)
    if normalized["view"] == "as_posted" and repo.is_closed(last, start_period=read.start_period):
        saved = closed_report(normalized, read)
        ending = {int(key): row["ending"] for key, row in saved["balance_sheet"].items()}
        current = {int(key): row["current"] for key, row in saved["profit_statement"].items()}
        issues.values = list(saved["issues"])
    else:
        ending = positions(selection, last, "ending")
        current = profit(selection, first, last, "current")
    values = {
        "revenue": add(*(current[n] for n in (1, 20, 22))),
        "expense": add(*(current[n] for n in (2, 3, 11, 14, 18, 24, 31))),
        "net_profit": current[32],
        **{
            name: ending[number]
            for name, number in (
                ("assets", 30),
                ("liabilities", 47),
                ("current_liabilities", 41),
                ("non_current_liabilities", 46),
                ("equity", 52),
                ("capital", 48),
                ("retained_profit", 51),
            )
        },
    }
    for name, codes, sign in _ACCOUNT_FIELDS:
        values[name] = _amount(selection, last, codes, sign=sign, field="/" + name)
    sources = _sources(selection)
    identity = {
        "company_id": read.company_id,
        "database_id": read.database_id,
        "scope": normalized,
        "values": values,
        "issues": issues.values,
        "sources": sources,
    }
    version = content_digest(json_value(identity))
    if scope.get("expected_head", version) != version:
        fail("snapshot_changed", "所选财务摘要已经变化，请重新读取。")
    return FinancialSummary(
        version, normalized, **values, issues=tuple(issues.values), sources=sources
    )


def account_balances(keys, period, read, selection=None):
    if isinstance(keys, str | bytes) or not isinstance(keys, Sequence) or len(keys) > 500:
        fail("invalid_input", "一次最多查询500项明确科目和对象余额。")
    validate_contract("core.schema.json", "Month", period)
    for key in keys:
        if not isinstance(key, AccountBalanceKey):
            fail("invalid_input", "余额查询须指定科目和可选对象。")
        validate_contract("reporting.schema.json", "AccountCode", key.account_code)
        if key.object_id is not None:
            validate_contract("core.schema.json", "Id", key.object_id)
    if len(set(keys)) != len(keys):
        fail("invalid_input", "余额查询不能重复同一科目和对象。")
    selected = query_selection(read, selection)
    if selected.view == "as_posted" and selected.as_of != period:
        fail("invalid_input", "账户历史月余额的采用截止月必须等于所查月份。")
    if not keys:
        read.repository("ledger")
        return ()
    issues = Issues()
    selector = Selection(
        read.repository("ledger"),
        selected.view,
        selected.as_of or period,
        read.start_period,
        issues,
    )
    before = previous(period)
    result = []
    # Loading these two cubes once also loads their selected journals and origins once.
    balance_cube(selector, before)
    balance_cube(selector, period)
    firsts = {
        row["query_index"]: row
        for row in selector.repo.read_first_recorded(keys, as_of=selected.as_of)
    }
    for row in firsts.values():
        selector.heads[row["result_id"]] = row
        selector.source(row)
    opening = selector.opening_rows() or []
    for index, key in enumerate(keys):
        cells = {
            name: _amount(
                selector,
                cutoff,
                (key.account_code,),
                object_id=key.object_id,
                field=f"/accounts/{key.account_code}/{key.object_id or 'all'}/{name}",
            )
            for name, cutoff in (("opening", before), ("ending", period))
        }

        origins = [firsts[index]["first_period"]] if index in firsts else []
        if any(
            row["account_code"] == key.account_code
            and (key.object_id is None or row.get("object_id") == key.object_id)
            and (parse_fen(row["debit_fen"]) or parse_fen(row["credit_fen"]))
            for row in opening
        ):
            origins.append(read.start_period)
        result.append((key, cells, min(origins, default=None)))
    sources = _sources(selector)
    return tuple(
        AccountBalance(
            key, **cells, first_recorded_period=first, issues=tuple(issues.values), sources=sources
        )
        for key, cells, first in result
    )
