"""Stored-plan source assessments preserve all business data and identities."""

from copy import deepcopy

import pytest
from test_replay_document import compile_document, document

from ai_accounting.kernel.replay_document import ReplayDocumentCompileError
from ai_accounting.kernel.replay_registration_dependencies import (
    add_replay_registration_preparations,
)


def source_plan():
    request = compile_document(document())["request"]
    step = {
        "step_id": "expense-source", "command": "save_facts", "depends_on": [],
        "payload": {"facts": [{
            "kind": "expense", "subject_id": "existing-business-subject",
            "data": {"period": "2026-01", "amount_fen": 12000},
            "expected_revision": 0, "evidence": ["a" * 64],
            "source_locations": [{"source_id": "actual-current-source",
                                  "source_fact_id": "actual-current-source-fact",
                                  "evidence_digest": "a" * 64, "location": "CSV!F9"}],
        }]},
    }
    request["steps"].insert(3, step)
    return request, step


def test_inserted_preparation_uses_exact_sources_and_only_real_review_output():
    original, save = source_plan()
    frozen = deepcopy(original)
    result = add_replay_registration_preparations(original)
    assert original == frozen
    assert result["request"]["request_id"] == original["request_id"]
    assert result["request"]["source_manifest"] == original["source_manifest"]
    prep_id = result["added_preparations"][0]["preparation_step_id"]
    steps = {row["step_id"]: row for row in result["request"]["steps"]}
    prep, amended = steps[prep_id], steps[save["step_id"]]
    original_record = save["payload"]["facts"][0]
    assert prep["payload"]["source_locations"] == original_record["source_locations"]
    basis = prep["payload"]["separation_intent"]["review_basis"][0]
    assert basis == dict(original_record["source_locations"][0], kind="distinct_material_location")
    assert amended["payload"]["facts"][0] == dict(
        original_record, review={"$replay_ref": prep["output_ref"], "field": "review"}
    )
    assert amended["depends_on"] == [prep_id]
    assert prep["output_fields"] == ["review"]
    again = add_replay_registration_preparations(result["request"])
    assert again["request"] == result["request"] and not again["added_preparations"]


def test_completed_sources_and_unpositioned_sources_are_not_changed():
    request, step = source_plan()
    assert add_replay_registration_preparations(
        request, completed_step_ids=[step["step_id"]]
    )["request"] == request
    step["payload"]["facts"][0]["source_locations"] = []
    assert add_replay_registration_preparations(request)["request"] == request


@pytest.mark.parametrize("case,code", [
    ("batch", "registration_preparation_batch_requires_review"),
    ("review", "registration_existing_review_requires_check"),
    ("unknown", "registration_completed_step_unknown"),
])
def test_unestablished_reviews_are_explicitly_rejected(case, code):
    request, step = source_plan()
    ids = []
    if case == "batch":
        step["payload"]["facts"].append(deepcopy(step["payload"]["facts"][0]))
    elif case == "review":
        step["payload"]["facts"][0]["review"] = {"unverified": "review"}
    else:
        ids = ["not-this-plan"]
    with pytest.raises(ReplayDocumentCompileError) as exc:
        add_replay_registration_preparations(request, completed_step_ids=ids)
    assert exc.value.issues[0]["code"] == code
