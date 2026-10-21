"""Source selectors share proven guards without losing ambiguous or conditional paths."""

from copy import deepcopy
from types import SimpleNamespace
from urllib.parse import urljoin

import pytest
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from ai_accounting_modular.contracts import _dispatch, _fact_sources
from ai_accounting_modular.contracts._validation import _bundle, _Validator
from tests.modular.contracts.test_fact_sources import _facts


@pytest.fixture
def compile_plan(monkeypatch):
    documents, _ = _bundle()
    document = deepcopy(documents["business-facts.schema.json"])
    monkeypatch.setattr(_dispatch, "_GUARDS", dict(_dispatch._GUARDS))

    def compile_node(node):
        document["$defs"]["SourcePlanTest"] = node
        selected_documents = documents | {"business-facts.schema.json": document}
        selected = Registry()
        for name, source in selected_documents.items():
            if "$id" not in source:
                continue
            resource = Resource.from_contents(source, default_specification=DRAFT202012)
            for uri in {source["$id"], urljoin(source["$id"], name)}:
                selected = selected.with_resource(uri, resource)
        selected = selected.crawl()
        _dispatch.install_guards([(document, selected.resolver(base_uri=document["$id"]))])
        monkeypatch.setattr(
            _fact_sources,
            "_bundle",
            lambda: (selected_documents, selected),
        )
        _fact_sources._source_plan.cache_clear()
        plan = _fact_sources._source_plan("SourcePlanTest")
        validator = _Validator(
            {"$ref": document["$id"] + "#/$defs/SourcePlanTest"}, registry=selected
        )
        return plan, validator

    yield compile_node
    _fact_sources._source_plan.cache_clear()


_LOCATOR = {"$ref": "core.schema.json#/$defs/SourceLocator"}
_A = "10000000-0000-4000-8000-000000000001"
_B = "10000000-0000-4000-8000-000000000002"


def test_any_of_collects_all_matching_branches_without_an_exclusive_discriminator(compile_plan):
    plan, validator = compile_plan(
        {
            "anyOf": [
                {"properties": {"a": _LOCATOR}, "required": ["a"]},
                {"properties": {"b": _LOCATOR}, "required": ["b"]},
            ]
        }
    )
    value = {"a": {"source_id": _A}, "b": {"source_id": _B}}
    assert validator.is_valid(value)
    assert tuple(plan.source_paths(value)) == ((_A, "/a/source_id"), (_B, "/b/source_id"))


@pytest.mark.parametrize("selected", ("first", "second", None))
def test_conditions_keep_the_actual_selected_source_branch(compile_plan, selected):
    plan, validator = compile_plan(
        {
            "if": {"properties": {"mode": {"const": "first"}}, "required": ["mode"]},
            "then": {"properties": {"a": _LOCATOR}, "required": ["a"]},
            "else": {"properties": {"b": _LOCATOR}, "required": ["b"]},
        }
    )
    value = {"mode": selected, "a": {"source_id": _A}, "b": {"source_id": _B}}
    if selected is None:
        del value["mode"]
    assert validator.is_valid(value)
    assert tuple(plan.source_paths(value)) == (
        ((_A, "/a/source_id"),) if selected == "first" else ((_B, "/b/source_id"),)
    )


def test_overlapping_one_of_branches_are_not_silently_made_exclusive(compile_plan):
    plan, validator = compile_plan(
        {
            "oneOf": [
                {"properties": {"a": _LOCATOR}, "required": ["a"]},
                {"properties": {"b": _LOCATOR}, "required": ["b"]},
            ]
        }
    )
    value = {"a": {"source_id": _A}, "b": {"source_id": _B}}
    assert not validator.is_valid(value)
    # The strict outer validator rejects this input; a selector must not hide
    # its ambiguity by pretending only the first possible branch matched.
    assert tuple(plan.source_paths(value)) == ((_A, "/a/source_id"), (_B, "/b/source_id"))


def test_impossible_real_fact_branches_do_not_receive_full_revalidation(monkeypatch):
    validate = _fact_sources._Validator
    calls = []

    def observed(schema, **kwargs):
        validator = validate(schema, **kwargs)

        def matches(value):
            calls.append(schema["$ref"])
            return validator.is_valid(value)

        return SimpleNamespace(is_valid=matches)

    monkeypatch.setattr(_fact_sources, "_Validator", observed)
    _fact_sources._source_plan.cache_clear()
    try:
        plan = _fact_sources._source_plan("Fact")
        fact = next(fact for fact in _facts() if fact["kind"] == "operations.expense")
        actual = tuple(plan.source_paths(fact))
        guarded_count = len(calls)
        calls.clear()
        monkeypatch.setattr(_fact_sources, "possible_branch", lambda branch, value: True)
        assert tuple(plan.source_paths(fact)) == actual
        assert guarded_count < len(calls) // 2
    finally:
        _fact_sources._source_plan.cache_clear()
