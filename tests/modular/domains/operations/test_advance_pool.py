"""Conservation is checked against integer capacities, not implementation output."""

from itertools import product

from ai_accounting_modular.domains.operations._advance_pool import conserve_vat


def test_all_small_tax_paths_remain_monotone_and_exhaust_exact_source_capacity():
    for capacity in range(1, 8):
        for actual_remaining_tax in range(capacity + 1):
            for path in product((0, 1), repeat=capacity):
                remaining_gross, remaining_tax = capacity, actual_remaining_tax
                adopted = []
                for normal_tax in path:
                    tax = conserve_vat(normal_tax, 1, remaining_gross, remaining_tax)
                    assert tax in (0, 1)
                    adopted.append(tax)
                    remaining_gross -= 1
                    remaining_tax -= tax
                    assert 0 <= remaining_tax <= remaining_gross
                assert sum(adopted) == actual_remaining_tax
                assert remaining_tax == remaining_gross == 0


def test_real_red_vat_tail_is_preserved_without_switching_to_a_ratio():
    assert conserve_vat(80, 8080, 8080, 81) == 81
    # Inclusive 3% gives gross10/net10 here. Rounded whole-source net17/18
    # proportional allocation would incorrectly force a one-fen VAT.
    assert conserve_vat(0, 10, 18, 1) == 0
    assert conserve_vat(1, 8, 8, 1) == 1


def test_max_amount_only_uses_bounded_arithmetic():
    maximum = 2**63 - 1
    assert conserve_vat(0, maximum, maximum, maximum - 1) == maximum - 1
