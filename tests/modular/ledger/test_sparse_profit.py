"""Fixed financial meanings and failure order for sparse integer contributions."""

from uuid import NAMESPACE_DNS, uuid5

import pytest

from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ContractError,
    validate_contract,
)
from ai_accounting_modular.ledger._financial_cells import Issues, known, zeros
from ai_accounting_modular.ledger._financial_profit import pretax_slices, profit
from ai_accounting_modular.ledger._profit import pretax_values, profit_rows


def _target(kind="operations.expense", **values):
    if kind == "taxes.assessment":
        initial = dict.fromkeys(
            (
                "net_sales_fen",
                "accrued_vat_fen",
                "payable_vat_fen",
                "urban_fen",
                "education_fen",
                "local_education_fen",
                "surtax_fen",
                "rounding_adjustment_fen",
                "relief_fen",
            ),
            "0",
        )
        initial.update(
            type="vat", adopted_basis="calculated", filing_state="partial", filing_issues=[]
        )
    elif kind == "operations.other_income":
        initial = {"amount_fen": "5", "expense_or_income_class": "bank_interest"}
    else:
        initial = {
            "amount_fen": "0",
            "expense_or_income_class": "administration",
            "report_detail_state": "known",
        }
    result = {"kind": kind, "result_slot": "main", "values": initial | values}
    validate_contract("business-facts.schema.json", "ResultTarget", result)
    return result


def _detail(line, amount=None, code="missing_detail"):
    return (
        {"line": line, "amount_fen": str(amount)}
        if amount is not None
        else {
            "line": line,
            "issue": {"code": code, "meaning": "明确分项", "message": "来源未能确认分项。"},
        }
    )


def _item(lines=(), details=(), *, target=None, sign=1):
    return {
        "lines": [
            {
                "account_code": code,
                "debit_fen": str(max(amount, 0)),
                "credit_fen": str(max(-amount, 0)),
            }
            for code, amount in lines
        ],
        "details": details,
        "target": target or _target(),
        "sign": sign,
    }


class _Selected:
    def __init__(self, *items, prior_unknown=()):
        self.issues, self.prior_unknown = Issues(), prior_unknown
        self.heads = {
            str(index): {
                "result_id": str(index),
                "business_id": str(uuid5(NAMESPACE_DNS, f"sparse-{index}")),
                "generation": 1,
                "result_slot": "main",
                "business_period": "2026-01",
            }
            for index in range(len(items))
        }
        self.items = dict(zip(self.heads, items, strict=True))

    def flow(self, first, last):
        return {key: value["sign"] for key, value in self.items.items()}

    def lines(self, heads):
        return {row["result_id"]: self.items[row["result_id"]]["lines"] for row in heads}

    def report_details(self, heads):
        return {row["result_id"]: self.items[row["result_id"]]["details"] for row in heads}

    def prior(self, *args):
        cells = zeros(32)
        for line in self.prior_unknown:
            cells[line] = self.issues.unknown(f"/prior/{line}", "接续", "未确定接续。")
        return cells

    def target(self, head):
        return self.items[head["result_id"]]["target"]

    def kind(self, head):
        return self.target(head)["kind"]

    def load_targets(self, heads):
        pass  # This arithmetic fixture already holds the actual validated Targets.

    def profit_rows(self, head, lines):
        return profit_rows(lines)


def _read(selected):
    return profit(selected, "2026-01", "2026-03", "current")


def test_sparse_details_preserve_main_totals_and_issue_order():
    selected = _Selected(_item((("5602", 120),), (_detail(15, 20), _detail(16), _detail(17))))
    actual = _read(selected)
    assert len(actual) == 32
    assert {line: actual[line] for line in (14, 15, 16, 17, 21, 30, 32)} == {
        14: known(120),
        15: known(20),
        16: {"state": "unestablished", "issue_indexes": [0]},
        17: {"state": "unestablished", "issue_indexes": [1]},
        21: known(-120),
        30: known(-120),
        32: known(-120),
    }
    assert [issue["field"] for issue in selected.issues.values] == [
        "/profit_statement/16/current",
        "/profit_statement/17/current",
    ]


@pytest.mark.parametrize("rounding, expected", [("-1", 7), ("1", 8)])
def test_vat_rounding_detail_does_not_replace_main_income(rounding, expected):
    selected = _Selected(
        _item(
            (("6301", -5), ("630101", -3)),
            target=_target("taxes.assessment", rounding_adjustment_fen=rounding),
        )
    )
    actual = _read(selected)
    assert actual[22] == known(8) and actual[23] == known(expected)
    assert actual[30] == known(8)


def test_interest_fallback_and_overridden_details_keep_their_original_meanings():
    selected = _Selected(
        _item(
            (("5603", -3), ("560301", -2), ("571103", 5)),
            (_detail(29),),
            target=_target("operations.other_income"),
        )
    )
    actual = _read(selected)
    assert actual[18] == known(-5) and actual[19] == known(-5)
    assert actual[24] == known(5) and actual[29] == known(5)
    assert actual[30] == known(0)
    # The prior contract creates the detail issue before applying the authoritative line29.
    assert [issue["field"] for issue in selected.issues.values] == ["/profit_statement/29/current"]
    explicit = _read(_Selected(_item((("560301", -2),), (_detail(19),))))
    assert explicit[19] == {"state": "unestablished", "issue_indexes": [0]}


def test_unmapped_nonzero_and_conflicting_unknowns_keep_the_original_issue_indexes():
    selected = _Selected(_item((("9999", 5),), (_detail(24),)))
    actual = _read(selected)
    assert (
        actual[24] == actual[30] == actual[32] == {"state": "unestablished", "issue_indexes": [1]}
    )
    assert [issue["field"] for issue in selected.issues.values] == [
        "/profit_statement/24/current",
        "/profit_statement/30/current",
    ]
    selected = _Selected(
        _item(details=(_detail(15), _detail(15, code="other_missing"), _detail(16)))
    )
    actual = _read(selected)
    assert actual[15] == {"state": "unestablished", "issue_indexes": [0]}
    assert actual[16] == {"state": "unestablished", "issue_indexes": [1]}
    assert selected.issues.values[0]["code"] == "report_basis_missing"
    assert actual[30] == known(0)


def test_opposite_signs_cancel_known_values_but_never_unknowns():
    assert _read(_Selected(_item((("5602", 10),), sign=-1), _item((("5602", 10),))))[14] == known(0)
    selected = _Selected(_item(details=(_detail(15),), sign=-1), _item(details=(_detail(15),)))
    assert _read(selected)[15] == {"state": "unestablished", "issue_indexes": [0]}


def test_unknown_short_circuit_keeps_ordered_overflow_checks():
    selected = _Selected(_item((("5602", MAX_FEN),)), _item((("5602", 1),)), prior_unknown=(14,))
    assert _read(selected)[14] == {"state": "unestablished", "issue_indexes": [0]}
    # A local unknown comes first and can absorb the later known addition.
    selected = _Selected(_item((("5602", MAX_FEN),), (_detail(14), _detail(14, 1))))
    assert _read(selected)[14] == {"state": "unestablished", "issue_indexes": [0]}
    # An overflowing known addition before that unknown remains an error.
    with pytest.raises(ContractError, match="资源累计金额超出"):
        _read(_Selected(_item((("5602", MAX_FEN),), (_detail(14, 1), _detail(14)))))
    # Scaling MIN is checked before it can be absorbed into an unknown prior.
    with pytest.raises(ContractError, match="资源累计金额超出"):
        _read(_Selected(_item((("5602", -MAX_FEN), ("5602", -1)), sign=-1), prior_unknown=(14,)))


def test_main_and_interest_overflow_are_not_hidden_by_other_rows_or_bank_interest_fallback():
    with pytest.raises(ContractError, match="资源累计金额超出"):
        _read(_Selected(_item((("5602", MAX_FEN),)), _item((("5602", 1),))))
    selected = _Selected(
        _item(
            (("5603", -1), ("560301", MAX_FEN), ("560301", 1)),
            target=_target("operations.other_income"),
        )
    )
    with pytest.raises(ContractError, match="资源累计金额超出"):
        _read(selected)


def test_pretax_slices_retain_main_only_values_sign_and_sorted_identity():
    selected = _Selected(
        _item((("5001", -100), ("5401", 20), ("5801", 10)), (_detail(15, 999),), sign=-1),
        _item((("5801", 3),)),
        _item((("5602", -5),)),
    )
    expected = [
        {
            "business_id": head["business_id"],
            "result_slot": "main",
            "period": "2026-01",
            "sign": -1 if key == "0" else 1,
            "values": [100, 20, 80] if key == "0" else [0, 0, 5],
        }
        for key, head in selected.heads.items()
        if key != "1"
    ]
    assert pretax_slices(selected, "2026-01", "2026-03") == sorted(
        expected, key=lambda row: row["business_id"]
    )
    assert pretax_values({1: 100, 2: 20, 31: 10}) == (100, 20, 80)
    with pytest.raises(ContractError, match="报表第32行合计"):
        pretax_values({1: MAX_FEN, 31: -1})
