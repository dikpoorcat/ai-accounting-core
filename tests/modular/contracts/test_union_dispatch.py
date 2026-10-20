"""Union pruning must preserve ordinary JSON Schema acceptance, including ambiguity."""

from copy import deepcopy
from itertools import product

from jsonschema import Draft202012Validator, validators
from referencing import Registry, Resource
from referencing.jsonschema import DRAFT202012

from ai_accounting_modular.contracts import _dispatch
from ai_accounting_modular.contracts._validation import _bundle, _Validator
from tests.modular.contracts.test_fact_sources import _facts


def test_required_literals_are_only_a_prefilter_not_an_alternative_validator(monkeypatch):
    schema = {
        "$id": "https://example.test/schema",
        "$defs": {
            "first": {
                "type": "object",
                "required": ["kind", "amount"],
                "properties": {"kind": {"const": "first"}, "amount": {"type": "integer"}},
                "additionalProperties": False,
            },
            "scoped": {
                "$id": "inner/",
                "$defs": {
                    "local": {
                        "type": "object",
                        "properties": {
                            "kind": {"enum": ["second", "shared"]},
                        },
                    }
                },
                "allOf": [{"$ref": "#/$defs/local"}, {"required": ["kind"]}],
            },
        },
        "oneOf": [
            {"$ref": "#/$defs/first"},
            {"$ref": "inner/"},
            {"properties": {"kind": {"const": "shared"}}, "required": ["kind"]},
            {"type": "integer", "const": 1},
            {"type": "string"},
        ],
        # These are data, not schemas to compile or resolve.
        "examples": [{"$ref": "not-a-schema", "required": 42}],
    }
    registry = (
        Registry()
        .with_resource(
            schema["$id"], Resource.from_contents(schema, default_specification=DRAFT202012)
        )
        .crawl()
    )
    monkeypatch.setattr(_dispatch, "_GUARDS", {})
    _dispatch.install_guards([(schema, registry.resolver(base_uri=schema["$id"]))])
    fast = validators.extend(Draft202012Validator, {"oneOf": _dispatch.one_of})(
        schema, registry=registry
    )
    oracle = Draft202012Validator(schema, registry=registry)
    values = [None, True, False, 1, 2, "first", [], {}]
    for kind, amount, extra in product(
        [None, 1, True, "first", "second", "shared", "unknown"], [None, 1, 1.5, "1"], [False, True]
    ):
        value = {"kind": kind, "amount": amount}
        if extra:
            value["extra"] = True
        values.extend([value, {key: item for key, item in value.items() if key != "amount"}])
    for value in values:
        assert fast.is_valid(value) == oracle.is_valid(value), value
    assert not fast.is_valid({"kind": "shared"})  # Both matching branches remain ambiguous.
    assert not fast.is_valid({"kind": "first", "amount": "1"})


def test_all_real_fact_kinds_match_unpruned_validation_with_invalid_variants():
    documents, registry = _bundle()
    uri = documents["business-facts.schema.json"]["$id"] + "#/$defs/Fact"
    unpruned = validators.extend(_Validator, {"oneOf": Draft202012Validator.VALIDATORS["oneOf"]})(
        {"$ref": uri}, registry=registry
    )
    fast = _Validator({"$ref": uri}, registry=registry)
    examples = {fact["kind"]: fact for fact in _facts()}
    for kind, fact in examples.items():
        assert fast.is_valid(fact) and unpruned.is_valid(fact), kind
        missing, wrong_kind, unknown_field = deepcopy(fact), deepcopy(fact), deepcopy(fact)
        del missing["data"]
        wrong_kind["kind"] = "unsupported.kind"
        unknown_field["not_a_contract_field"] = "1"
        for value in (missing, wrong_kind, unknown_field):
            assert not fast.is_valid(value) and not unpruned.is_valid(value), kind
