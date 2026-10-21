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
from ._balance_selection import BalanceSelection
from ._financial_calculation import ACCOUNT_FIELDS as _ACCOUNT_FIELDS
from ._financial_calculation import amount as _amount
from ._financial_cells import Issues
from ._read_support import json_value
from ._types import AccountBalance, AccountBalanceKey, FinancialBalances
from ._values import fail


def _fields(names):
    if isinstance(names, str | bytes) or not isinstance(names, Sequence):
        fail("invalid_input", "财务余额须指定明确字段名称。")
    known = {name for name, _, _ in _ACCOUNT_FIELDS}
    if any(not isinstance(name, str) or name not in known for name in names):
        fail("invalid_input", "所选财务余额字段没有明确的科目读取声明。")
    if len(set(names)) != len(names):
        fail("invalid_input", "财务余额字段不能重复。")
    return {name: (codes, sign) for name, codes, sign in _ACCOUNT_FIELDS if name in names}


def summary_inputs(names):
    fields = _fields(names)
    return ReadInputs(
        result_kinds=("opening.package",),
        account_codes=tuple(sorted({code for codes, _ in fields.values() for code in codes})),
    )


def _sources(selection):
    return tuple(
        sorted(
            (selection.source(selection.heads[key]) for key in selection.used),
            key=lambda ref: (ref["business_id"], ref["result_slot"], ref["result_id"]),
        )
    )


def financial_summary(scope, read):
    from ._financial_projection import public_summary

    return public_summary(scope, read)


def financial_balances(scope, names, read):
    fields = _fields(names)
    normalized = _financial.normalize(scope, read)
    last = normalized["period_to"]
    issues = Issues()
    keys = tuple(
        AccountBalanceKey(code)
        for code in sorted({code for codes, _ in fields.values() for code in codes})
    )
    selector = BalanceSelection(
        read.repository("ledger"), normalized["view"], last, read.start_period, issues, keys
    )
    values = {
        name: _amount(selector, last, codes, sign=sign, field="/" + name)
        for name, (codes, sign) in fields.items()
    }
    sources = _sources(selector)
    version = content_digest(
        json_value(
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "scope": normalized,
                "values": values,
                "issues": issues.values,
                "sources": sources,
            }
        )
    )
    if scope.get("expected_head", version) != version:
        fail("snapshot_changed", "所选财务余额已经变化，请重新读取。")
    return FinancialBalances(version, normalized, values, tuple(issues.values), sources)


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
    selector = BalanceSelection(
        read.repository("ledger"),
        selected.view,
        selected.as_of or period,
        read.start_period,
        issues,
        tuple(keys),
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
