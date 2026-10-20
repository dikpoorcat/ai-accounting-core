from __future__ import annotations

import inspect
from copy import deepcopy
from dataclasses import FrozenInstanceError, fields
from pathlib import Path
from typing import get_args, get_type_hints

import pytest

from ai_accounting_modular.contracts import public
from ai_accounting_modular.contracts.public import (
    AdoptedBasis,
    ContractError,
    CurrentObject,
    Dependency,
    DependencyRelationKind,
    EvaluatedSlot,
    EvaluationContext,
    Fact,
    JsonValue,
    ReadContext,
    ResultSourceRef,
    ResultTarget,
    SourceLocator,
    content_digest,
    loads_json,
    schema_document,
)

EXAMPLES = Path(__file__).resolve().parents[3] / "docs/redesign/examples"


def outcome():
    examples = loads_json((EXAMPLES / "business-facts-cases.json").read_bytes())
    plan = examples["investment_lifecycle"]["plan_examples"]["company_funding"]
    source = {
        "business_id": plan["business_id"],
        "result_slot": plan["result_slot"],
        "result_id": "00000000-0000-4000-8000-000000000001",
        "result_generation": 1,
        "content_digest": content_digest(
            {
                "calculation_key": plan["calculation_key"],
                "calculation_revision": plan["calculation_revision"],
                "basis_digest": content_digest(plan["basis"]),
                "target_digest": content_digest(plan["target"]),
            }
        ),
    }
    return source, plan["target"], plan["basis"]


def test_evaluated_slot_is_an_exported_frozen_public_outcome_not_a_posting_plan():
    slot = EvaluatedSlot(*outcome())
    assert [field.name for field in fields(slot)] == ["source", "target", "basis"]
    assert not any(hasattr(slot, name) for name in ["plan", "lines", "vouchers", "repository"])
    assert public.EvaluatedSlot is EvaluatedSlot
    assert public.EvaluationContext is EvaluationContext
    assert {"EvaluatedSlot", "EvaluationContext"} <= set(public.__all__)
    assert get_type_hints(EvaluatedSlot) == {
        "source": ResultSourceRef,
        "target": ResultTarget,
        "basis": AdoptedBasis,
    }
    with pytest.raises(FrozenInstanceError):
        slot.source = {}


@pytest.mark.parametrize("component", ["source", "target", "basis"])
def test_evaluated_slot_reuses_strict_wire_validation(component):
    values = dict(zip(["source", "target", "basis"], outcome(), strict=True))
    values[component] = deepcopy(values[component])
    values[component]["unexpected"] = True
    with pytest.raises(ContractError) as error:
        EvaluatedSlot(**values)
    assert error.value.code == "invalid_input"


def test_evaluated_slot_rejects_mixed_result_slots():
    source, target, basis = outcome()
    source["result_slot"] = "different_slot"
    with pytest.raises(ContractError, match="同一结果槽"):
        EvaluatedSlot(source, target, basis)


def test_evaluation_context_has_one_typed_protocol_for_public_dependencies():
    assert isinstance(EvaluationContext.read_context, property)
    assert get_type_hints(EvaluationContext.read_context.fget)["return"] is ReadContext
    assert get_type_hints(EvaluationContext.get_fact)["return"] == Fact | None
    assert get_type_hints(EvaluationContext.related_facts)["return"] == tuple[Fact, ...]
    assert (
        get_type_hints(EvaluationContext.related_facts)["relation_kind"] is DependencyRelationKind
    )
    assert get_type_hints(EvaluationContext.get_object)["return"] == CurrentObject | None
    assert get_type_hints(EvaluationContext.get_result)["return"] == EvaluatedSlot | None
    assert get_type_hints(EvaluationContext.resolve)["return"] == list[JsonValue]
    assert get_type_hints(EvaluationContext.resolve_sources)["return"] == list[SourceLocator]
    assert list(inspect.signature(EvaluationContext.get_result).parameters) == [
        "self",
        "business_id",
        "result_slot",
    ]
    assert not any(
        hasattr(EvaluationContext, name)
        for name in ["connection", "execute", "commit", "posting_plan", "get_plan"]
    )


def test_related_facts_reuses_the_generated_dependency_enum():
    definition = schema_document("business-facts.schema.json")["$defs"]["Dependency"]
    expected = tuple(definition["properties"]["relation_kind"]["enum"])
    assert get_args(DependencyRelationKind.__value__) == expected
    assert get_type_hints(Dependency)["relation_kind"] is DependencyRelationKind
    assert public.DependencyRelationKind is DependencyRelationKind
    assert list(inspect.signature(EvaluationContext.related_facts).parameters) == [
        "self",
        "producer_id",
        "relation_kind",
    ]
