"""Independent finite priority queues verify the efficient allocation formula."""

from fractions import Fraction
from itertools import product

import pytest

from ai_accounting_modular.contracts.public import MAX_FEN, cumulative_shares


def naive_sequence(count, weights):
    shares = [0] * len(weights)
    for _ in range(count):
        winner = max(
            range(len(weights)),
            key=lambda index: (Fraction(weights[index], 2 * shares[index] + 1), -index),
        )
        shares[winner] += 1
    return shares


@pytest.mark.parametrize("size", [1, 2, 3, 4])
def test_every_small_weight_matches_independent_priority_oracle(size):
    for weights in product(range(1, 4), repeat=size):
        previous = [0] * size
        for paid in range(2 * sum(weights) + 1):
            actual = cumulative_shares(paid, weights)
            assert actual == naive_sequence(paid, weights), (paid, weights)
            assert sum(actual) == paid
            assert all(new >= old for new, old in zip(actual, previous, strict=True))
            previous = actual
        assert cumulative_shares(0, weights) == [0] * size
        assert cumulative_shares(sum(weights), weights) == list(weights)


def test_three_one_fen_parts_never_produce_negative_second_payment():
    assert cumulative_shares(1, [1, 1, 1]) == [1, 0, 0]
    assert cumulative_shares(2, [1, 1, 1]) == [1, 1, 0]
    assert cumulative_shares(3, [1, 1, 1]) == [1, 1, 1]


def test_signed_64_bit_money_uses_a_short_boundary_not_one_step_per_fen():
    weights = [MAX_FEN - 2, 1, 1]
    assert cumulative_shares(MAX_FEN, weights) == weights
    before = cumulative_shares(MAX_FEN - 1, weights)
    assert sum(before) == MAX_FEN - 1
    assert all(a <= b for a, b in zip(before, weights, strict=True))


@pytest.mark.parametrize(
    "paid,weights",
    [
        (-1, [1]),
        (1, []),
        (1, [0]),
        (1, [-1]),
        (True, [1]),
        (1, [True]),
        (1.0, [1]),
        (1, [0.5]),
    ],
)
def test_invalid_allocation_inputs_are_not_silently_repaired(paid, weights):
    with pytest.raises(ValueError):
        cumulative_shares(paid, weights)
