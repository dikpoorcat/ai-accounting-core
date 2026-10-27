"""The unique accounting execution path, shared by wire calls and offline rebuilds."""

from ai_accounting_modular.contracts.public import (
    ContractError,
    fact_source_ids,
    fact_source_paths,
    validate_contract,
)
from ai_accounting_modular.identity.public import IdentityContext
from ai_accounting_modular.records.public import RecordsChange, RecordsContext
from ai_accounting_modular.storage.public import company_metadata

from . import _handoff, _notes, _reading
from ._changes import classify
from ._context import SubmissionContext
from ._planning import collect_changes, evaluate
from ._submission import ExecutionOutcome


def execute(application, submission, unit, *, evaluation_period_to=None, prepared_sources=None):
    submission.validate()
    completed = False

    def require_complete():
        if not completed:
            raise ContractError("invalid_operation", "应用办理未完整成功，不能提交部分结果。")

    unit.check_before_commit(require_complete)
    mutations, seen = submission.mutations, set()
    for mutation in mutations:
        business_id = mutation.get("business_id") or mutation["fact"]["business_id"]
        if business_id in seen:
            raise ContractError("invalid_input", "同单每个业务身份只能出现一次。")
        seen.add(business_id)
    if not (
        mutations
        or submission.objects
        or submission.sources
        or submission.notes
        or submission.unresolved is not None
        or evaluation_period_to is not None
    ):
        raise ContractError("invalid_input", "业务单不能没有任何内容。")
    company_id = submission.company_id
    if unit.company_id != company_id:
        raise ContractError("invalid_input", "应用事务与请求公司不一致。")
    if evaluation_period_to is not None:
        validate_contract("core.schema.json", "Month", evaluation_period_to)
    metadata = company_metadata(unit)
    repo = unit.repository("application")
    heads = repo.heads(seen)
    changes = [
        classify(
            mutation,
            heads.get(mutation.get("business_id") or mutation["fact"]["business_id"]),
        )
        for mutation in mutations
    ]
    previous_facts = {
        fact["business_id"]: fact
        for fact in application.registry.export_heads(list(heads.values()), unit)
    }
    handoffs = _handoff.prepare(changes, previous_facts, application.registry)
    for change in changes:
        if change.fact:
            application.registry.owner(change.fact["kind"])
    identity_plan = application.identity.plan_change(
        submission.objects, IdentityContext(unit, submission.command_id)
    )
    records_plan = application.records.plan_change(
        RecordsChange(sources=submission.sources),
        RecordsContext(unit, submission.command_id, prepared_sources=prepared_sources),
    )
    application.records.apply_current(records_plan, unit)
    previous_period_to = max(
        metadata.start_period,
        repo.working_period_to() or "0001-01",
        repo.latest_period() or "0001-01",
        application.ledger.latest_period(unit) or "0001-01",
    )
    context = SubmissionContext(
        unit,
        application.registry,
        application.identity,
        application.ledger,
        application.records,
        objects=identity_plan.objects,
        facts={change.business_id: change.fact for change in changes},
        money=application.money,
        evaluation_period_to=max(
            [
                previous_period_to,
                evaluation_period_to or previous_period_to,
                submission.correction_period or previous_period_to,
                *(change.fact["period"] for change in changes if change.fact),
            ]
        ),
        previous_period_to=previous_period_to,
    )
    for index, change in enumerate(changes):
        if change.fact:
            paths = fact_source_paths(mutations[index]["fact"])
            context.require_sources(
                tuple(paths),
                business_id=change.business_id,
                field=f"/mutations/{index}/fact",
                source_paths=paths,
            )
    for index, obj in enumerate(submission.objects):
        context.require_sources(obj.get("evidence_ids", ()), field=f"/objects/{index}/evidence_ids")
    descriptions = collect_changes(
        changes, identity_plan.changed_ids, context, application.registry, handoffs
    )
    responsibilities = _handoff.active_responsibilities(descriptions, handoffs)
    plans = evaluate(descriptions, context, application.ledger, submission.correction_period)
    natural_keys = {
        business_id: item["natural_key"] for business_id, item in responsibilities.items()
    }
    prior_heads = repo.heads(responsibilities)
    proposed = [
        (
            key,
            item["fact"]["kind"] if "fact" in item else prior_heads[key]["kind"],
            natural_keys[key] if "fact" in item else None,
        )
        for key, item in responsibilities.items()
    ]
    repo.validate_natural_keys(proposed)
    changed_keys = {
        key: value
        for key, _, value in proposed
        if key not in prior_heads or prior_heads[key]["natural_key"] != value
    }
    repo.clear_natural_keys(list(changed_keys))
    for change in changes:
        if change.changed:
            repo.save(
                change.head(submission.command_id, natural_key=natural_keys[change.business_id]),
                complete_result_slots=responsibilities[change.business_id]["complete_result_slots"],
                search_revision=unit.next_head,
            )
    direct_changes = {change.business_id for change in changes if change.changed}
    for business_id, key in changed_keys.items():
        if business_id not in direct_changes:
            repo.update_natural_key(business_id, key)
    repo.advance_working_period(context.evaluation_period_to, metadata.start_period)
    application.identity.apply_current(identity_plan, unit)
    for object_id, ids in identity_plan.source_uses.items():
        application.records.replace_source_uses("object", object_id, ids, unit)
    for owner, plan in plans:
        owner.apply_current(plan, unit)
    posting_plans = [posting for _, plan in plans for posting in plan["plans"]]
    if any(posting["fund_movements"] for posting in posting_plans):
        if application.money is None:
            raise ContractError("invalid_operation", "实际资金身份模块尚未装配。")
        application.money.register_occurrences(posting_plans, unit)
    adoption = application.ledger.adopt(
        [plan for _, plan in plans],
        unit,
        correction_period=submission.correction_period,
        explanation=submission.explanation,
    )
    touched = sorted(responsibilities)
    dependencies = [edge for _, plan in plans for edge in plan["dependencies"]]
    projection_changed = False
    if touched:
        relation_changed = repo.replace_relations(
            touched,
            context.declared_relations,
            search_revision=unit.next_head,
        )
        dependency_changed = repo.replace_dependencies(touched, dependencies)
        publication_changed = repo.replace_publications(
            touched, [item for _, plan in plans for item in plan["publications"]]
        )
        responsibility_changed = repo.replace_result_slots(responsibilities)
        projection_changed = (
            relation_changed or dependency_changed or publication_changed or responsibility_changed
        )
    for change in changes:
        if change.changed:
            application.records.replace_source_uses(
                "business",
                change.business_id,
                fact_source_ids(change.fact) if change.fact else (),
                unit,
            )
    before_notes = (
        {
            note["scope"]: note["seq"]
            for note in application.records.current_notes(
                tuple(note["scope"] for note in submission.notes), unit
            )
        }
        if application._reading_targets is not None and submission.notes
        else {}
    )
    context_plan, invalidated_notes = _notes.finish(
        application,
        submission,
        adoption,
        unit,
        withdrawn_business_ids=tuple(
            change.business_id for change in changes if change.changed and change.fact is None
        ),
    )
    keys = set()
    if context.evaluation_period_to > previous_period_to:
        keys.add("company")
    for change in changes:
        if change.changed:
            for head in (
                change.before,
                change.head(submission.command_id, natural_key=natural_keys[change.business_id]),
            ):
                if head and head["period"]:
                    keys.add(f"facts:{head['family']}:{head['period']}")
    if identity_plan.changed_ids:
        keys.add("objects")
    if keys:
        application.records.mark_document_targets(sorted(keys), unit)
    final_heads = repo.heads(seen | set(touched))

    def check_projection():
        application.registry.export_heads(list(final_heads.values()), unit)
        _handoff.verify_released(handoffs, unit)

    unit.check_before_commit(check_projection)
    affected_periods = sorted(
        {
            head["period"]
            for change in changes
            if change.changed
            for head in (
                change.before,
                change.head(submission.command_id, natural_key=natural_keys[change.business_id]),
            )
            if head and head["period"]
        }
        | {ref["posting_period"] for ref in adoption.results}
    )
    changed = bool(
        keys
        or changed_keys
        or adoption.changed
        or projection_changed
        or records_plan.changed_source_ids
        or context_plan.changed_scopes
        or invalidated_notes
    )
    reading_changed = False
    if changed and application._reading_targets is not None:
        impact = _reading.applied(
            adoption=adoption,
            objects=tuple(
                obj
                for obj in identity_plan.objects
                if obj["object_id"] in identity_plan.changed_ids
            ),
            facts=tuple(
                fact
                for change in changes
                if change.changed
                for fact in (previous_facts.get(change.business_id), change.fact)
                if fact is not None
            ),
            notes=set(invalidated_notes)
            | {
                note["scope"]
                for note in context_plan.notes
                if before_notes.get(note["scope"]) != note["seq"]
            },
            issue_periods=context_plan.issue_periods,
            working_period_changed=context.evaluation_period_to != previous_period_to,
        )
        if application._notify_reading(impact, unit):
            reading_changed = True

    completed = True
    return ExecutionOutcome(
        heads=tuple(final_heads[key] for key in sorted(seen)),
        affected_periods=tuple(affected_periods),
        objects=tuple(identity_plan.objects),
        notes=tuple(context_plan.notes),
        issue_scopes=(context_plan.issue_scope,) if context_plan.issue_scope is not None else (),
        unresolved=tuple(context_plan.issues),
        changed=changed,
        accounting_changed=adoption.changed,
        reading_changed=reading_changed,
    )
