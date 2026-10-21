"""Replace changed Activity roots, then publish the complete small display index."""

from ai_accounting_modular.contracts.public import ContractError, ReadInputs, loads_json

from . import _brief_activity as activity
from . import _brief_activity_index as index
from . import _dependencies, _monthly


def mark(repository, state, scope, impact, head):
    key = state["scope_key"]
    if state["state"] == "failed":
        repository.activity_mark(key, head, full=True)
        repository.activity_advance_failed(key, head)
        return False
    raw = repository.input_dependencies(key, index.COMPONENT)
    selected = (
        _dependencies.inputs({"_inputs": loads_json(raw)}) if raw is not None else ReadInputs()
    )
    closure = any(value.period_from <= scope["period_to"] for value in impact.closure_ranges)
    management = any(
        identity in selected.object_ids or kind in selected.object_kinds
        for identity, kind in impact.objects
    ) or any(
        identity in selected.current_fact_ids or kind in selected.fact_kinds
        for identity, kind, _ in impact.facts
    )
    affected = raw is None or closure or _monthly.affected(scope, index.COMPONENT, impact, selected)
    if affected:
        repository.activity_mark(
            key,
            head,
            {
                identity
                for change in impact.formal_changes
                for identity in (change.business_id, *change.resource_business_ids)
            },
            full=raw is None or closure or management,
        )
    if affected or state["state"] == "preparing":
        repository.mark(key, index.COMPONENT, head)
    return affected


def prepare(reader, scope, read, state):
    repository, key = read.repository("reading"), state["scope_key"]
    pending = index.pending(repository, key, wanted=state["wanted_head"])
    if pending is None or pending["full"]:
        return activity.prepare(reader, scope, read)
    saved = index.base(repository, state)
    if saved is None:
        return activity.prepare(reader, scope, read)
    changed = set(pending["business_ids"])
    dirty = changed | {
        item.business_id
        for item in saved
        if item.collection == index.ROOTS
        and any(source["business_id"] in changed for source in item.value["sources"])
    }
    retained = saved.retain(dirty)
    members, vouchers, roots = [], [], []
    if dirty:
        members, vouchers, roots = activity.capture(
            reader, scope, read, business_ids=tuple(sorted(dirty))
        )
    try:
        return activity.assemble(members, vouchers, roots, retained=retained)
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "业务活动的当前展示索引不完整。") from error
