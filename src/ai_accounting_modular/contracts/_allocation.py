"""Exact monotone cumulative allocation independent of any business domain."""

from collections.abc import Sequence


def cumulative_shares(paid: int, weights: Sequence[int]) -> list[int]:
    """Allocate cumulative units by Webster's highest averages, tying by index.

    Rounding at the standard divisor gives a priority threshold. Its count can
    differ from the requested count by at most half the number of weights;
    moving that short boundary to the exact count yields the same allocation
    as taking the first ``paid`` priorities w/(2*n+1). No step depends on the
    magnitude of ``paid``. Every comparison uses integer cross multiplication.
    """
    weights = tuple(weights)
    if (
        type(paid) is not int
        or paid < 0
        or not weights
        or any(type(weight) is not int or weight <= 0 for weight in weights)
    ):
        raise ValueError("positive integer weights and nonnegative integer units required")
    weight_sum = sum(weights)
    shares = [(2 * paid * weight + weight_sum) // (2 * weight_sum) for weight in weights]
    missing = paid - sum(shares)
    while missing:
        chosen = None
        for index, weight in enumerate(weights):
            if missing < 0 and shares[index] == 0:
                continue
            denominator = 2 * shares[index] + (1 if missing > 0 else -1)
            if chosen is None:
                chosen = index
                continue
            other_denominator = 2 * shares[chosen] + (1 if missing > 0 else -1)
            comparison = weight * other_denominator - weights[chosen] * denominator
            if (missing > 0 and comparison > 0) or (
                missing < 0 and (comparison < 0 or comparison == 0 and index > chosen)
            ):
                chosen = index
        direction = 1 if missing > 0 else -1
        shares[chosen] += direction
        missing -= direction
    return shares
