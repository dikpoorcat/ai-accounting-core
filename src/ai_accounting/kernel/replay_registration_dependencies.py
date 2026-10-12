"""Add exact duplicate preparations to already declared source registrations."""

from __future__ import annotations

from copy import deepcopy

from pydantic import ValidationError

from .contracts import KernelError
from .duplicates import ELIGIBLE_KINDS
from .replay_contracts import ReplayPrepare
from .replay_document import ReplayDocumentCompileError
from .replay_runner import compile_replay_steps
from .types import canonical, digest


def add_replay_registration_preparations(original_request, *, completed_step_ids=()):
    """Insert read-only preparations without reassigning any business identity.

    Only registrations already carrying exact typed material positions receive
    a separation intent. The intent does not assert that every candidate is
    distinct: the normal preparation checks the current candidate's locations,
    and the atomic save repeats that check with its actual candidate digest.
    Completed nodes are immutable and must be identified by the caller's exact
    receipts before applying this helper to a partially executed request.
    """
    try:
        ReplayPrepare.model_validate_json(canonical(original_request))
    except (ValidationError, ValueError, TypeError) as exc:
        raise ReplayDocumentCompileError(
            [{"path": "request", "code": "registration_plan_invalid", "message": str(exc)}]
        ) from exc
    completed = set(completed_step_ids)
    request = deepcopy(original_request)
    existing = {step["step_id"]: step for step in request["steps"]}
    if not completed.issubset(existing):
        raise ReplayDocumentCompileError(
            [
                {
                    "path": "completed_step_ids",
                    "code": "registration_completed_step_unknown",
                    "message": "completed IDs must belong to this request",
                }
            ]
        )
    added, new_steps = [], []
    for step in request["steps"]:
        records = step["payload"].get("facts", []) if step["command"] == "save_facts" else []
        eligible = [
            record
            for record in records
            if record.get("kind") in ELIGIBLE_KINDS and record.get("source_locations")
        ]
        if step["step_id"] in completed or not eligible:
            new_steps.append(step)
            continue
        if len(records) != 1:
            raise ReplayDocumentCompileError(
                [
                    {
                        "path": step["step_id"],
                        "code": "registration_preparation_batch_requires_review",
                        "message": "batch peers need a joint current candidate assessment",
                    }
                ]
            )
        record = eligible[0]
        preparation_id = "s_regprep_" + digest([step["step_id"], record["subject_id"]]).hex()
        output_ref = "r_regprep_" + digest([step["step_id"], record["subject_id"]]).hex()
        review_ref = {"$replay_ref": output_ref, "field": "review"}
        if preparation_id in existing:
            if record.get("review") != review_ref:
                raise ReplayDocumentCompileError(
                    [
                        {
                            "path": step["step_id"],
                            "code": "registration_preparation_conflict",
                            "message": "existing preparation supplies a different review",
                        }
                    ]
                )
            new_steps.append(step)
            continue
        if record.get("review") is not None:
            raise ReplayDocumentCompileError(
                [
                    {
                        "path": step["step_id"],
                        "code": "registration_existing_review_requires_check",
                        "message": "an explicitly selected review cannot be replaced automatically",
                    }
                ]
            )
        basis = [
            dict(deepcopy(location), kind="distinct_material_location")
            for location in record["source_locations"]
        ]
        payload = {
            key: deepcopy(record[key])
            for key in (
                "kind",
                "subject_id",
                "data",
                "evidence",
                "expected_revision",
                "source_locations",
            )
        }
        payload["separation_intent"] = {
            "action": "create_separate",
            "explanation": (
                "The final replay document declares this typed business separately at its exact "
                "preserved material positions. Separation requires current candidates to have "
                "different verified positions."
            ),
            "review_basis": basis,
        }
        preparation = {
            "step_id": preparation_id,
            "command": "prepare_fact_registration",
            "payload": payload,
            "depends_on": deepcopy(step.get("depends_on", [])),
            "output_ref": output_ref,
            "output_fields": ["review"],
        }
        step.setdefault("depends_on", []).append(preparation_id)
        record["review"] = review_ref
        new_steps.extend((preparation, step))
        added.append(
            {
                "save_step_id": step["step_id"],
                "preparation_step_id": preparation_id,
                "kind": record["kind"],
                "subject_id": deepcopy(record["subject_id"]),
                "output_ref": output_ref,
            }
        )
    request["steps"] = new_steps
    try:
        validated = ReplayPrepare.model_validate_json(canonical(request))
        if validated.continuation_from is None:
            compile_replay_steps(validated.steps)
        # A continuation's external parent dependencies are checked by its
        # explicit actual checkpoint chain, never fabricated by this helper.
    except (ValidationError, ValueError, KernelError) as exc:
        raise ReplayDocumentCompileError(
            [{"path": "request", "code": "registration_plan_invalid", "message": str(exc)}]
        ) from exc
    return {"request": request, "added_preparations": added, "issues": []}
