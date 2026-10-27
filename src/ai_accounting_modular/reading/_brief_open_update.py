"""Patch changed claims and the cooperation provider into the existing Open indexes."""

from collections import Counter, defaultdict
from copy import deepcopy
from dataclasses import replace

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    content_digest,
)

from . import _brief_open as brief
from . import _brief_open_cooperation as cooperation
from . import _brief_open_groups as groups
from . import _brief_open_index as index
from . import _brief_open_inputs as inputs
from . import _brief_open_rollover as rollover
from . import _brief_open_sources as sources
from . import _brief_open_totals as totals
from . import _brief_open_tree as blocks
from . import _dependencies, _monthly
from ._brief_open_index import base
from ._collections import CollectionFilter
from ._context import last_period


def mark(repository, state, scope, impact, head):
    key = state["scope_key"]
    try:
        directory = base(repository, state)
        payload = directory.payload if directory is not None else None
        if state["state"] == "failed":
            repository.open_mark(key, head, full=True)
            repository.open_advance_failed(key, head)
            return False
        provider = (
            inputs.matching_provider(directory, scope["period_to"], impact)
            if directory is not None
            else ReadInputs()
        )
        selected = inputs.ORDINARY.merge(inputs.static(provider))
        exact = inputs.affected(directory, scope["period_to"], impact) if directory else True
        affected = exact or _monthly.affected(scope, index.COMPONENT, impact, selected)
        if affected:
            inputs.mark(
                repository,
                key,
                impact,
                head,
                selected,
                provider,
                directory,
                full=payload is None,
            )
        if affected or state["state"] == "preparing":
            repository.mark(key, index.COMPONENT, head)
        return affected
    except (ContractError, KeyError, TypeError, ValueError):
        repository.open_mark(key, head, full=True)
        current = repository.state(key, index.COMPONENT)
        if current is not None:
            repository.fail(
                key,
                index.COMPONENT,
                current["wanted_head"],
                "storage_corrupt",
                expected_revision=current["revision"],
                expected_state=current["state"],
            )
        repository.open_advance_failed(key, head)
        return False


def _references(directory, collection, before, after, entries, deletes):
    changes = Counter(after)
    changes.subtract(before)
    changes = {identity: amount for identity, amount in changes.items() if amount}
    current = directory.get(collection, changes)
    added, removed = set(), set()
    for identity, delta in changes.items():
        old_count = current.get(identity, {}).get("count", 0)
        count = old_count + delta
        if count < 0 or (delta < 0 and identity not in current):
            index.corrupt()
        deletes.add((collection, identity))
        if count:
            entries.append(index.item(collection, identity, {"count": count}))
        if not old_count and count:
            added.add(identity)
        elif old_count and not count:
            removed.add(identity)
    return tuple(sorted(added)), tuple(sorted(removed))


def _descriptions(directory, group, previous, proposed, fold, entries, deletes):
    changes = Counter(totals.text(value) for value in proposed)
    changes.subtract(totals.text(value) for value in previous)
    changes = {text: count for text, count in changes.items() if count}
    if not changes:
        return
    identities = {text: blocks.key(group, content_digest(text)) for text in changes}
    current = directory.get(index.DESCRIPTIONS, identities.values())
    survivors = set()
    for text, delta in changes.items():
        identity = identities[text]
        saved = current.get(identity)
        before = 0 if saved is None else saved.get("count")
        if type(before) is not int or before < 0 or (saved is not None and before == 0):
            index.corrupt()
        if saved is not None and (set(saved) != {"text", "count"} or saved["text"] != text):
            index.corrupt()
        after = before + delta
        if after < 0:
            index.corrupt()
        deletes.add((index.DESCRIPTIONS, identity))
        fold["description_count"] += (after > 0) - (before > 0)
        if after:
            entries.append(index.item(index.DESCRIPTIONS, identity, {"text": text, "count": after}))
            survivors.add(text)
    distinct = fold["description_count"]
    if type(distinct) is not int or not 0 <= distinct <= fold["count"]:
        index.corrupt()
    if distinct == 1:
        fold["description"] = (
            next(iter(survivors))
            if survivors
            else directory.surviving_description(group, {content_digest(text) for text in changes})
        )
    else:
        fold["description"] = None


def _groups(directory, old, new_entries, category_folds, entries, deletes):
    previous, proposed = defaultdict(list), defaultdict(list)
    for value in old.values():
        previous[value["group_key"]].append(value)
    for entry in new_entries:
        proposed[entry.group_key].append(entry)
    identities = set(previous) | set(proposed)
    folds = directory.get(index.FOLDS, identities)
    old_groups = directory.get("open_items", folds, required=True)
    old_ids = set(old)
    for identity in sorted(identities):
        fold = folds.get(identity, totals.empty())
        old_count = fold["count"]
        old_first = fold.get("first")
        if identity in old_groups:
            totals.category_adjust(category_folds, old_groups[identity], -1)
        for value in previous[identity]:
            totals.adjust(fold, value, -1)
        for entry in proposed[identity]:
            totals.adjust(fold, entry.value, 1)
        _descriptions(
            directory,
            identity,
            previous[identity],
            [entry.value for entry in proposed[identity]],
            fold,
            entries,
            deletes,
        )
        deletes.update(((index.FOLDS, identity), ("open_items", identity)))
        if not fold["count"]:
            continue
        candidates = [(entry.sort_key, entry.item_id, entry.value) for entry in proposed[identity]]
        previous_first = (old_first["sort"], old_first["id"]) if old_first is not None else None
        if old_count and previous_first is None:
            index.corrupt()
        new_precedes_old = (
            bool(candidates)
            and previous_first is not None
            and min(value[:2] for value in candidates) <= previous_first
        )
        if old_count > len(previous[identity]) and not new_precedes_old:
            if old_first["id"] not in old_ids:
                existing = old_groups[identity]
                candidates.append(
                    (
                        old_first["sort"],
                        old_first["id"],
                        {
                            "group_key": identity,
                            "category_key": existing["category_key"],
                            "party": existing["party"],
                            "_matter_title": old_first["title"],
                        },
                    )
                )
            else:
                # At most changed members + one surviving representative, using the
                # existing group index even when the group has 100,000 members.
                found = directory.item_page(
                    "open_members",
                    CollectionFilter(group_key=identity),
                    None,
                    len(previous[identity]) + 1,
                )
                for row in found:
                    if row["item_id"] not in old_ids:
                        value = directory.check(row)
                        candidates.append((row["sort_key"], row["item_id"], value))
                        break
        if not candidates:
            index.corrupt()
        order, member_id, first = min(candidates, key=lambda value: value[:2])
        fold["first"] = {"id": member_id, "sort": order, "title": first["_matter_title"]}
        value = totals.group(first, fold)
        totals.category_adjust(category_folds, value, 1)
        entries.extend(
            (
                groups.group_item(value, sort_description=first["_matter_title"]),
                index.item(index.FOLDS, identity, fold),
            )
        )


def prepare(reader, scope, read, state):
    repository, key = read.repository("reading"), state["scope_key"]
    full, dirty = inputs.pending(repository, key)
    initial = rollover.initial(repository, state)
    if full and not initial:
        return brief.prepare(reader, scope, read)
    directory = base(repository, state)
    payload = directory.payload if directory is not None else None
    current_cutoff = last_period(reader, read) or scope["period_to"]
    if payload is None:
        previous = (
            rollover.select(repository, scope, current_cutoff, read.start_period)
            if initial
            else None
        )
        if previous is None:
            return brief.prepare(reader, scope, read)
        state, payload, directory = previous
        candidates = reader.ledger.obligation_cutoff_candidates(
            payload["open_items"]["cutoff_period"], scope["period_to"], read
        )
        dirty.update(candidates.business_ids)
        complete = sources._complete(reader.ledger, scope, read)
    else:
        if current_cutoff < payload["open_items"]["current_cutoff_period"]:
            return brief.prepare(reader, scope, read)
        complete = payload["open_items"]["complete"]
    return prepare_from_base(
        reader,
        scope,
        read,
        state,
        payload,
        directory,
        dirty,
        current_cutoff=current_cutoff,
        complete=complete,
    )


def prepare_from_base(
    reader, scope, read, state, payload, directory, dirty, *, current_cutoff, complete
):
    extra, cooperation_inputs = cooperation.capture(reader, scope, read, current_cutoff)
    previous_provider = directory.provider_inputs()
    dirty.update(inputs.business_ids(previous_provider))
    dirty.update(inputs.business_ids(cooperation_inputs))
    dirty.add("@fixed")
    old_records = directory.get(index.BUSINESSES, dirty)
    old_members = directory.get(
        "open_members",
        {identity for value in old_records.values() for identity in value["members"]},
        required=True,
    )
    if any(
        value["subject_id"] != business
        for business, record in old_records.items()
        for identity in record["members"]
        for value in (old_members[identity],)
    ):
        index.corrupt()
    captured = sources.capture(
        reader,
        scope,
        read,
        business_ids=dirty - {"@fixed"},
        complete=complete,
    )
    members = brief.project(reader, captured, scope["company_id"], read)
    member_entries = brief.member_items(members, extra)
    members.extend(extra)
    bindings = brief.bind_displays(reader, members, captured, read)
    records = inputs.records(captured, members, bindings)
    records = inputs.with_provider(cooperation_inputs, records)
    entries = list(member_entries)
    entries.extend(
        index.item(index.BUSINESSES, identity, value) for identity, value in records.items()
    )
    deletes = {("open_members", identity) for identity in old_members}
    deletes.update((index.BUSINESSES, identity) for identity in old_records)
    category_folds = deepcopy(payload["_category_folds"])
    _groups(directory, old_members, member_entries, category_folds, entries, deletes)
    added_objects, removed_objects = _references(
        directory,
        index.OBJECTS,
        inputs.object_counts(old_records),
        inputs.object_counts(records),
        entries,
        deletes,
    )
    added_notes, removed_notes = _references(
        directory,
        index.NOTES,
        inputs.note_counts(old_records),
        inputs.note_counts(records),
        entries,
        deletes,
    )
    _references(
        directory,
        index.VOUCHERS,
        inputs.voucher_counts(old_records),
        inputs.voucher_counts(records),
        entries,
        deletes,
    )
    summary = {
        name: value
        for name, value in payload.items()
        if name not in {"_item_epoch", "_semantic_digest", "_collections"}
    }
    summary["_category_folds"] = category_folds
    deletes.update(
        (index.PROVIDER, item.item_id) for item in inputs.provider_items(previous_provider)
    )
    entries.extend(inputs.provider_items(cooperation_inputs))
    added, removed = inputs.business_changes(old_records, records)
    provider_added, provider_removed = _dependencies.difference(
        inputs.ORDINARY.merge(inputs.static(previous_provider)),
        inputs.ORDINARY.merge(inputs.static(cooperation_inputs)),
    )
    added = added.merge(
        provider_added, ReadInputs(object_ids=added_objects, note_scopes=added_notes)
    )
    removed = removed.merge(
        provider_removed, ReadInputs(object_ids=removed_objects, note_scopes=removed_notes)
    )
    summary["open_items"] = totals.summary(
        category_folds,
        groups.CATEGORIES,
        scope["period_to"],
        captured.current_cutoff,
        captured.complete,
    )
    prepared = index.patch(
        summary, entries, deletes, directory, state, scope_key=_monthly.key(scope)
    )
    return replace(prepared, model=replace(prepared.model, input_adds=added, input_removes=removed))
