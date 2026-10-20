"""Resource scheduling metadata has one strict and amount-free meaning."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    check_resource_dependency,
    resource_exclusions,
    resource_use_affects,
    validate_contract,
)

SOURCE = "50000000-0000-4000-8000-000000000090"
USER = "50000000-0000-4000-8000-000000000001"
OBSERVER = "50000000-0000-4000-8000-000000000002"


def edge(mode, **updates):
    value = {
        "consumer_business_id": USER if mode == "use" else OBSERVER,
        "consumer_result_slot": "main",
        "producer_kind": "business",
        "producer_id": SOURCE,
        "producer_public_slot": "primary",
        "read_mode": mode,
        "relation_kind": "settlement",
        "period_from": "2026-01",
        "period_to": "2026-03" if mode == "balance" else "2026-01",
    }
    value.update(updates)
    return value


def check(value):
    validate_contract("business-facts.schema.json", "Dependency", value)
    check_resource_dependency(value)
    return value


@pytest.mark.parametrize("mode", ["use", "balance"])
@pytest.mark.parametrize("relation", ["settlement", "cost"])
def test_resource_modes_are_exact_business_resource_scopes(mode, relation):
    valid = edge(mode, relation_kind=relation)
    assert check(valid) == valid
    for patch in (
        {"producer_kind": "object"},
        {"producer_kind": "aggregate"},
        {"relation_kind": "calculation"},
        {"amount_fen": "100"},
    ):
        with pytest.raises(ContractError):
            check({**valid, **patch})
    for field in ("period_from", "period_to"):
        missing = deepcopy(valid)
        missing.pop(field)
        with pytest.raises(ContractError):
            check(missing)


@pytest.mark.parametrize("mode", ["use", "balance"])
def test_necessary_actual_day_must_be_real_and_belong_to_declared_end_month(mode):
    current = edge(mode)
    day = "2026-01-15" if mode == "use" else "2026-03-15"
    assert check({**current, "effective_on": day})["effective_on"] == day
    for patch in (
        {"effective_on": "2026-02-30"},
        {"effective_on": "2026-04-01"},
        {"period_from": "2026-04"},
    ):
        with pytest.raises(ContractError):
            check({**current, **patch})
    if mode == "use":
        with pytest.raises(ContractError):
            check({**current, "period_to": "2026-02", "effective_on": "2026-02-15"})


def test_exclusions_name_exact_result_slots_and_are_only_balance_metadata():
    excluded = [{"business_id": USER, "result_slot": "netting"}]
    watched = check(edge("balance", excluded_result_slots=excluded))
    assert resource_exclusions(watched) == frozenset({(USER, "netting")})
    assert resource_exclusions(edge("balance")) == frozenset()
    for invalid in (
        edge("use", excluded_result_slots=excluded),
        edge("balance", excluded_result_slots=excluded * 2),
        edge("balance", excluded_result_slots=[{"business_id": USER}]),
        edge("balance", excluded_result_slots=[{"business_id": USER, "slot": "netting"}]),
    ):
        with pytest.raises(ContractError):
            check(invalid)


@pytest.mark.parametrize(
    ("use_patch", "balance_patch", "expected"),
    [
        ({}, {}, True),
        ({"producer_public_slot": "vat"}, {}, False),
        ({"producer_id": OBSERVER}, {}, False),
        ({"relation_kind": "cost"}, {}, False),
        ({"period_from": "2026-04", "period_to": "2026-04"}, {}, False),
        ({}, {"period_from": "2026-02"}, False),
        (
            {"effective_on": "2026-01-20"},
            {"period_to": "2026-01", "effective_on": "2026-01-10"},
            False,
        ),
        (
            {"effective_on": "2026-01-10"},
            {"period_to": "2026-01", "effective_on": "2026-01-10"},
            True,
        ),
        ({}, {"period_to": "2026-01", "effective_on": "2026-01-10"}, True),
        ({"effective_on": "2026-01-20"}, {}, True),
        ({}, {"excluded_result_slots": [{"business_id": USER, "result_slot": "main"}]}, False),
        ({}, {"excluded_result_slots": [{"business_id": USER, "result_slot": "netting"}]}, True),
        (
            {"consumer_result_slot": "netting"},
            {"excluded_result_slots": [{"business_id": USER, "result_slot": "main"}]},
            True,
        ),
        (
            {"consumer_result_slot": "netting"},
            {"excluded_result_slots": [{"business_id": USER, "result_slot": "netting"}]},
            False,
        ),
    ],
)
def test_shared_matching_never_confuses_resources_unknown_days_or_sibling_slots(
    use_patch, balance_patch, expected
):
    use, watched = check(edge("use", **use_patch)), check(edge("balance", **balance_patch))
    before = deepcopy((use, watched))
    assert resource_use_affects(use, watched) is expected
    assert (use, watched) == before
