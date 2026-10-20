"""Independent per-fen oracle verifies original-source interval operations."""

from itertools import combinations

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.operations._tax_ranges import (
    gross,
    intersect,
    return_ranges,
    subtract,
    validate_resolution,
    wire,
)
from ai_accounting_modular.domains.operations._vat import _split, split_ranges


def intervals(points):
    result = []
    for value in sorted(points):
        if result and result[-1][1] == value:
            result[-1] = result[-1][0], value + 1
        else:
            result.append((value, value + 1))
    return result


def cells(values):
    return {value for start, end in values for value in range(start, end)}


def test_interval_operations_match_independent_integer_set_oracle():
    subsets = [set(group) for size in range(6) for group in combinations(range(5), size)]
    for left in subsets:
        for right in subsets:
            a, b = intervals(left), intervals(right)
            assert cells(intersect(a, b)) == left & right
            assert cells(subtract(a, b)) == left - right
            assert gross(a) == len(left)


def test_complete_resolution_is_not_allowed_to_hide_gaps_or_overlap():
    identifier = "synthetic-source"
    valid = {
        "target_business_id": identifier,
        "segments": [
            {"state": "not_due", "ranges": wire([(4, 7)])},
            {
                "state": "due",
                "ranges": wire([(0, 4)]),
                "tax_period": "2026-01",
                "trigger": "invoice",
                "event_ids": [],
            },
        ],
    }
    validate_resolution(valid, identifier, 7)
    for invalid in [[(3, 7)], [(5, 7)], [(4, 8)], []]:
        valid["segments"][0]["ranges"] = wire(invalid)
        with pytest.raises(ContractError):
            validate_resolution(valid, identifier, 7)
    validate_resolution({"target_business_id": identifier, "segments": []}, identifier, 0)


def test_max_range_uses_endpoints_and_never_enumerates_money():
    limit = 2**63 - 1
    assert subtract([(0, limit)], [(1, limit - 1)]) == [(0, 1), (limit - 1, limit)]
    assert gross([(0, limit)]) == limit


def test_later_explicit_range_does_not_move_an_already_consumed_earlier_month():
    first = {"business_id": "first", "period": "2026-01", "data": {"gross_fen": "5"}}
    later = {
        "business_id": "later",
        "period": "2026-02",
        "data": {"gross_fen": "3", "tax_ranges": wire([(0, 3)])},
    }
    assert return_ranges([first], 10)["first"] == wire([(0, 5)])
    with pytest.raises(ContractError):
        return_ranges([later, first], 10)


@pytest.mark.parametrize("rate", ["0.01", "0.03"])
def test_original_boundaries_conserve_all_rounding_without_each_part_rounding(rate):
    params = {"nominal_rate": "0.03", "tax_base_rate": rate, "payable_rate": rate}
    vat = {"waive_rate_reduction": False}
    for capacity in range(1, 150):
        expected = _split(capacity, params, vat)
        parts = [split_ranges([(start, start + 1)], params, vat) for start in range(capacity)]
        assert tuple(sum(value[index] for value in parts) for index in (0, 1)) == expected
        assert all(net >= 0 and tax >= 0 and net + tax == 1 for net, tax in parts)
    # Two disjoint pieces are added before a subsequent consumer sees a total.
    assert split_ranges([(0, 17), (48, 103)], params, vat) == tuple(
        sum(
            row[index]
            for row in (
                split_ranges([(0, 17)], params, vat),
                split_ranges([(48, 103)], params, vat),
            )
        )
        for index in (0, 1)
    )
