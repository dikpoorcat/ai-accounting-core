"""Fact document parts prove the complete shard before deriving immutable metadata."""

from copy import deepcopy
from dataclasses import FrozenInstanceError

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    FactDocumentPart,
    canonical_json,
    fact_source_ids,
    loads_json,
)
from tests.modular.contracts.test_fact_sources import _facts, _locator_objects


def shard(*facts):
    fact = facts[0]
    return {
        "format": "ai-accounting-standard/1",
        "key": "facts:" + fact["kind"].split(".", 1)[0] + ":" + fact["period"],
        "facts": list(facts),
    }


def test_every_existing_fact_retains_its_complete_sources_and_standard_bytes():
    for fact in _facts():
        value = shard(fact)
        before = deepcopy(value)
        part = FactDocumentPart(value)
        expected = deepcopy(value)
        expected["facts"][0]["evidence_ids"].sort()
        assert part.canonical_json == canonical_json(expected)
        assert part.key == value["key"]
        assert part.business_ids == (fact["business_id"],)
        assert part.source_ids == fact_source_ids(fact)
        assert value == before


def test_metadata_and_canonical_body_are_detached_and_normalized_without_losing_nested_sources():
    fact = deepcopy(next(value for value in _facts() if value["kind"] == "money.bank_statement"))
    for locator in _locator_objects(fact):
        locator["source_id"] = "10000000-0000-4000-8000-000000000003"
    fact["evidence_ids"] = [
        "10000000-0000-4000-8000-000000000002",
        "10000000-0000-4000-8000-000000000001",
    ]
    value = shard(fact)
    part = FactDocumentPart(value)
    assert part.source_ids == tuple(f"10000000-0000-4000-8000-00000000000{n}" for n in (1, 2, 3))
    expected = loads_json(part.canonical_json)
    assert expected["facts"][0]["evidence_ids"] == sorted(fact["evidence_ids"])
    value["facts"][0]["data"].clear()
    value["facts"].clear()
    expected["facts"][0]["evidence_ids"].clear()
    assert loads_json(part.canonical_json)["facts"][0]["data"]["rows"]
    assert len(part.source_ids) == 3
    assert not hasattr(part, "__dict__")
    with pytest.raises(FrozenInstanceError):
        part.source_ids = ()


@pytest.mark.parametrize(
    "damage",
    [
        "missing_business",
        "bool_amount",
        "duplicate_evidence",
        "unknown_field",
        "duplicate_business",
        "wrong_month",
        "wrong_family",
    ],
)
def test_complete_fact_schema_and_shard_coherence_are_not_bypassed_by_normal_construction(damage):
    fact = deepcopy(next(value for value in _facts() if value["kind"] == "operations.expense"))
    value = shard(fact)
    if damage == "missing_business":
        del fact["business_id"]
    elif damage == "bool_amount":
        fact["data"]["amount_fen"] = True
    elif damage == "duplicate_evidence":
        fact["evidence_ids"] = ["10000000-0000-4000-8000-000000000001"] * 2
    elif damage == "unknown_field":
        fact["data"]["unexpected"] = "invalid"
    elif damage == "duplicate_business":
        value["facts"].append(deepcopy(fact))
    elif damage == "wrong_month":
        value["key"] = "facts:operations:2099-01"
    else:
        value["key"] = "facts:money:" + fact["period"]
    with pytest.raises(ContractError) as error:
        FactDocumentPart(value)
    assert error.value.code == "invalid_input"


@pytest.mark.parametrize("bad", [None, False, [], {}, "{}", {"facts": [object()]}])
def test_invalid_part_structure_has_a_contract_error(bad):
    with pytest.raises(ContractError):
        FactDocumentPart(bad)


def test_an_empty_complete_fact_shard_keeps_the_existing_schema_meaning():
    value = {"format": "ai-accounting-standard/1", "key": "facts:operations:2026-01", "facts": []}
    part = FactDocumentPart(value)
    assert part.canonical_json == canonical_json(value)
    assert part.business_ids == part.source_ids == ()
