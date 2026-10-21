"""Apply ordinary obligation changes to BriefOpen's existing member and group indexes."""

from collections import Counter, defaultdict
from copy import deepcopy

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    content_digest,
    loads_json,
)

from . import _brief_open as brief
from . import _brief_open_groups as groups
from . import _brief_open_index as index
from . import _brief_open_inputs as inputs
from . import _brief_open_rows as rows
from . import _brief_open_sources as sources
from . import _brief_open_totals as totals
from . import _dependencies, _monthly
from ._collections import CollectionFilter


def base(repository, state):
    row = repository.payload(state["scope_key"], index.COMPONENT)
    if row is None:
        return None
    value = loads_json(row["payload_json"])
    if row["head"] != state["ready_head"] or content_digest(value) != state["content_digest"]:
        index.corrupt()
    index.validate(value)
    _dependencies.inputs(value)
    return value


def mark(repository, state, scope, impact, head):
    key = state["scope_key"]
    try:
        payload = base(repository, state)
        if state["state"] == "failed":
            repository.open_mark(key, head, full=True)
            repository.open_advance_failed(key, head)
            return False
        selected = _dependencies.inputs(payload) if payload is not None else ReadInputs()
        directory = index.Directory(repository, key, payload) if payload is not None else None
        exact = inputs.affected(directory, scope["period_to"], impact) if directory else True
        affected = exact or _monthly.affected(scope, index.COMPONENT, impact, selected)
        if affected:
            inputs.mark(repository, key, impact, head, selected, directory, full=payload is None)
        if affected or state["state"] == "preparing":
            repository.mark(key, index.COMPONENT, head)
        return affected
    except (ContractError, KeyError, TypeError, ValueError):
        repository.open_mark(key, head, full=True)
        repository.fail(
            key,
            index.COMPONENT,
            state["wanted_head"],
            "storage_corrupt",
            expected_state=state["state"],
        )
        repository.open_advance_failed(key, head)
        return False


def _references(directory, collection, before, after, entries, deletes):
    changes = Counter(after)
    changes.subtract(before)
    changes = {identity: amount for identity, amount in changes.items() if amount}
    current = directory.get(collection, changes)
    for identity, delta in changes.items():
        count = current.get(identity, {}).get("count", 0) + delta
        if count < 0 or (delta < 0 and identity not in current):
            index.corrupt()
        deletes.add((collection, identity))
        if count:
            entries.append(index.item(collection, identity, {"count": count}))


def _groups(directory, old, new, category_folds, entries, deletes):
    previous, proposed = defaultdict(list), defaultdict(list)
    for value in old.values():
        previous[value["group_key"]].append(value)
    for value in new:
        proposed[value["group_key"]].append(value)
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
        for value in proposed[identity]:
            totals.adjust(fold, value, 1)
        deletes.update(((index.FOLDS, identity), ("open_items", identity)))
        if not fold["count"]:
            continue
        candidates = [
            (brief.member_item(value).sort_key, value["id"], value) for value in proposed[identity]
        ]
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
                            "_matter_title": existing["description"],
                        },
                    )
                )
            else:
                # At most changed members + one surviving representative, using the
                # existing group index even when the group has 100,000 members.
                found = directory.repository.item_page(
                    directory.scope_key,
                    index.COMPONENT,
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
        fold["first"] = {"id": member_id, "sort": order}
        value = totals.group(first, fold)
        totals.category_adjust(category_folds, value, 1)
        entries.extend((groups.group_item(value), index.item(index.FOLDS, identity, fold)))


def prepare(reader, scope, read, state):
    repository, key = read.repository("reading"), state["scope_key"]
    full, dirty = inputs.pending(repository, key)
    if full:
        return brief.prepare(reader, scope, read)
    payload = base(repository, state)
    if payload is None:
        return brief.prepare(reader, scope, read)
    directory = index.Directory(repository, key, payload)
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
    if any("position" in value["_detail_scope"] for value in old_members.values()):
        return brief.prepare(reader, scope, read)
    captured = sources.capture(
        reader, scope, read, business_ids=dirty, complete=payload["open_items"]["complete"]
    )
    if captured.current_cutoff != payload["open_items"]["current_cutoff_period"] or any(
        content.target["kind"] == "opening.package"
        or content.target["kind"].startswith("investment.cooperation_")
        for content in captured.contents.values()
    ):
        return brief.prepare(reader, scope, read)
    members = rows.project(captured, scope["company_id"], reader.identity, read)
    descriptors = brief.describe(reader, members, captured, read)
    records, _ = inputs.records(captured, members, descriptors)
    entries = [brief.member_item(value) for value in members]
    entries.extend(
        index.item(index.BUSINESSES, identity, value) for identity, value in records.items()
    )
    deletes = {("open_members", identity) for identity in old_members}
    deletes.update((index.BUSINESSES, identity) for identity in old_records)
    category_folds = deepcopy(payload["_category_folds"])
    _groups(directory, old_members, members, category_folds, entries, deletes)
    _references(
        directory,
        index.OBJECTS,
        inputs.object_counts(old_records),
        inputs.object_counts(records),
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
    summary["open_items"] = totals.summary(
        category_folds,
        groups.CATEGORIES,
        scope["period_to"],
        captured.current_cutoff,
        captured.complete,
    )
    return index.patch(summary, entries, deletes, directory, state)
