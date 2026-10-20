"""An independent per-fen oracle verifies the production interval allocator."""

from itertools import combinations_with_replacement, product

import pytest

from ai_accounting_modular.contracts.public import ContractError
from ai_accounting_modular.domains.operations._cumulative import AmountRange, inherit_cashflow
from ai_accounting_modular.domains.operations._overpayment_pool import allocate_settlement_ranges


def expanded(spans):
    return sorted(position for span in spans for position in range(span.before, span.after))


def test_intervals_match_exhaustive_small_integer_cash_stack():
    cases = 0
    for count in range(1, 4):
        for paid in combinations_with_replacement(range(1, 7), count):
            for amounts in product(range(1, 4), repeat=count):
                if any(sum(amounts[: index + 1]) > total for index, total in enumerate(paid)):
                    continue
                entries = [
                    (str(index), total, value)
                    for index, (total, value) in enumerate(zip(paid, amounts, strict=True))
                ]
                actual = allocate_settlement_ranges(entries)
                free, previous = [], 0
                for index, (total, value) in enumerate(zip(paid, amounts, strict=True)):
                    free.extend(range(previous, total))
                    selected = sorted(free.pop() for _ in range(value))
                    assert expanded(actual[str(index)]) == selected
                    assert (
                        actual[str(index)]
                        == allocate_settlement_ranges(entries[: index + 1])[str(index)]
                    )
                    previous = total
                cases += 1
    assert cases > 500


def test_late_discovery_consumes_multiple_unoccupied_segments():
    allocations = allocate_settlement_ranges([("first", 5, 1), ("second", 8, 2), ("late", 8, 4)])
    assert [(span.before, span.after) for span in allocations["first"]] == [(4, 5)]
    assert [(span.before, span.after) for span in allocations["second"]] == [(6, 8)]
    assert [(span.before, span.after) for span in allocations["late"]] == [(1, 4), (5, 6)]


@pytest.mark.parametrize("entries", [[("a", 1, 2)], [("a", 2, 1), ("b", 2, 2)]])
def test_cumulative_claims_cannot_borrow_unavailable_cash(entries):
    with pytest.raises(ContractError) as error:
        allocate_settlement_ranges(entries)
    assert error.value.code == "dependency_conflict"


def test_maximum_money_is_not_enumerated_or_added_to_claim_amount():
    maximum = 2**63 - 1
    ranges = allocate_settlement_ranges([("first", maximum, maximum - 1), ("last", maximum, 1)])
    assert [(span.before, span.after) for span in ranges["last"]] == [(0, 1)]


def test_fragment_classification_is_combined_before_normalizing_weights():
    source = {
        "cashflow_weights": [
            {"category": "employee_payments", "weight": "1"},
            {"category": "other_operating_payments", "weight": "2"},
        ]
    }
    target = {}
    inherit_cashflow(source, target, [AmountRange(0, 2), AmountRange(5, 7)])
    assert target["cashflow_weights"] == [
        {"category": "employee_payments", "weight": "1"},
        {"category": "other_operating_payments", "weight": "3"},
    ]


@pytest.mark.parametrize("mixed", [False, True])
def test_zero_interval_preserves_known_single_nature_without_inventing_mixed_proportions(mixed):
    source = {"cashflow_weights": [{"category": "employee_payments", "weight": "2"}]}
    if mixed:
        source["cashflow_weights"].append({"category": "other_operating_payments", "weight": "1"})
    target = {}
    inherit_cashflow(source, target, [AmountRange(1, 1)])
    if mixed:
        assert "cashflow_weights" not in target
    else:
        assert target["cashflow_weights"] == [{"category": "employee_payments", "weight": "1"}]
