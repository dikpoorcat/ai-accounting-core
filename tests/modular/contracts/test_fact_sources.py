"""Independent fixture identities exercise every declared Fact variant."""

from __future__ import annotations

import json
from copy import deepcopy
from functools import lru_cache
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

import pytest

from ai_accounting_modular.contracts.public import (
    ContractError,
    fact_source_ids,
    fact_source_paths,
    schema_document,
    validate_contract,
)

EXAMPLES = Path(__file__).resolve().parents[3] / "docs/redesign/examples"


def _objects(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from _objects(child)
    elif isinstance(value, list):
        for child in value:
            yield from _objects(child)


@lru_cache(maxsize=1)
def _facts():
    result = []
    for path in sorted(EXAMPLES.glob("*.json")):
        for value in _objects(json.loads(path.read_text(encoding="utf-8"))):
            if not {"business_id", "kind", "period", "evidence_ids", "data"} <= value.keys():
                continue
            try:
                validate_contract("business-facts.schema.json", "Fact", value)
            except ContractError:
                continue
            result.append(value)
    return tuple(result)


def _kinds():
    definitions = schema_document("business-facts.schema.json")["$defs"]

    def visit(node):
        if "$ref" in node:
            return visit(definitions[node["$ref"].split("/")[-1]])
        if "oneOf" in node:
            return set().union(*(visit(child) for child in node["oneOf"]))
        return {node["properties"]["kind"]["const"]}

    return visit(definitions["Fact"])


def _locator_objects(fact):
    # Independent fixture oracle: the examples' strict locators contain only
    # these two fields. Production instead follows their schema references.
    return [
        value
        for value in _objects(fact)
        if "source_id" in value and value.keys() <= {"source_id", "location"}
    ]


def test_all_fact_variants_and_existing_fixture_sources_are_covered():
    assert {fact["kind"] for fact in _facts()} == _kinds()
    for fact in _facts():
        before = deepcopy(fact)
        # Existing examples deliberately reuse their documented top-level
        # source for every nested locator.
        assert fact_source_ids(fact) == tuple(sorted(set(fact["evidence_ids"])))
        assert fact == before


def test_every_actual_nested_locator_can_independently_own_its_source_identity():
    for fact_index, original in enumerate(_facts()):
        fact = deepcopy(original)
        expected = set(fact["evidence_ids"])
        for index, locator in enumerate(_locator_objects(fact)):
            source = str(uuid5(NAMESPACE_URL, f"fact-source-test:{fact_index}:{index}"))
            locator["source_id"] = source
            expected.add(source)
        assert fact_source_ids(fact) == tuple(sorted(expected)), fact["kind"]


@pytest.mark.parametrize(
    ("kind", "path"),
    [
        ("money.bank_statement", ("data", "rows", 0, "source")),
        ("money.platform_activity", ("data", "rows", 0, "source")),
        ("money.movement", ("data", "return_source")),
        ("money.reserve_internal", ("data", "boundary_source")),
        ("operations.service_sale", ("data", "tax_point", "source")),
        ("people.remuneration", ("data", "tax_basis", "source")),
        ("taxes.tax_point", ("data", "events", 0, "source")),
    ],
)
def test_explicit_real_nested_source_fields_need_no_duplicate_evidence(kind, path):
    def location(fact):
        value = fact
        for component in path:
            value = value[component]
        return value

    chosen = None
    for candidate in _facts():
        if candidate["kind"] != kind:
            continue
        try:
            location(candidate)
        except (KeyError, IndexError, TypeError):
            continue
        chosen = deepcopy(candidate)
        break
    assert chosen is not None
    chosen["evidence_ids"] = []
    source_id = str(uuid5(NAMESPACE_URL, "explicit-source:" + kind))
    for locator in _locator_objects(chosen):
        locator["source_id"] = source_id
    assert fact_source_ids(chosen) == (source_id,)


def test_business_references_are_not_source_locators():
    fact = deepcopy(
        next(
            fact
            for fact in _facts()
            if fact["kind"] == "money.movement" and fact["data"]["type"] == "settlement"
        )
    )
    fact["evidence_ids"] = []
    assert fact["data"]["allocations"][0]["source"]["business_id"]
    assert fact_source_ids(fact) == ()
    # An arbitrary source_id cannot be smuggled into a BusinessRef.
    fact["data"]["allocations"][0]["source"]["source_id"] = str(
        uuid5(NAMESPACE_URL, "not-a-locator")
    )
    with pytest.raises(ContractError):
        fact_source_ids(fact)


def test_opening_member_source_ids_are_indexed_without_duplicate_root_evidence():
    fact = deepcopy(
        next(
            fact
            for fact in _facts()
            if fact["kind"] == "opening.package" and fact["data"]["members"]
        )
    )
    fact["evidence_ids"] = []
    expected = {}
    for index, member in enumerate(fact["data"]["members"]):
        source = str(uuid5(NAMESPACE_URL, f"opening-member-source:{index}"))
        member["evidence_ids"] = [source]
        expected[source] = (f"/data/members/{index}/evidence_ids/0",)
    # Explicit member sources must be distinct from ordinary object/member IDs.
    assert fact_source_paths(fact) == dict(sorted(expected.items()))
    assert fact_source_ids(fact) == tuple(sorted(expected))


def test_thousand_top_level_sources_plus_nested_source_have_no_false_limit():
    fact = deepcopy(next(fact for fact in _facts() if fact["kind"] == "money.bank_statement"))
    evidence = [str(uuid5(NAMESPACE_URL, f"top-source:{index}")) for index in range(1000)]
    nested = str(uuid5(NAMESPACE_URL, "nested-source"))
    fact["evidence_ids"] = evidence
    first = fact["data"]["rows"][0]
    first["source"] = {"source_id": nested, "location": "第一页"}
    second = deepcopy(first)
    second["row_id"] = str(uuid5(NAMESPACE_URL, "second-bank-row"))
    second["source"]["location"] = "第二页"
    fact["data"]["rows"] = [first, second]
    expected = tuple(sorted({*evidence, nested}))
    assert len(expected) == 1001 and fact_source_ids(fact) == expected
    fact["evidence_ids"].reverse()
    fact["data"]["rows"].reverse()
    assert fact_source_ids(fact) == expected


def test_extraction_rejects_invalid_fact_instead_of_guessing_its_shape():
    fact = deepcopy(_facts()[0])
    fact["kind"] = "unrecognized.kind"
    with pytest.raises(ContractError):
        fact_source_ids(fact)


def test_source_paths_preserve_all_array_locations_and_nested_missing_source_field():
    fact = deepcopy(next(fact for fact in _facts() if fact["kind"] == "money.bank_statement"))
    source = fact["evidence_ids"][0]
    nested = str(uuid5(NAMESPACE_URL, "only-nested-missing-source"))
    first = fact["data"]["rows"][0]
    first["source"] = {"source_id": source, "location": "第一页"}
    second, third = deepcopy(first), deepcopy(first)
    second["row_id"] = str(uuid5(NAMESPACE_URL, "source-path-row-two"))
    second["source"]["location"] = "第二页"
    third["row_id"] = str(uuid5(NAMESPACE_URL, "source-path-row-three"))
    third["source"] = {"source_id": nested, "location": "第三页"}
    fact["data"]["rows"] = [first, second, third]
    paths = fact_source_paths(fact)
    assert paths[source] == (
        "/data/rows/0/source/source_id",
        "/data/rows/1/source/source_id",
        "/evidence_ids/0",
    )
    assert paths[nested] == ("/data/rows/2/source/source_id",)
    assert tuple(paths) == fact_source_ids(fact) == tuple(sorted((source, nested)))
    # Every reported pointer resolves to that exact identity in the submitted fact.
    for expected_id, pointers in paths.items():
        for pointer in pointers:
            value = fact
            for part in pointer[1:].split("/"):
                part = part.replace("~1", "/").replace("~0", "~")
                value = value[int(part)] if isinstance(value, list) else value[part]
            assert value == expected_id


def test_compiled_paths_escape_json_pointer_property_names():
    # Current Fact names contain no '/' or '~'. Exercise the same compiled
    # property/array collector without inventing an unsupported public Fact.
    from ai_accounting_modular.contracts._fact_sources import _Plan

    source = str(uuid5(NAMESPACE_URL, "escaped-source-path"))
    plan = _Plan(properties=(("a~/b", _Plan(items=_Plan(capture=True))),))
    assert tuple(plan.source_paths({"a~/b": [{"source_id": source}]})) == (
        (source, "/a~0~1b/0/source_id"),
    )
