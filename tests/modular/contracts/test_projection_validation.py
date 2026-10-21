"""Selected fields retain their installed schemas, presence and strict JSON types."""

from copy import deepcopy

import pytest

from ai_accounting_modular.contracts import _projection_validation as runtime
from ai_accounting_modular.contracts.public import (
    MAX_FEN,
    ContractError,
    validate_contract,
    validate_contract_items,
)

ID = "00000000-0000-4000-8000-000000000001"
FIELDS = ("direction", "category", "party_id", "business_period")


def obligation():
    return {
        "direction": "payable",
        "category": "trade",
        "party_id": ID,
        "business_period": "2026-01",
    }


def test_full_batches_use_the_original_contract_and_return_unmodified_input():
    refs = [{"business_id": ID, "slot": "main"}]
    before = deepcopy(refs)
    for value in refs:
        validate_contract("core.schema.json", "BusinessRef", value)
    assert validate_contract_items("core.schema.json", "BusinessRef", refs) is refs
    assert refs == before
    assert validate_contract_items("core.schema.json", "BusinessRef", []) == []
    with pytest.raises(ContractError, match="/1/slot"):
        validate_contract_items("core.schema.json", "BusinessRef", [*refs, refs[0] | {"slot": 1}])


def test_fields_check_only_declared_properties_and_preserve_optional_presence():
    value = obligation()
    before = deepcopy(value)
    batch = [value, {key: val for key, val in value.items() if key != "party_id"}]
    assert validate_contract_items("core.schema.json", "Obligation", batch, fields=FIELDS) is batch
    assert value == before
    # No complete resource is promised by the selected-property check.
    with pytest.raises(ContractError):
        validate_contract("core.schema.json", "Obligation", value)
    with pytest.raises(ContractError):
        validate_contract_items(
            "core.schema.json", "Obligation", [value | {"party_id": None}], fields=FIELDS
        )
    with pytest.raises(ContractError):
        validate_contract_items(
            "core.schema.json", "Obligation", [value | {"account_code": "2202"}], fields=FIELDS
        )
    del value["business_period"]
    with pytest.raises(ContractError):
        validate_contract_items("core.schema.json", "Obligation", [value], fields=FIELDS)


@pytest.mark.parametrize(
    "schema,definition,field,valid,invalid",
    [
        ("core.schema.json", "Obligation", "business_period", "2026-01", "2026-01-02"),
        ("core.schema.json", "Obligation", "party_id", ID, "a" * 36),
        ("core.schema.json", "Obligation", "direction", "payable", "outflow"),
        ("core.schema.json", "Obligation", "amount_fen", str(MAX_FEN), str(MAX_FEN + 1)),
        ("core.schema.json", "Obligation", "account_code", "2202", "unknown"),
        ("business-facts.schema.json", "ResultSourceRef", "result_generation", 1, True),
        ("business-facts.schema.json", "ResultSourceRef", "result_generation", 1, 1.0),
        ("business-facts.schema.json", "TargetOperationsExpense", "kind", "operations.expense", 1),
    ],
)
def test_property_refs_retain_formats_constants_integer_precision_and_external_refs(
    schema, definition, field, valid, invalid
):
    validate_contract_items(schema, definition, [{field: valid}], fields=(field,))
    with pytest.raises(ContractError, match="/0/" + field):
        validate_contract_items(schema, definition, [{field: invalid}], fields=(field,))


@pytest.mark.parametrize("fields", [["direction"], ("direction", "direction"), (1,), ("unknown",)])
def test_only_installed_unique_object_properties_can_be_selected(fields):
    with pytest.raises(ContractError) as caught:
        validate_contract_items("core.schema.json", "Obligation", [], fields=fields)
    assert caught.value.code == "invalid_contract"


@pytest.mark.parametrize(
    "schema,definition", [("missing", "Obligation"), ("core.schema.json", "Fen")]
)
def test_projection_cannot_accept_an_arbitrary_schema_or_scalar_as_an_object(schema, definition):
    with pytest.raises(ContractError) as caught:
        validate_contract_items(schema, definition, [], fields=("value",))
    assert caught.value.code == "invalid_contract"


@pytest.mark.parametrize(
    "bad", [(obligation(),), [{"party_id": "\ud800"}], [{"party_id": float("nan")}]]
)
def test_batch_is_a_real_json_array_and_rejects_invalid_characters_and_constants(bad):
    with pytest.raises(ContractError):
        validate_contract_items("core.schema.json", "Obligation", bad, fields=FIELDS)


def test_no_accepted_value_is_cached_and_valid_batches_do_not_run_python_diagnostics(monkeypatch):
    def unexpected(*args):
        raise AssertionError("native success must not run the diagnostic interpreter")

    value = obligation()
    with monkeypatch.context() as hook:
        hook.setattr(runtime, "_diagnostic", unexpected)
        validate_contract_items("core.schema.json", "Obligation", [value], fields=FIELDS)
    value["category"] = "invented"
    with pytest.raises(ContractError):
        validate_contract_items("core.schema.json", "Obligation", [value], fields=FIELDS)


def test_nested_array_constraints_are_not_erased_by_a_top_level_projection():
    fields = ("allowed_settlements",)
    validate_contract_items(
        "core.schema.json", "Obligation", [{fields[0]: ["money"]}], fields=fields
    )
    for invalid in ([], ["invented"], ["money"] * 5):
        with pytest.raises(ContractError):
            validate_contract_items(
                "core.schema.json", "Obligation", [{fields[0]: invalid}], fields=fields
            )


def test_dashboard_root_properties_resolve_within_the_installed_namespace():
    batch = [{"schema_version": 3}]
    validate_contract_items(
        "dashboard-response-v2.json", "dashboard_context", batch, fields=("schema_version",)
    )
    with pytest.raises(ContractError):
        validate_contract_items(
            "dashboard-response-v2.json",
            "dashboard_context",
            [{"schema_version": True}],
            fields=("schema_version",),
        )
