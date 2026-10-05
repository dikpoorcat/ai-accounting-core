"""Dense cohort storage preserves the existing ordered frozen-root contract."""

import sys

import pytest
from test_settlement_period_scopes import allocation, cash_payment, setup

from ai_accounting.kernel import settlement_freeze as freeze
from ai_accounting.kernel import settlement_freeze_v1 as old_groups
from ai_accounting.kernel.types import YearMonth, canonical


@pytest.mark.parametrize("bad", [None, "source", "paid", "other"])
@pytest.mark.parametrize("amount,paid,other", [(100, 20, 30), (100, 100, 0), (0, 0, 0), (0, 20, 0)])
def test_group_replacement_matches_preserved_named_field_oracle(bad, amount, paid, other):
    state = freeze._empty_state("payroll:wage:net")
    state.update(
        source_event_count=1, first_source_period=YearMonth("2026-01").ordinal,
        category="payable", account="221101", counterparty_id="employee",
        source_kind="payroll", source_amount=amount, paid=paid, other_settled=other,
        movement_count=2, unresolved_movement_count=1 if bad else 0,
    )
    if bad is not None:
        state["bad_" + bad] = True
    replacement = state | {"paid": paid + 1, "movement_count": 3}
    dense, named = {}, {}
    for item, direction in ((state, 1), (state, -1), (replacement, 1)):
        freeze._add_group(dense, item, direction)
        old_groups._add_group(named, item, direction)
        assert freeze._groups_for_root(dense) == old_groups._groups_for_root(named)
    freeze._add_group(dense, replacement, -1)
    assert dense == {}
    assert freeze._group_delta(freeze._empty_state("unused"), 1) == ()


@pytest.mark.parametrize("length", [0, 4, 5, 19, 21, 25])
def test_group_restore_keeps_strict_field_count(length):
    root = {"groups": [[0] * length]}
    with pytest.raises(ValueError):
        old_groups._groups_from_root(root)
    with pytest.raises(ValueError):
        freeze._groups_from_root(root)


def test_restored_values_are_independent_and_reduce_actual_container_storage():
    period = YearMonth("2026-01").ordinal
    root = {"groups": [
        [period, "payable", "221101", f"employee-{number}", "payroll",
         1, 0, 1, 0, 1, 0, 1, 0, 0, 0, 100, 100, 120, 20, 0]
        for number in range(4096)
    ]}
    dense = freeze._groups_from_root(root)
    named = old_groups._groups_from_root(root)
    assert len(dense) == len(named) == len(root["groups"])
    assert all(type(values) is list and len(values) == 15 for values in dense.values())
    assert canonical(freeze._groups_for_root(dense)) == canonical(
        old_groups._groups_for_root(named)
    )
    dense_storage = sys.getsizeof(dense) + sum(sys.getsizeof(values) for values in dense.values())
    named_storage = sys.getsizeof(named) + sum(sys.getsizeof(values) for values in named.values())
    # Both representations keep the same number of key tuples and references
    # to the same integer objects; count the containers that actually differ.
    assert dense_storage < named_storage * 0.6
    first_key = next(iter(dense))
    dense[first_key][freeze._G_REMAINING_SUM] = 99
    assert root["groups"][0][5 + freeze._G_REMAINING_SUM] == 100
    assert named[first_key]["remaining_sum"] == 100


def test_group_updates_preserve_checked_field_order_and_fully_reversed_guard(monkeypatch):
    state = freeze._empty_state("debt")
    state.update(source_event_count=1, source_amount=100, movement_count=2)
    seen = []
    original_checked = freeze.checked

    def tracked(value):
        seen.append(value)
        return original_checked(value)

    monkeypatch.setattr(freeze, "checked", tracked)
    groups = {}
    freeze._add_group(groups, state, 1)
    expected = old_groups._group_delta(state, 1)
    assert seen == [100, *(expected[field] for field in freeze._FIELDS)]
    group = groups[freeze._group_key(state)]
    group[freeze._G_PAID_SUM] = 1
    with pytest.raises(ValueError, match="not fully reversed"):
        freeze._add_group(groups, state, -1)
    group[freeze._G_OBLIGATION_COUNT] = (1 << 63) - 1
    with pytest.raises(ValueError, match="signed 64-bit"):
        freeze._add_group(groups, state, 1)


def test_independent_prepared_roots_and_every_saved_byte_match_old_group_oracle(
    tmp_path, monkeypatch
):
    company = setup(tmp_path)
    company.close("2026-01")
    company.save(
        cash_payment(
            "2026-02", 800000,
            allocation("labor_project_cost", "cost", "net", 800000),
        ),
        "balance-paid",
    )
    company.publish("balance-paid")
    company.close("2026-02")
    with company.engine.store.connection(read_only=True) as connection:
        connection.execute("BEGIN")
        dense = freeze._authoritative_freezes(company.engine, connection)
        with monkeypatch.context() as oracle:
            oracle.setattr(freeze, "_groups_from_root", old_groups._groups_from_root)
            oracle.setattr(freeze, "_groups_for_root", old_groups._groups_for_root)
            oracle.setattr(freeze, "_add_group", old_groups._add_group)
            named = freeze._authoritative_freezes(company.engine, connection)
        assert dense == named
        prepared, blocks, revisions = dense
        assert len(prepared) == 2
        assert not freeze._frozen_difference(connection, prepared, blocks, revisions)
