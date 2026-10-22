"""Replace only touched Activity groups, with two independent representative orders."""

from collections import defaultdict

from . import _brief_activity_index as index
from . import _brief_activity_totals as totals
from ._collections import CollectionFilter, IndexItem


def _survivors(directory, collection, identity, removed):
    rows = directory.repository.item_page(
        directory.scope_key,
        index.COMPONENT,
        collection,
        CollectionFilter(group_key=identity),
        None,
        len(removed) + 1,
    )
    return [item for item in directory.checked(rows) if item.item_id not in removed]


def replace(directory, old_members, new_members, categories, entries, deletes):
    previous, proposed = defaultdict(list), defaultdict(list)
    for member in old_members:
        previous[member["row"]["group_key"]].append(member)
    for member in new_members:
        proposed[member["row"]["group_key"]].append(member)
    identities = previous.keys() | proposed.keys()
    folds = directory.get(totals.FOLDS, identities)
    groups = {item.item_id: item for item in directory.items("activity", identities)}
    if folds.keys() != groups.keys():
        index.corrupt()
    old_candidates, new_candidates = totals.candidates(old_members), totals.candidates(new_members)
    actual = {
        item.item_id: item
        for item in directory.items(totals.CANDIDATES, [item.item_id for item in old_candidates])
    }
    if actual != {item.item_id: item for item in old_candidates}:
        index.corrupt()
    entries.extend(new_candidates)
    deletes.update((totals.CANDIDATES, item.item_id) for item in old_candidates)
    for identity in sorted(identities):
        fold = folds.get(identity, totals.empty())
        existing = groups.get(identity)
        if existing is not None:
            totals.category_adjust(categories, existing.value, -1)
        totals.adjust(fold, previous[identity], -1)
        totals.adjust(fold, proposed[identity], 1)
        deletes.update(((totals.FOLDS, identity), ("activity", identity)))
        if not fold["count"]:
            if (
                fold["unknown"]
                or int(fold["sum"])
                or fold["month"]
                or fold["dates"]
                or fold["vouchers"]
            ):
                index.corrupt()
            continue
        removed_candidates = {item.item_id for item in old_candidates if item.group_key == identity}
        candidates = [item for item in new_candidates if item.group_key == identity]
        old_first = fold.get("first")
        keep_first = old_first is not None and old_first[1] not in removed_candidates
        if keep_first:
            first_items = directory.items(totals.CANDIDATES, (old_first[1],))
            if (
                len(first_items) != 1
                or [first_items[0].sort_key, first_items[0].item_id] != old_first
                or first_items[0].group_key != identity
            ):
                index.corrupt()
            candidates.extend(first_items)
        else:
            candidates.extend(
                _survivors(directory, totals.CANDIDATES, identity, removed_candidates)
            )
        if not candidates:
            index.corrupt()
        chosen = min(candidates, key=lambda item: (item.sort_key, item.item_id))
        if keep_first and chosen.item_id == old_first[1]:
            representative = None
        else:
            members = {member["row"]["key"]: member for member in proposed[identity]}
            member_id = chosen.value["member_id"]
            representative = members.get(member_id)
            if representative is None:
                representative = directory.get("activity_members", (member_id,), required=True)[
                    member_id
                ]
            if totals.candidates((representative,))[0] != chosen:
                index.corrupt()
        old_order = fold.get("order")
        removed_members = {member["row"]["key"] for member in previous[identity]}
        orders = [
            (item.sort_key, item.item_id)
            for item in (totals.member_item(member) for member in proposed[identity])
        ]
        if old_order is not None and old_order[1] not in removed_members:
            orders.append(tuple(old_order))
        else:
            orders.extend(
                (item.sort_key, item.item_id)
                for item in _survivors(directory, "activity_members", identity, removed_members)
            )
        if not orders:
            index.corrupt()
        order = min(orders)
        fold["first"], fold["order"] = [chosen.sort_key, chosen.item_id], list(order)
        value = (
            totals.group_value(existing.value, fold, order[0])
            if representative is None
            else totals.group(representative, fold, order[0])
        )
        entries.extend((value, IndexItem(totals.FOLDS, identity, identity, fold)))
        totals.category_adjust(categories, value.value, 1)
