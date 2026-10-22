"""Refresh actual dirty roots and their groups through one atomic derived patch."""

from copy import deepcopy

from . import _brief_activity as activity
from . import _brief_activity_dependencies as dependencies
from . import _brief_activity_index as index
from . import _brief_activity_totals as totals
from . import _brief_activity_update_groups as groups


def mark(repository, state, scope, impact, head):
    key = state["scope_key"]
    if state["state"] == "failed":
        repository.activity_mark(key, head, full=True)
        repository.activity_advance_failed(key, head)
        return False
    directory = index.base(repository, state)
    changed = {
        identity
        for change in impact.formal_changes
        if any(period <= scope["period_to"] for period in change.posted_periods)
        for identity in (change.business_id, *change.resource_business_ids)
    }
    closure = any(value.period_from <= scope["period_to"] for value in impact.closure_ranges)
    roots = directory.dependents(index.BUSINESSES, changed) if directory else set()
    objects = (
        directory.dependents(index.OBJECTS, {identity for identity, _ in impact.objects})
        if directory
        else set()
    )
    posted = any(scope["period_to"] in change.posted_periods for change in impact.formal_changes)
    affected = directory is None or closure or bool(roots or objects) or posted
    if affected:
        repository.activity_mark(
            key, head, changed, full=directory is None or closure or bool(objects)
        )
    if affected or state["state"] == "preparing":
        repository.mark(key, index.COMPONENT, head)
    return affected


def prepare(reader, scope, read, state):
    repository, key = read.repository("reading"), state["scope_key"]
    pending = index.pending(repository, key, wanted=state["wanted_head"])
    if pending is None or pending["full"]:
        return activity.prepare(reader, scope, read)
    directory = index.base(repository, state)
    if directory is None:
        return activity.prepare(reader, scope, read)
    changed = set(pending["business_ids"])
    dirty = changed | directory.dependents(index.BUSINESSES, changed)
    old_roots = directory.get(index.ROOTS, dirty)
    old_members = directory.get(
        "activity_members",
        {identity for root in old_roots.values() for identity in root["members"]},
        required=True,
    )
    old_vouchers = directory.get(
        "brief_vouchers",
        {identity for root in old_roots.values() for identity in root["vouchers"]},
        required=True,
    )
    if any(member["row"]["subject_id"] not in old_roots for member in old_members.values()):
        index.corrupt()
    members, vouchers, roots = (
        activity.capture(reader, scope, read, business_ids=tuple(sorted(dirty)))
        if dirty
        else ([], [], [])
    )
    if any(root.item_id not in dirty for root in roots):
        index.corrupt()
    selected_roots = {root.item_id: root.value for root in roots}
    for item in (*vouchers, *(totals.member_item(member) for member in members)):
        dependencies.owned(item, selected_roots)
    entries = [*vouchers, *roots, *(totals.member_item(member) for member in members)]
    deletes = {(index.ROOTS, identity) for identity in old_roots}
    deletes.update(("activity_members", identity) for identity in old_members)
    deletes.update(("brief_vouchers", identity) for identity in old_vouchers)
    summary = deepcopy(directory.payload)
    summary["_inputs"] = dependencies.changes(
        directory,
        [
            index.root_item(
                value["business_id"],
                value["sources"],
                value["object_ids"],
                members=value["members"],
                vouchers=value["vouchers"],
            )
            for value in old_roots.values()
        ],
        roots,
        entries,
        deletes,
    ).wire()
    groups.replace(
        directory, list(old_members.values()), members, summary["_category_folds"], entries, deletes
    )
    totals.review_adjust(summary["_review_folds"], old_members.values(), -1)
    totals.review_adjust(summary["_review_folds"], members, 1)
    return index.patch(summary, entries, deletes, directory, state)
