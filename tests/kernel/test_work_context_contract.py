"""Native work reads publish a small strict contract without business defaults."""

from copy import deepcopy

import pytest
from jsonschema import Draft202012Validator
from pydantic import ValidationError
from test_work_context import company as company_fixture
from test_work_context import expense, query, source

from ai_accounting.kernel.discovery import Discovery
from ai_accounting.kernel.work_context_contract import WORK_CONTEXT_ADAPTER


@pytest.fixture
def company(tmp_path):
    return company_fixture.__wrapped__(tmp_path)


@pytest.fixture
def populated(company):
    Discovery(company).update_company_note(
        "confirmed company background",
        expected_revision=0,
        request_id="company-note",
        evidence_digest=company.proof,
    )
    _, evidence = source(company, "original")
    expense(company, "posted", evidence=evidence, publish=True)
    expense(company, "pending")
    return query(company)


def test_real_populated_context_validates_without_changes(populated):
    validated = WORK_CONTEXT_ADAPTER.validate_python(populated)
    assert validated == populated
    assert WORK_CONTEXT_ADAPTER.dump_python(validated, mode="json") == populated
    assert populated["materials"]
    assert populated["entities"]
    assert any(item["adoption"] is not None for item in populated["items"])


def test_real_empty_context_validates_without_changes(company):
    empty = query(company)
    assert empty["items"] == empty["materials"] == empty["entities"] == []
    assert WORK_CONTEXT_ADAPTER.validate_python(empty) == empty


@pytest.mark.parametrize("damage", ["extra", "missing_has_more", "bool_version", "string_has_more"])
def test_outer_shape_and_native_scalar_types_are_strict(populated, damage):
    damaged = deepcopy(populated)
    if damage == "extra":
        damaged["unregistered_field"] = "not allowed"
    elif damage == "missing_has_more":
        damaged.pop("has_more")
    elif damage == "bool_version":
        damaged["schema_version"] = True
    else:
        damaged["has_more"] = "false"
    with pytest.raises(ValidationError):
        WORK_CONTEXT_ADAPTER.validate_python(damaged)


@pytest.mark.parametrize("section", ["fact", "source", "profile", "database_format"])
def test_nested_objects_do_not_accept_unknown_structural_fields(populated, section):
    damaged = deepcopy(populated)
    if section == "fact":
        target = damaged["items"][0]
    elif section == "source":
        target = damaged["materials"][0]["source"]
    elif section == "profile":
        target = damaged["entities"][0]["profile"]
    else:
        target = damaged["company_context"]["database_format"]
    target["unregistered_field"] = "not allowed"
    with pytest.raises(ValidationError):
        WORK_CONTEXT_ADAPTER.validate_python(damaged)


def test_optional_original_profile_fields_are_not_defaulted(populated):
    actual = deepcopy(populated)
    profile = actual["entities"][0]["profile"]
    for field in ("active", "employment_status", "external_identifiers", "note"):
        profile.pop(field)
    validated = WORK_CONTEXT_ADAPTER.validate_python(actual)
    assert validated == actual
    assert "active" not in validated["entities"][0]["profile"]
    assert "employment_status" not in validated["entities"][0]["profile"]


def test_source_body_is_existing_json_and_not_registration_union(populated):
    schema = WORK_CONTEXT_ADAPTER.json_schema()
    fields = schema["$defs"]["ContextSourceFact"]["properties"]
    assert fields["data"]["type"] == "object"
    assert fields["data"]["additionalProperties"] == {"$ref": "#/$defs/JsonValue"}
    body = populated["items"][0]["data"]
    assert WORK_CONTEXT_ADAPTER.validate_python(populated)["items"][0]["data"] == body


def test_contract_local_definitions_are_closed_and_describe_real_json(populated):
    schema = WORK_CONTEXT_ADAPTER.json_schema()
    Draft202012Validator.check_schema(schema)
    references = []

    def visit(value):
        if isinstance(value, dict):
            if "$ref" in value:
                references.append(value["$ref"])
            for item in value.values():
                visit(item)
        elif isinstance(value, list):
            for item in value:
                visit(item)

    visit(schema)
    assert references
    for reference in references:
        assert reference.startswith("#/$defs/")
        assert reference.removeprefix("#/$defs/") in schema["$defs"]
    Draft202012Validator(schema).validate(
        WORK_CONTEXT_ADAPTER.dump_python(
            WORK_CONTEXT_ADAPTER.validate_python(populated), mode="json"
        )
    )
