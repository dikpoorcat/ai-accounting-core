"""A grouped amount has exactly the same limits and unknowns as sequential cells."""

from itertools import product
from types import SimpleNamespace

import pytest

from ai_accounting_modular.contracts.public import MAX_FEN, MIN_FEN, ContractError
from ai_accounting_modular.ledger._cashflow import cash, missing_classification
from ai_accounting_modular.ledger._financial_cells import Issues, add, known
from ai_accounting_modular.ledger._financial_projection_fold import (
    Fold,
    Notice,
    Unknown,
    merge_notices,
    values_fold,
)

A = Notice("/profit_statement/24/current", "report_basis_missing", "原始X", "说明X")
B = Notice(A.field, "other", "B", "说明B")
C = Notice(A.field, "report_basis_missing", "原始Y", "说明Y")


def observed(values):
    issues = Issues()
    value = values_fold(values).finish()
    encountered = ()
    for item in values:
        if isinstance(item, Unknown):
            encountered = merge_notices(encountered, item.notices)
    for note in encountered:
        note = note.final()
        issues.unknown(note.field, note.meaning, note.message, code=note.code)
    if isinstance(value, Unknown):
        for note in value.notices:
            note = note.final()
            result = issues.unknown(note.field, note.meaning, note.message, code=note.code)
    else:
        result = known(value)
    return result, issues.values


def sequential(values):
    issues, result = Issues(), known()
    for value in values:
        if isinstance(value, Unknown):
            for note in value.notices:
                note = note.final()
                right = issues.unknown(note.field, note.meaning, note.message, code=note.code)
            result = right if value.resets_initial else add(result, right)
        else:
            result = add(result, known(value))
    return result, issues.values


def test_original_fallback_code_does_not_destroy_first_notice_or_associativity():
    left = merge_notices(merge_notices((A,), (B,)), (C,))
    right = merge_notices((A,), merge_notices((B,), (C,)))
    assert left == right and left[0].meaning == "原始X" and left[0].mixed_codes
    assert observed(tuple(Unknown((note,)) for note in (A, B, C))) == sequential(
        tuple(Unknown((note,)) for note in (A, B, C))
    )


def test_grouping_matches_real_cell_arithmetic_at_all_short_extreme_sequences():
    values = (
        0,
        1,
        -1,
        MAX_FEN,
        MIN_FEN,
        Unknown((A,)),
        Unknown((B,)),
        Unknown((C,), resets_initial=True),
    )
    for entries in product(values, repeat=3):
        try:
            expected = sequential(entries)
        except ContractError as error:
            with pytest.raises(ContractError) as caught:
                observed(entries)
            assert caught.value.code == error.code == "invalid_input"
        else:
            assert observed(entries) == expected
        for split in range(4):
            assert values_fold(entries[:split]).then(values_fold(entries[split:])) == values_fold(
                entries
            )


def test_unknown_only_swallows_global_later_prefix_not_invalid_local_amounts():
    value = values_fold((MAX_FEN, 1))
    assert isinstance(value.finish(Unknown((A,))), Unknown)
    with pytest.raises(ContractError):
        value.finish()
    with pytest.raises(ValueError):
        values_fold((Unknown((A,)), MAX_FEN + 1))


@pytest.mark.parametrize(
    "raw",
    [
        [True, 0, 0, [], False],
        [0, 0.0, 0, [], False],
        [0, 1, 0, [], False],
        [2, 0, 1, [], False],
        [0, 0, 0, [["field", "code", "meaning", "message", 0]], False],
        [0, 0, 0, [A.record(), A.record()], False],
        [0, 0, 0, [], True],
    ],
)
def test_broken_persisted_fold_is_not_accepted_as_an_empty_or_valid_amount(raw):
    with pytest.raises(ContractError) as caught:
        Fold.from_record(raw)
    assert caught.value.code == "storage_corrupt"


def test_unknown_fold_can_preserve_an_out_of_range_unconsumed_total():
    fold = values_fold((Unknown((A,)), MAX_FEN, MAX_FEN))
    assert Fold.from_record(fold.record()) == fold
    assert isinstance(fold.finish(), Unknown)


def test_real_cash_unknown_replaces_active_prior_index_but_keeps_all_registered_issues():
    def prior(issues):
        return {
            line: issues.unknown(f"/cash_flow_statement/{line}/prior", "接续", "缺接续")
            for line in range(1, 23)
        }

    legacy_issues = Issues()
    selection = SimpleNamespace(
        issues=legacy_issues,
        heads={"head": {"result_id": "head"}},
        flow=lambda first, last: {"head": 1},
        children=lambda table, heads: {
            "head": [
                {
                    "movement_id": "actual",
                    "public_slot": "primary",
                    "transfer_group_id": None,
                    "cashflow_parts_json": None,
                }
            ]
        },
        source=lambda head: None,
        prior=lambda statement, first, last: prior(legacy_issues),
    )
    expected = cash(selection, "2026-01", "2026-01", "current")
    issues = Issues()
    initial = prior(issues)[1]
    prior_note = Notice(
        *(
            issues.values[initial["issue_indexes"][0]][key]
            for key in ("field", "code", "meaning", "message")
        )
    )
    # The separate issue-registration channel preserves every prior/current issue.
    current = None
    for line in range(1, 21):
        cell = missing_classification(issues, line, "current")
        if line == 1:
            current = Notice(
                *(
                    issues.values[cell["issue_indexes"][0]][key]
                    for key in ("field", "code", "meaning", "message")
                )
            )
    result = values_fold((Unknown((current,), resets_initial=True),)).finish(Unknown((prior_note,)))
    assert result.notices == (current,)
    actual = issues.unknown(current.field, current.meaning, current.message, code=current.code)
    assert actual == expected[1] and actual["issue_indexes"] == [22]
    assert issues.values == legacy_issues.values


def test_reset_with_multiple_fields_keeps_earlier_issues_outside_the_active_cell():
    prior = Notice("/prior", "prior", "接续", "缺接续")
    first = Notice("/current/1", "cash", "分类一", "缺分类一")
    other = Notice("/current/2", "cash", "分类二", "缺分类二")
    issues = Issues()
    for note in (prior, first, other):
        issues.unknown(note.field, note.meaning, note.message, code=note.code)
    fold = values_fold((Unknown((first, other)), Unknown((first,), resets_initial=True)))
    value = fold.finish(Unknown((prior,)))
    assert value.notices == (first,)
    active = issues.unknown(first.field, first.meaning, first.message, code=first.code)
    assert active["issue_indexes"] == [1]
    assert [issue["field"] for issue in issues.values] == ["/prior", "/current/1", "/current/2"]
