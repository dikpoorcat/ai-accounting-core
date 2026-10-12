"""Pure publication edges for each typed account's continuous reconciliation."""

from __future__ import annotations

from copy import deepcopy

from pydantic import ValidationError

from .contracts import KernelError
from .replay_contracts import ReplayPrepare
from .replay_document import ReplayDocumentCompileError
from .replay_runner import compile_replay_steps, validate_replay_sequence
from .types import YearMonth, canonical


def _fail(path, code, message):
    raise ReplayDocumentCompileError([{"path": path, "code": code, "message": message}])


def add_bank_reconciliation_dependencies(original_request):
    """Add ordering only; retain every payload, ID, output and source verbatim.

    This accepts the complete preserved ReplayPrepare request, before completed
    nodes are removed for a continuation. Accounts are grouped by their exact
    typed bank_account_id wire value, including current-run output references.
    No account, month or opening balance is invented to fill a gap.
    """
    try:
        original = ReplayPrepare.model_validate_json(canonical(original_request))
        _, dependencies = compile_replay_steps(original.steps)
    except (ValidationError, ValueError, KernelError) as exc:
        _fail("request", "bank_dependency_plan_invalid", str(exc))
    request = deepcopy(original_request)
    raw_steps = {step["step_id"]: step for step in request["steps"]}
    typed_steps = {step.step_id: step for step in original.steps}
    records = {}
    groups = {}
    for step in original.steps:
        if step.command != "save_facts":
            continue
        for fact in step.payload.get("facts", []):
            if fact.get("kind") not in {"bank_opening", "opening_bank", "bank_reconciliation"}:
                continue
            data = fact.get("data")
            if not isinstance(data, dict) or "bank_account_id" not in data or "period" not in data:
                _fail(
                    step.step_id,
                    "bank_dependency_source_unresolved",
                    "bank source needs exact typed account and month",
                )
            subject = canonical(fact["subject_id"])
            if subject in records:
                _fail(
                    step.step_id,
                    "bank_dependency_subject_duplicate",
                    "bank source subjects must be unique",
                )
            records[subject] = fact
            account = canonical(data["bank_account_id"])
            groups.setdefault(account, {}).setdefault(fact["kind"], []).append(fact)
    publishers = {}
    for step in original.steps:
        if step.command == "confirm":
            for subject in step.payload.get("subjects", []):
                key = canonical(subject)
                if key in records:
                    publishers.setdefault(key, []).append(step.step_id)

    def publication(fact):
        found = publishers.get(canonical(fact["subject_id"]), [])
        if len(found) != 1:
            _fail(
                canonical(fact["subject_id"]),
                "bank_publication_not_unique",
                "each bank source needs one formal publication",
            )
        return found[0]

    def preview(fact):
        confirm_id = publication(fact)
        confirm = typed_steps[confirm_id]
        ancestors, queue = set(), list(dependencies[confirm_id])
        while queue:
            parent = queue.pop()
            if parent not in ancestors:
                ancestors.add(parent)
                queue.extend(dependencies[parent])
        candidates = [
            parent
            for parent in ancestors
            if typed_steps[parent].command == "preview"
            and typed_steps[parent].payload.get("subjects") == confirm.payload.get("subjects")
        ]
        if len(candidates) != 1:
            _fail(
                confirm_id,
                "bank_preview_not_unique",
                "bank publication needs its unique matching preview",
            )
        return candidates[0]

    added = []
    for account, kinds in groups.items():
        reconciliations = sorted(
            kinds.get("bank_reconciliation", []), key=lambda fact: fact["data"]["period"]
        )
        if not reconciliations:
            continue
        months = [fact["data"]["period"] for fact in reconciliations]
        if len(months) != len(set(months)):
            _fail(
                account,
                "bank_reconciliation_period_duplicate",
                "one bank account cannot have multiple reconciliations in a month",
            )
        openings = kinds.get("bank_opening", []) or kinds.get("opening_bank", [])
        if len(openings) != 1:
            _fail(
                account,
                "bank_opening_plan_not_unique",
                "continuous reconciliation needs one explicit book opening",
            )
        opening = openings[0]
        if opening["data"]["period"] != months[0]:
            _fail(
                account,
                "bank_reconciliation_start_gap",
                "reconciliation must start in the explicit book opening month",
            )
        prior, prior_period = opening, None
        for fact in reconciliations:
            month = fact["data"]["period"]
            if prior_period and YearMonth(month).ordinal != YearMonth(prior_period).ordinal + 1:
                _fail(
                    account,
                    "bank_reconciliation_period_gap",
                    "intermediate reconciliation facts are missing",
                )
            step_id, predecessor = preview(fact), publication(prior)
            body = raw_steps[step_id]
            current = body.setdefault("depends_on", [])
            if predecessor not in current:
                current.append(predecessor)
                added.append(
                    {
                        "step_id": step_id,
                        "depends_on": predecessor,
                        "period": month,
                        "previous_period": prior_period,
                        "bank_account_id": deepcopy(fact["data"]["bank_account_id"]),
                        "reason": "previous_reconciliation_adopted"
                        if prior_period
                        else "book_opening_adopted",
                    }
                )
            prior, prior_period = fact, month
    try:
        validated = ReplayPrepare.model_validate_json(canonical(request))
        order, edges = compile_replay_steps(validated.steps)
        validate_replay_sequence(order, edges, validated.first_period, validated.last_period)
    except (ValidationError, ValueError, KernelError) as exc:
        _fail("request", "bank_dependency_plan_invalid", str(exc))
    return {"request": request, "added_edges": added, "issues": []}
