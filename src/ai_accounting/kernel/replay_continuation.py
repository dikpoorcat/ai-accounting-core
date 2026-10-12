"""Pure, receipt-based continuation of one current-root replay before any close.

The caller preserves the original request and complete API receipts. This helper
does not allocate business identities, open databases, or import historical data.
"""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

from pydantic import ValidationError

from .contracts import KernelError
from .replay_checkpoint_proofs import verify_ancestor_checkpoints, verify_checkpoint_receipts
from .replay_contracts import ReplayPrepare, ReplaySource
from .replay_document import ReplayDocumentCompileError
from .replay_runner import (
    _request_key,
    _safe_step_payload,
    compile_replay_steps,
    validate_replay_sequence,
)
from .types import canonical, digest

READ_ONLY_PREPARATIONS = frozenset(
    {
        "preview",
        "prepare_fact_registration",
        "preview_identity_correction",
        "payroll_reuse_basis",
        "prepare_payroll",
        "prepare_obligations",
        "obligation_basis",
        "preview_material_allocation",
        "preview_tax_import",
        "prepare_asset_activation_batch",
        "prepare_asset_consumption_month",
        "replay_classification_prepare",
        "preview_close",
    }
)

PREPARATION_FOR_CONFIRMATION = {
    "confirm": {"preview"},
    "confirm_identity_correction": {"preview_identity_correction"},
    "confirm_obligations": {"prepare_obligations"},
    "confirm_material_allocation": {"preview_material_allocation"},
    "confirm_tax_import": {"preview_tax_import"},
    "confirm_asset_activation_batch": {"prepare_asset_activation_batch"},
    "confirm_asset_consumption_month": {"prepare_asset_consumption_month"},
    "confirm_payroll_preparation": {"prepare_payroll", "payroll_reuse_basis"},
    "preview": {"prepare_payroll"},
}


def _references(value):
    if isinstance(value, dict):
        if set(value) == {"$replay_ref", "field"}:
            yield value["$replay_ref"]
        else:
            for child in value.values():
                yield from _references(child)
    elif isinstance(value, list):
        for child in value:
            yield from _references(child)


def _fail(path, code, message):
    raise ReplayDocumentCompileError([{"path": path, "code": code, "message": message}])


def _replace_completed(value, bindings, completed_references, *, strict=False):
    if isinstance(value, dict):
        if set(value) == {"$replay_ref", "field"}:
            key = value["$replay_ref"], value["field"]
            if key in bindings:
                return deepcopy(bindings[key])
            if strict or key[0] in completed_references:
                _fail(
                    canonical(value),
                    "continuation_output_missing",
                    "actual declared output is missing",
                )
            return deepcopy(value)
        return {
            key: _replace_completed(child, bindings, completed_references, strict=strict)
            for key, child in value.items()
        }
    if isinstance(value, list):
        return [
            _replace_completed(child, bindings, completed_references, strict=strict)
            for child in value
        ]
    return deepcopy(value)


def prepare_replay_continuation(
    original_request,
    *,
    completed_receipts,
    request_id,
    source_manifest_extra=(),
    parent_run_id=None,
    ancestor_checkpoints=(),
):
    """Remove committed nodes and substitute only their exact declared outputs.

    Receipt envelopes are exact ``replay_status(..., step_id=...)`` results for
    this run. Their stable request IDs, commands, dependency closure and output
    selectors are checked against the preserved original request. This helper
    currently requires that no close has committed and all monthly close pairs
    remain. Completed read-only preparations still needed by pending nodes are
    prepared again under new step IDs; their original receipts stay immutable.
    With parent_run_id, completed parent dependencies remain explicit for the
    server's independently verified, validation-only lineage expansion.
    """
    try:
        original = ReplayPrepare.model_validate_json(canonical(original_request))
    except (ValidationError, ValueError, TypeError) as exc:
        _fail("original_request", "continuation_original_invalid", str(exc))
    if not isinstance(request_id, str) or not request_id or len(request_id) > 200:
        _fail(
            "request_id", "continuation_request_id_invalid", "a new strict request ID is required"
        )
    if request_id == original.request_id:
        _fail(
            "request_id",
            "continuation_request_id_reused",
            "continuation needs a distinct prepare request ID",
        )
    if parent_run_id is not None and (
        not isinstance(parent_run_id, str) or not parent_run_id or len(parent_run_id) > 200
    ):
        _fail(
            "parent_run_id", "continuation_parent_run_invalid", "an exact parent run ID is required"
        )
    old = original.model_dump(mode="json")
    if digest(old["source_manifest"]).hex() != original.source_manifest_digest:
        _fail(
            "source_manifest_digest",
            "continuation_source_manifest_mismatch",
            "original manifest digest does not match",
        )
    ancestors = verify_ancestor_checkpoints(original, ancestor_checkpoints)
    if original.continuation_from and parent_run_id is None:
        _fail(
            "parent_run_id", "continuation_parent_run_required",
            "an explicitly linked current run must remain the new parent",
        )
    if parent_run_id in ancestors["run_ids"]:
        _fail("parent_run_id", "continuation_ancestor_cycle", "new parent repeats an ancestor")
    verified = verify_checkpoint_receipts(
        original, completed_receipts,
        ancestor_steps=ancestors["virtual_steps"], ancestor_bindings=ancestors["bindings"],
    )
    ancestors["virtual_steps"] = verified["ancestor_steps"]
    original = original.model_copy(update={"steps": verified["current_steps"]})
    order, edges = verified["order"], verified["edges"]
    steps_by_id = {step.step_id: step for step in order}
    completed = verified["completed"]
    completed_references = verified["references"]
    bindings, proofs = verified["bindings"], verified["proofs"]
    for step in original.steps:
        if step.payload.get("company_id", original.company_id) != original.company_id:
            _fail(step.step_id, "continuation_company_mismatch",
                  "original node does not match its company")
    reprepare = set()
    completed_read_outputs = {
        steps_by_id[key].output_ref: key
        for key in completed
        if steps_by_id[key].command in READ_ONLY_PREPARATIONS and steps_by_id[key].output_ref
    }
    pending = [step.step_id for step in original.steps if step.step_id not in completed]
    while pending:
        current = pending.pop()
        current_step = steps_by_id[current]
        consumed_refs = set(_references(current_step.payload)) | set(
            _references(current_step.model_dump(mode="json")["output_selectors"])
        )
        needed = {
            completed_read_outputs[reference]
            for reference in consumed_refs
            if reference in completed_read_outputs
        }
        needed.update(
            parent
            for parent in edges[current]
            if steps_by_id[parent].command
            in PREPARATION_FOR_CONFIRMATION.get(current_step.command, set())
        )
        for parent in needed:
            if (
                parent in completed
                and steps_by_id[parent].command in READ_ONLY_PREPARATIONS
                and parent not in reprepare
            ):
                reprepare.add(parent)
                pending.append(parent)
    new_read_ids = {
        key: "s_cont_" + digest([original.request_id, request_id, key]).hex() for key in reprepare
    }
    refreshed_references = {
        steps_by_id[key].output_ref for key in reprepare if steps_by_id[key].output_ref
    }
    reusable_bindings = {
        key: value for key, value in bindings.items() if key[0] not in refreshed_references
    }
    literal_references = completed_references - refreshed_references
    remaining = []
    for step in original.steps:
        if step.step_id in completed and step.step_id not in reprepare:
            continue
        item = step.model_dump(mode="json")
        item["step_id"] = new_read_ids.get(step.step_id, step.step_id)
        item["depends_on"] = [
            new_read_ids.get(key, key)
            for key in edges[step.step_id]
            if parent_run_id is not None or key not in completed or key in reprepare
        ]
        item["payload"] = _replace_completed(item["payload"], reusable_bindings, literal_references)
        item["output_selectors"] = _replace_completed(
            item["output_selectors"], reusable_bindings, literal_references
        )
        body = item["payload"]
        if body.get("company_id", original.company_id) != original.company_id:
            _fail(
                step.step_id,
                "continuation_company_mismatch",
                "original node does not match its company",
            )
        if "request_id" in body:
            if body["request_id"] != _request_key(original.request_id, step.step_id):
                _fail(
                    step.step_id,
                    "continuation_request_mismatch",
                    "stored node request ID differs from the original plan",
                )
            body["request_id"] = _request_key(request_id, item["step_id"])
        if step.command == "close" and body.get("approval_id") == "replay-scope-authority":
            body.pop("approval_id")
        remaining.append(item)
    request = deepcopy(old)
    request["request_id"], request["steps"] = request_id, remaining
    if parent_run_id is not None:
        request["continuation_from"] = parent_run_id
    extra = []
    existing_sources = {item["source_id"] for item in request["source_manifest"]}
    for index, source in enumerate(source_manifest_extra):
        try:
            item = ReplaySource.model_validate_json(canonical(source)).model_dump(mode="json")
        except (ValidationError, ValueError, TypeError) as exc:
            _fail(f"source_manifest_extra/{index}", "continuation_source_invalid", str(exc))
        if item["source_id"] in existing_sources:
            _fail(
                f"source_manifest_extra/{index}",
                "continuation_duplicate_source",
                "source IDs cannot repeat",
            )
        existing_sources.add(item["source_id"])
        extra.append(item)
    request["source_manifest"].extend(extra)
    request["source_manifest_digest"] = digest(request["source_manifest"]).hex()
    request["preserved_evidence"] = sorted(
        set(request["preserved_evidence"])
        | {item["evidence_digest"] for item in extra if item["evidence_digest"]}
    )
    try:
        validated = ReplayPrepare.model_validate_json(canonical(request))
        validation_request = deepcopy(request)
        if parent_run_id is not None:
            virtual = [step.model_dump(mode="json") for step in ancestors["virtual_steps"]]
            replaced_close_reviews = {
                key for key in reprepare if steps_by_id[key].command == "preview_close"
            }
            for step in order:
                if step.step_id not in completed or step.step_id in replaced_close_reviews:
                    continue
                item = step.model_dump(mode="json")
                item["payload"] = _replace_completed(
                    item["payload"], bindings, completed_references, strict=True
                )
                item["depends_on"] = [
                    key for key in edges[step.step_id] if key not in replaced_close_reviews
                ]
                item["output_ref"], item["output_fields"], item["output_selectors"] = None, [], {}
                virtual.append(item)
            validation_request["steps"] = [*virtual, *remaining]
        expanded = ReplayPrepare.model_validate_json(canonical(validation_request))
        expanded_order, dependencies = compile_replay_steps(expanded.steps)
        validate_replay_sequence(
            expanded_order, dependencies, validated.first_period, validated.last_period
        )
        from .command_schema import command_models
        from .service import default_registry

        registry = default_registry()
        models = command_models(registry)
        service = SimpleNamespace(registry=registry, command_models=models)
        schemas = {}
        remaining_ids = {item["step_id"] for item in remaining}
        for step in expanded_order:
            if step.step_id not in remaining_ids:
                continue
            if step.command not in schemas:
                schemas[step.command] = models[step.command].json_schema()
            _safe_step_payload(
                service,
                validated.company_id,
                validated.request_id,
                step,
                command_schema=schemas[step.command],
            )
    except (ValidationError, ValueError, KernelError) as exc:
        _fail("remaining_plan", "continuation_requires_fresh_preparation", str(exc))
    return {
        "request": request,
        "completed_step_ids": [step.step_id for step in order if step.step_id in completed],
        "binding_proof": proofs,
        "ancestor_binding_proof": ancestors["proofs"],
        "ancestor_run_ids": ancestors["run_ids"],
        "reprepared_steps": [
            {
                "original_step_id": step.step_id,
                "new_step_id": new_read_ids[step.step_id],
                "command": step.command,
            }
            for step in order
            if step.step_id in reprepare
        ],
        "original_request_digest": digest(old).hex(),
        "issues": [],
    }
