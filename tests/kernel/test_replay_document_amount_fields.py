"""Reject obsolete material dimensions before any replay database is created."""

from copy import deepcopy

import pytest

from ai_accounting.kernel.replay_document_materials import material_amount_field_issues


def linked_document(field="fact.accounting_gross_salary_fen", *, group=False):
    return {
        "facts": [{"key": "wage", "kind": "payroll_bounded", "data": {}}],
        "steps": [],
        "materials": [
            {
                "key": "original-group" if group else "original-row",
                "kind": "material_group_resolution" if group else "material_resolution_v2",
                "data": {
                    "links": [
                        {
                            "calculation_id": "$ref:wage:calculation_id",
                            "amount_field": field,
                            "amount_fen": 100_000,
                        }
                    ]
                },
            }
        ],
    }


@pytest.mark.parametrize("group", [False, True])
def test_current_model_rejects_retired_field_even_if_raw_document_keeps_it(group):
    document = linked_document("fact.tax_reported_salary_fen", group=group)
    document["facts"][0]["data"]["tax_reported_salary_fen"] = 100_000
    before = deepcopy(document)
    issues = material_amount_field_issues(document)
    assert len(issues) == 1
    assert issues[0]["path"] == document["materials"][0]["key"] + "/data/links/0/amount_field"
    assert issues[0]["code"] == "material_amount_field_invalid"
    assert issues[0]["target_kind"] == "payroll_bounded"
    assert issues[0]["allowed_fields"] == ["fact.accounting_gross_salary_fen"]
    assert document == before


@pytest.mark.parametrize("field", ["fact.accounting_gross_salary_fen", "result.gross_fen"])
def test_current_wage_alias_dimensions_remain_valid_without_synthetic_calculation(field):
    assert not material_amount_field_issues(linked_document(field))


@pytest.mark.parametrize("kind", [None, "unknown_kind", "payroll_opening_state"])
def test_unresolved_or_historical_target_cannot_supply_a_fact_amount(kind):
    document = linked_document()
    if kind is None:
        document["facts"] = []
    else:
        document["facts"][0]["kind"] = kind
    issue = material_amount_field_issues(document)[0]
    assert issue["code"] == "material_amount_target_kind_unknown"
    assert issue["allowed_fields"] == []


def test_generated_output_kind_is_resolved_without_fabricating_its_amount():
    document = linked_document("fact.residual_fen")
    document["facts"] = []
    document["steps"] = [
        {"key": "prepare-and-confirm", "outputs": [{"key": "wage", "kind": "asset_activation"}]}
    ]
    assert not material_amount_field_issues(document)
    document["steps"].append(
        {"key": "ambiguous-output", "outputs": [{"key": "wage", "kind": "payroll_bounded"}]}
    )
    assert (
        material_amount_field_issues(document)[0]["code"] == "material_amount_target_kind_unknown"
    )


def test_integer_dimension_check_does_not_accept_decimal_string_or_boolean_fields():
    from typing import ClassVar

    from ai_accounting.kernel.contracts import Fact, Registry

    class TypedAmounts(Fact):
        kind: ClassVar[str] = "typed_amounts"
        integer_fen: int
        nullable_fen: int | None
        text_fen: str
        flag_fen: bool
        # Even an explicit alias must not revive an absent input field.
        material_amount_aliases: ClassVar[dict[str, str]] = {
            "fact.absent_fen": "result.integer_fen"
        }

    registry = Registry()
    registry.models[TypedAmounts.kind] = TypedAmounts
    document = linked_document("fact.text_fen")
    document["facts"][0]["kind"] = TypedAmounts.kind
    issue = material_amount_field_issues(document, registry=registry)[0]
    assert issue["allowed_fields"] == ["fact.integer_fen", "fact.nullable_fen"]
    for field in ("fact.flag_fen", "fact.absent_fen"):
        document["materials"][0]["data"]["links"][0]["amount_field"] = field
        assert material_amount_field_issues(document, registry=registry)[0]["code"] == (
            "material_amount_field_invalid"
        )


def test_pure_compiler_blocks_retired_material_dimension_before_returning_a_request():
    from test_replay_document import compile_document
    from test_replay_document_remuneration import remuneration_document

    from ai_accounting.kernel.replay_document import ReplayDocumentCompileError

    document = remuneration_document("payroll_bounded")
    document["materials"] = linked_document()["materials"]
    # Use a valid material body so this is an amount semantic error, not an
    # unrelated material schema error. No source file or database is consulted.
    document["materials"][0]["data"].update(
        period="2026-01",
        source_id="$ref:original:subject_id",
        source_fact_id="$ref:original:fact_id",
        location="L1",
        treatment="recognize",
        amount_fen=100_000,
        recognition_period="2026-01",
    )
    document["materials"][0]["source_keys"] = ["owner-original"]
    proof = document["sources"][0]["sha256"]
    document["materials"].append(
        {
            "key": "original",
            "kind": "material_source_v2",
            "source_keys": ["owner-original"],
            "data": {
                "period": "2026-01",
                "evidence_digest": proof,
                "category": "payroll",
                "purpose": "business",
                "specification": {"format": "text", "passages": [], "all_pages_reviewed": True},
            },
        }
    )
    for row in document["inventory_replay_plan"]["rows"]:
        if row["category"] == "payroll":
            row["no_business"] = False
            row["source_coverage"] = [{"source_key": "owner-original", "sha256": proof}]
    assert not compile_document(document)["issues"]
    document["materials"][0]["data"]["links"][0]["amount_field"] = "fact.tax_reported_salary_fen"
    with pytest.raises(ReplayDocumentCompileError) as error:
        compile_document(document)
    issues = [x for x in error.value.issues if x["code"] == "material_amount_field_invalid"]
    assert len(issues) == 1
    assert issues[0]["path"] == "original-row/data/links/0/amount_field"
    assert "fact.accounting_gross_salary_fen" in issues[0]["message"]
    assert issues[0]["allowed_fields"] == ["fact.accounting_gross_salary_fen"]
