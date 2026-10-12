"""Pure verification of explicitly linked, actual replay checkpoint receipts."""

from __future__ import annotations

from copy import deepcopy

from pydantic import ValidationError

from .contracts import KernelError
from .replay_contracts import ReplayPrepare, ReplayStep
from .replay_document import ReplayDocumentCompileError
from .replay_runner import (
    _output_field,
    _request_key,
    compile_replay_steps,
    validate_replay_sequence,
)
from .types import canonical, digest


def _fail(path, code, message):
    raise ReplayDocumentCompileError([{"path": path, "code": code, "message": message}])


def replace_known_outputs(value, bindings, references, *, strict=False):
    if isinstance(value, dict):
        if set(value) == {"$replay_ref", "field"}:
            pair = value["$replay_ref"], value["field"]
            if pair in bindings:
                return deepcopy(bindings[pair])
            if strict or pair[0] in references:
                _fail(
                    canonical(value),
                    "continuation_output_missing",
                    "actual declared output is missing",
                )
            return deepcopy(value)
        return {
            key: replace_known_outputs(child, bindings, references, strict=strict)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            replace_known_outputs(child, bindings, references, strict=strict) for child in value
        ]
    return deepcopy(value)


def verify_checkpoint_receipts(
    original, completed_receipts, *, ancestor_steps=(), ancestor_bindings=None
):
    """Return only actual completed nodes, their literal outputs and proof metadata."""
    ancestor_bindings = ancestor_bindings or {}
    declared_outputs = {step.output_ref for step in original.steps if step.output_ref}
    available_bindings = {
        pair: value for pair, value in ancestor_bindings.items() if pair[0] not in declared_outputs
    }
    available_references = {pair[0] for pair in available_bindings}
    current_review_periods = {
        step.payload.get("period") for step in original.steps if step.command == "preview_close"
    }
    replaced_reviews = {
        step.step_id
        for step in ancestor_steps
        if step.command == "preview_close" and step.payload.get("period") in current_review_periods
    }
    ancestor_steps = tuple(
        step.model_copy(
            update={
                "depends_on": tuple(key for key in step.depends_on if key not in replaced_reviews)
            }
        )
        for step in ancestor_steps
        if step.step_id not in replaced_reviews
    )
    current_steps = []
    for step in original.steps:
        item = step.model_dump(mode="json")
        item["payload"] = replace_known_outputs(
            item["payload"], available_bindings, available_references
        )
        item["output_selectors"] = replace_known_outputs(
            item["output_selectors"], available_bindings, available_references
        )
        current_steps.append(ReplayStep.model_validate_json(canonical(item)))
    ancestor_ids = {step.step_id for step in ancestor_steps}
    if ancestor_ids.intersection(step.step_id for step in current_steps):
        _fail(
            "ancestor_checkpoints",
            "continuation_ancestor_step_collision",
            "a current step cannot replace a completed ancestor",
        )
    try:
        order, edges = compile_replay_steps((*ancestor_steps, *current_steps))
        validate_replay_sequence(order, edges, original.first_period, original.last_period)
    except (KernelError, ValueError) as exc:
        code = (
            "continuation_ancestral_context_required"
            if original.continuation_from
            else "continuation_original_invalid"
        )
        _fail("original_request", code, str(exc))
    by_id = {step.step_id: step for step in current_steps}
    receipts = {}
    if not isinstance(completed_receipts, (list, tuple)):
        _fail(
            "completed_receipts",
            "continuation_receipts_invalid",
            "complete receipt objects are required",
        )
    for index, receipt in enumerate(completed_receipts):
        path = f"completed_receipts/{index}"
        if not isinstance(receipt, dict) or not isinstance(receipt.get("step_id"), str):
            _fail(path, "continuation_receipt_invalid", "receipt must have an exact step ID")
        step_id = receipt["step_id"]
        if step_id in receipts:
            _fail(path, "continuation_duplicate_receipt", "one step cannot have multiple receipts")
        step = by_id.get(step_id)
        if step is None:
            _fail(path, "continuation_unknown_step", "receipt is not from this declared request")
        if receipt.get("command") != step.command:
            _fail(
                path,
                "continuation_command_mismatch",
                "receipt command differs from its original step",
            )
        if receipt.get("request_id") != _request_key(original.request_id, step_id):
            _fail(
                path,
                "continuation_request_mismatch",
                "receipt is not the original stable node request",
            )
        response = receipt.get("response")
        if (
            receipt.get("status") != "completed"
            or not isinstance(response, dict)
            or response.get("status") in {"needs_information", "blocked", "failed", "error"}
        ):
            _fail(
                path,
                "continuation_receipt_not_completed",
                "only complete actual responses establish outputs",
            )
        for value in (receipt, response):
            if value.get("company_id", original.company_id) != original.company_id:
                _fail(path, "continuation_company_mismatch", "receipt belongs to another company")
            if value.get("database_id", original.database_id) != original.database_id:
                _fail(path, "continuation_database_mismatch", "receipt belongs to another database")
        if step.command == "close":
            _fail(
                path,
                "continuation_closed_prefix_requires_review",
                "a committed close requires separate prefix review",
            )
        receipts[step_id] = receipt
    completed = set(receipts)
    for step_id in completed:
        if not set(edges[step_id]).issubset(completed | ancestor_ids):
            _fail(
                step_id,
                "continuation_receipt_dependency_missing",
                "all completed predecessor receipts must be supplied",
            )
    references = available_references | {
        by_id[key].output_ref for key in completed if by_id[key].output_ref
    }
    bindings, proofs, virtual = deepcopy(available_bindings), [], []
    for step in order:
        if step.step_id not in completed:
            continue
        receipt = receipts[step.step_id]
        payload = replace_known_outputs(step.payload, bindings, references, strict=True)
        if payload.get("company_id", original.company_id) != original.company_id:
            _fail(
                step.step_id,
                "continuation_company_mismatch",
                "original node does not match its company",
            )
        if step.command == "save_facts":
            expected = [row.get("subject_id") for row in payload.get("facts", [])]
            results = receipt["response"].get("results")
            actual = (
                [row.get("subject_id") for row in results]
                if isinstance(results, list) and all(isinstance(row, dict) for row in results)
                else None
            )
            if actual != expected:
                _fail(
                    step.step_id,
                    "continuation_subject_mismatch",
                    "saved response does not match the typed source batch",
                )
        elif (
            "subject_id" in payload
            and "subject_id" in receipt["response"]
            and payload["subject_id"] != receipt["response"]["subject_id"]
        ):
            _fail(
                step.step_id,
                "continuation_subject_mismatch",
                "receipt does not match the original source identity",
            )
        selectors = replace_known_outputs(
            step.model_dump(mode="json")["output_selectors"], bindings, references, strict=True
        )
        for field in step.output_fields:
            try:
                value = _output_field(receipt["response"], field, selectors)
            except (KernelError, TypeError, ValueError) as exc:
                _fail(step.step_id + "/" + field, "continuation_output_missing", str(exc))
            bindings[step.output_ref, field] = deepcopy(value)
            proofs.append(
                {
                    "reference": step.output_ref,
                    "field": field,
                    "value": deepcopy(value),
                    "source_step": step.step_id,
                    "original_request_id": receipt["request_id"],
                    "response_digest": digest(receipt["response"]).hex(),
                }
            )
        item = step.model_dump(mode="json")
        item.update(
            payload=payload,
            depends_on=list(edges[step.step_id]),
            output_ref=None,
            output_fields=[],
            output_selectors={},
        )
        virtual.append(ReplayStep.model_validate_json(canonical(item)))
    return {
        "current_steps": tuple(current_steps),
        "order": order,
        "edges": edges,
        "receipts": receipts,
        "completed": completed,
        "bindings": bindings,
        "proofs": proofs,
        "virtual_steps": tuple(virtual),
        "references": references,
        "ancestor_steps": ancestor_steps,
    }


def verify_ancestor_checkpoints(original, checkpoints):
    """Follow existing continuation_from only; unused or missing layers are errors."""
    if not isinstance(checkpoints, (list, tuple)):
        _fail(
            "ancestor_checkpoints",
            "continuation_ancestor_invalid",
            "actual checkpoint objects are required",
        )
    by_run = {}
    for checkpoint in checkpoints:
        if (
            not isinstance(checkpoint, dict)
            or not isinstance(checkpoint.get("run_id"), str)
            or not checkpoint["run_id"]
        ):
            _fail(
                "ancestor_checkpoints",
                "continuation_ancestor_invalid",
                "each ancestor needs its exact run ID",
            )
        if checkpoint["run_id"] in by_run:
            _fail(
                "ancestor_checkpoints",
                "continuation_ancestor_duplicate",
                "ancestor run IDs cannot repeat",
            )
        by_run[checkpoint["run_id"]] = checkpoint
    chain, seen, next_run = [], set(), original.continuation_from
    scope_fields = (
        "company_id",
        "database_id",
        "first_period",
        "last_period",
        "owner_confirmation",
    )
    while next_run is not None:
        if next_run in seen or len(chain) >= 16:
            _fail(
                "ancestor_checkpoints",
                "continuation_ancestor_cycle",
                "ancestor chain must be bounded and acyclic",
            )
        checkpoint = by_run.get(next_run)
        if checkpoint is None:
            _fail(
                "ancestor_checkpoints",
                "continuation_ancestral_context_required",
                "an explicitly declared ancestor checkpoint is missing",
            )
        try:
            request = ReplayPrepare.model_validate_json(canonical(checkpoint["request"]))
        except (ValidationError, ValueError, KeyError, TypeError) as exc:
            _fail(next_run, "continuation_ancestor_request_invalid", str(exc))
        for name in scope_fields:
            if getattr(request, name) != getattr(original, name):
                _fail(
                    next_run,
                    "continuation_ancestor_scope_mismatch",
                    "ancestor " + name + " differs",
                )
        manifest = [source.model_dump(mode="json") for source in request.source_manifest]
        if digest(manifest).hex() != request.source_manifest_digest:
            _fail(
                next_run,
                "continuation_source_manifest_mismatch",
                "ancestor manifest digest differs",
            )
        chain.append((next_run, request, checkpoint.get("completed_receipts")))
        seen.add(next_run)
        next_run = request.continuation_from
    if set(by_run) != seen:
        _fail(
            "ancestor_checkpoints",
            "continuation_ancestor_not_linked",
            "unlinked history cannot be used as an ancestor",
        )
    virtual, bindings, proofs = (), {}, []
    for run_id, request, receipts in reversed(chain):
        verified = verify_checkpoint_receipts(
            request, receipts, ancestor_steps=virtual, ancestor_bindings=bindings
        )
        virtual = verified["ancestor_steps"] + verified["virtual_steps"]
        bindings.update(verified["bindings"])
        proofs.extend(dict(proof, run_id=run_id) for proof in verified["proofs"])
    return {
        "virtual_steps": virtual,
        "bindings": bindings,
        "proofs": proofs,
        "run_ids": [run_id for run_id, _, _ in chain],
    }
