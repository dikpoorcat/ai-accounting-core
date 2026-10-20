"""The single fixed small-enterprise total-row equations for facts and reports."""

from collections.abc import Mapping

from ai_accounting_modular.contracts.public import (
    AmountCell,
    ContractError,
    format_fen,
    parse_fen,
    sum_fen,
    validate_contract,
)

_BALANCE = {
    15: tuple((i, 1) for i in (*range(1, 10), 14)),
    20: ((18, 1), (19, -1)),
    29: tuple((i, 1) for i in (16, 17, *range(20, 29))),
    30: ((15, 1), (29, 1)),
    41: tuple((i, 1) for i in range(31, 41)),
    46: tuple((i, 1) for i in range(42, 46)),
    47: ((41, 1), (46, 1)),
    52: tuple((i, 1) for i in range(48, 52)),
    53: ((47, 1), (52, 1)),
}
_PROFIT = {
    21: ((1, 1), (2, -1), (3, -1), (11, -1), (14, -1), (18, -1), (20, 1)),
    30: ((21, 1), (22, 1), (24, -1)),
    32: ((30, 1), (31, -1)),
}
_CASH = {
    7: ((1, 1), (2, 1), (3, -1), (4, -1), (5, -1), (6, -1)),
    13: ((8, 1), (9, 1), (10, 1), (11, -1), (12, -1)),
    19: ((14, 1), (15, 1), (16, -1), (17, -1), (18, -1)),
    20: ((7, 1), (13, 1), (19, 1)),
    22: ((21, 1), (20, 1)),
}


def _totals(rows, equations, last_line):
    if not isinstance(rows, Mapping):
        raise ContractError("invalid_input", "报表合计输入须为行号到AmountCell的映射。")
    working = dict(rows)
    for line, cell in working.items():
        if type(line) is not int or not 1 <= line <= last_line:
            raise ContractError("invalid_input", f"报表行号须为1至{last_line}的整数。")
        validate_contract("reporting.schema.json", "AmountCell", cell)
    required = {source for terms in equations.values() for source, _ in terms} - equations.keys()
    missing = sorted(required - working.keys())
    if missing:
        raise ContractError(
            "invalid_input", f"固定报表合计缺少基础行：{','.join(map(str, missing))}。"
        )
    result = {}
    for line, terms in equations.items():
        issues = {
            issue
            for source, _ in terms
            if working[source]["state"] == "unestablished"
            for issue in working[source]["issue_indexes"]
        }
        if issues:
            cell = {"state": "unestablished", "issue_indexes": sorted(issues)}
        else:
            try:
                value = sum_fen(
                    sign * parse_fen(working[source]["amount_fen"]) for source, sign in terms
                )
            except ValueError as error:
                raise ContractError(
                    "invalid_input", f"报表第{line}行合计超出有符号64位整数分范围。"
                ) from error
            cell = {"state": "known", "amount_fen": format_fen(value)}
        working[line] = result[line] = cell
    return result


def balance_totals(rows: Mapping[int, AmountCell]) -> dict[int, AmountCell]:
    """Return BS15/20/29/30/41/46/47/52/53 from their fixed base rows."""
    return _totals(rows, _BALANCE, 53)


def profit_totals(rows: Mapping[int, AmountCell]) -> dict[int, AmountCell]:
    """Return PL21/30/32, propagating only issues from their actual inputs."""
    return _totals(rows, _PROFIT, 32)


def cash_totals(rows: Mapping[int, AmountCell]) -> dict[int, AmountCell]:
    """Return CF7/13/19/20/22; CF21 remains an explicitly supplied balance."""
    return _totals(rows, _CASH, 22)
