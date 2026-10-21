"""Independent fixed-formula numbers and conservative unknown propagation."""

from copy import deepcopy
from types import MappingProxyType

import pytest

from ai_accounting_modular.contracts.public import MAX_FEN, ContractError
from ai_accounting_modular.ledger.reporting.public import (
    balance_totals,
    cash_totals,
    profit_totals,
    profit_totals_fen,
)


def known(value):
    return {"state": "known", "amount_fen": str(value)}


def unknown(*issues):
    return {"state": "unestablished", "issue_indexes": list(issues)}


def table(last, values=None):
    return {line: known((values or {}).get(line, 0)) for line in range(1, last + 1)}


@pytest.mark.parametrize(
    "calculate,last,values,expected",
    [
        (
            balance_totals,
            53,
            {
                1: 1000,
                2: 200,
                14: 50,
                16: 20,
                17: 30,
                18: 800,
                19: 100,
                21: 40,
                22: 50,
                23: 60,
                24: 70,
                25: 80,
                26: 90,
                27: 100,
                28: 110,
                31: 100,
                32: 200,
                34: 50,
                36: -10,
                42: 200,
                43: 30,
                44: 20,
                45: 10,
                48: 1000,
                49: 200,
                50: 300,
                51: 500,
            },
            {15: 1250, 20: 700, 29: 1350, 30: 2600, 41: 340, 46: 260, 47: 600, 52: 2000, 53: 2600},
        ),
        (
            profit_totals,
            32,
            {
                1: 100000,
                2: 20000,
                3: 1000,
                11: 2000,
                14: 3000,
                18: -500,
                20: 700,
                22: 300,
                24: 400,
                31: 5000,
            },
            {21: 75200, 30: 75100, 32: 70100},
        ),
        (
            cash_totals,
            22,
            {
                1: 1000,
                2: 100,
                3: 200,
                4: 50,
                5: 30,
                6: 20,
                8: 300,
                9: 40,
                10: 50,
                11: 60,
                12: 70,
                14: 500,
                15: 200,
                16: 100,
                17: 10,
                18: 20,
                21: 10000,
            },
            {7: 800, 13: 260, 19: 570, 20: 1630, 22: 11630},
        ),
    ],
)
def test_fixed_totals_accept_only_base_rows_and_do_not_mutate_or_reuse_cells(
    calculate, last, values, expected
):
    rows = table(last, values)
    for line in expected:
        rows.pop(line)
    before = deepcopy(rows)
    actual = calculate(MappingProxyType(rows))
    assert actual == {line: known(value) for line, value in expected.items()}
    assert rows == before
    # Existing computed values never replace their required independent base rows.
    supplied = {**rows, **{line: known(999999) for line in expected}}
    assert calculate(supplied) == actual
    next(iter(actual.values()))["amount_fen"] = "17"
    assert rows == before


def test_unknown_profit_details_do_not_poison_main_totals_but_tax_is_separate():
    rows = table(32, {1: 100000, 2: 20000, 22: 300, 24: 400})
    rows[15], rows[31] = unknown(900), unknown(8, 2, 8)
    before = deepcopy(rows)
    result = profit_totals(rows)
    assert result == {21: known(80000), 30: known(79900), 32: unknown(2, 8)}
    assert rows == before
    result[32]["issue_indexes"].append(44)
    assert rows == before


def test_unknown_same_issue_on_both_sides_never_cancels():
    rows = table(53)
    rows[18] = unknown(7)
    rows[19] = unknown(7)
    rows[2] = unknown(3)
    result = balance_totals(rows)
    assert result[20] == unknown(7)
    assert result[15] == unknown(3)
    assert result[29] == unknown(7)
    assert result[30] == unknown(3, 7)
    assert result[53] == known(0)


def test_unknown_cash_indexes_follow_only_their_fixed_branches():
    rows = table(22, {21: 9000})
    rows[1], rows[4], rows[12] = unknown(5, 2), unknown(5), unknown(9)
    result = cash_totals(rows)
    assert result == {
        7: unknown(2, 5),
        13: unknown(9),
        19: known(0),
        20: unknown(2, 5, 9),
        22: unknown(2, 5, 9),
    }
    result[20]["issue_indexes"].append(44)
    assert result[22] == unknown(2, 5, 9)
    rows = table(22)
    rows[21] = unknown(11)
    result = cash_totals(rows)
    assert result[20] == known(0)
    assert result[22] == unknown(11)


@pytest.mark.parametrize(
    "calculate,last,missing",
    [(balance_totals, 53, 19), (profit_totals, 32, 31), (cash_totals, 22, 21)],
)
def test_missing_base_row_is_an_error_even_when_old_totals_are_supplied(calculate, last, missing):
    rows = table(last)
    rows.pop(missing)
    with pytest.raises(ContractError, match="缺少基础行"):
        calculate(rows)


@pytest.mark.parametrize(
    "bad",
    [
        {"state": "known", "amount_fen": True},
        {"state": "known", "amount_fen": 1.5},
        {"state": "known", "amount_fen": 1},
        {"state": "known", "amount_fen": "1.0"},
        {"state": "known", "amount_fen": "9223372036854775808"},
        {"state": "known", "amount_fen": "0", "extra": 1},
        {"state": "unestablished", "issue_indexes": []},
        {"state": "unestablished", "issue_indexes": [True]},
        {"state": "unestablished", "issue_indexes": [1.0]},
        {"state": "unestablished", "issue_indexes": [1], "amount_fen": "0"},
    ],
)
def test_all_supplied_cells_obey_the_existing_strict_amount_schema(bad):
    rows = table(32)
    rows[4] = bad  # An unused detail still cannot carry invalid or arbitrary data.
    with pytest.raises(ContractError):
        profit_totals(rows)


@pytest.mark.parametrize("bad_key", [True, 1.0, "1", 0, 33])
def test_bool_float_and_out_of_range_row_keys_are_rejected(bad_key):
    rows = {bad_key: known(0), **{line: known(0) for line in range(2, 33)}}
    with pytest.raises(ContractError):
        profit_totals(rows)


@pytest.mark.parametrize(
    "calculate,last,values",
    [
        (balance_totals, 53, {1: 9223372036854775807, 2: 1}),
        (profit_totals, 32, {1: -9223372036854775808, 2: 1}),
        (cash_totals, 22, {1: 9223372036854775807, 21: 1}),
    ],
)
def test_known_sum_overflow_is_explicit_and_never_converts_to_float(calculate, last, values):
    rows = table(last, values)
    before = deepcopy(rows)
    with pytest.raises(ContractError, match="超出有符号64位"):
        calculate(rows)
    assert rows == before


@pytest.mark.parametrize("value", [None, [], [(1, known(0))]])
def test_input_is_a_mapping_not_an_implicit_row_sequence(value):
    with pytest.raises(ContractError, match="映射"):
        profit_totals(value)


def test_integer_profit_totals_share_the_fixed_equations_without_mutating_inputs():
    rows = {1: 100, 2: 20, 3: 4, 11: 5, 14: 6, 18: 7, 20: 8, 22: 9, 24: 10, 31: 11}
    before = dict(rows)
    assert profit_totals_fen(MappingProxyType(rows)) == {21: 66, 30: 65, 32: 54}
    assert rows == before
    assert profit_totals_fen(rows | {21: 999, 30: 999, 32: 999}) == {21: 66, 30: 65, 32: 54}
    assert profit_totals({line: known(value) for line, value in rows.items()}) == {
        21: known(66),
        30: known(65),
        32: known(54),
    }


@pytest.mark.parametrize("bad", [True, 1.0, None, "1", MAX_FEN + 1])
def test_integer_profit_totals_reject_non_integer_unknown_and_out_of_range_values(bad):
    rows = dict.fromkeys((1, 2, 3, 11, 14, 18, 20, 22, 24, 31), 0)
    rows[4] = bad  # An unused detail remains strictly typed.
    with pytest.raises(ContractError, match="64位整数分"):
        profit_totals_fen(rows)


@pytest.mark.parametrize("bad_key", [True, 1.0, "1", 0, 33])
def test_integer_profit_totals_keep_strict_line_keys(bad_key):
    rows = {bad_key: 0, **dict.fromkeys((2, 3, 11, 14, 18, 20, 22, 24, 31), 0)}
    with pytest.raises(ContractError, match="行号"):
        profit_totals_fen(rows)


def test_integer_profit_totals_require_all_base_rows_and_still_check_net_profit_overflow():
    rows = dict.fromkeys((1, 2, 3, 11, 14, 18, 20, 22, 24, 31), 0)
    with pytest.raises(ContractError, match="缺少基础行：31"):
        profit_totals_fen({key: value for key, value in rows.items() if key != 31})
    with pytest.raises(ContractError, match="报表第32行合计"):
        profit_totals_fen(rows | {1: MAX_FEN, 31: -1})
    with pytest.raises(ContractError, match="映射"):
        profit_totals_fen([])
