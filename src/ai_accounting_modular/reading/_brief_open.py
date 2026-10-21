"""Publish complete open-balance groups, hydrating only a visible member page."""

from collections import Counter

from ai_accounting_modular.contracts.public import ContractError, PageQuery

from . import _brief_open_cooperation as cooperation
from . import _brief_open_displays as displays
from . import _brief_open_groups as groups
from . import _brief_open_index as index
from . import _brief_open_inputs as inputs
from . import _brief_open_members as member_rows
from . import _brief_open_rows as rows
from . import _brief_open_sources as sources
from . import _brief_vouchers, _monthly
from ._collections import CollectionFilter, IndexItem, wire_page
from ._funds_items import key

_COMPONENT = "brief_open_items"


def member_item(member, *, position_order=None):
    order = (
        key("0", *member["_sort"]) if "_locator" in member else key("1", f"{position_order:020d}")
    )
    return IndexItem(
        "open_members",
        member["id"],
        order,
        member,
        business_id=member["subject_id"],
        group_key=member["group_key"],
        category=member["category_key"],
    )


def member_items(ordinary, extras):
    return [member_item(member) for member in ordinary] + [
        member_item(member, position_order=position) for position, member in enumerate(extras)
    ]


def bind_displays(reader, members, captured, read):
    return displays.capture(
        reader,
        [rows.reference(member) for member in members],
        read,
        known_origins=captured.contents.values(),
        known_vouchers=captured.selected_vouchers,
    )


def prepare(reader, scope, read):
    captured = sources.capture(reader, scope, read)
    members = rows.project(captured, scope["company_id"], reader.identity, read)
    extra, cooperation_inputs = cooperation.capture(reader, scope, read, captured.current_cutoff)
    member_entries = member_items(members, extra)
    members.extend(extra)
    summary, entries, folds, categories = groups.prepare(
        member_entries, scope["period_to"], captured.current_cutoff, complete=captured.complete
    )
    entries.extend(member_entries)
    bindings = bind_displays(reader, members, captured, read)
    records = inputs.records(captured, members, bindings)
    records = inputs.with_provider(cooperation_inputs, records)
    entries.extend(
        index.item(index.BUSINESSES, identity, value) for identity, value in records.items()
    )
    entries.extend(
        index.item(index.OBJECTS, identity, {"count": count})
        for identity, count in inputs.object_counts(records).items()
    )
    entries.extend(index.item(index.FOLDS, identity, value) for identity, value in folds.items())
    entries.extend(
        index.item(index.VOUCHERS, identity, {"count": count})
        for identity, count in inputs.voucher_counts(records).items()
    )
    return index.complete(
        {
            "open_items": summary,
            "_category_folds": categories,
            "_inputs": inputs.static(inputs.ORDINARY.merge(cooperation_inputs)).wire(),
            "_cooperation_inputs": cooperation_inputs.wire(),
        },
        entries,
        read.head,
    )


def page(snapshot, read, query):
    selected = _monthly.collection(
        snapshot,
        _COMPONENT,
        "open_items",
        CollectionFilter(),
        PageQuery(query.get("limit", 20), query.get("cursor")),
        read,
    )
    loaded = Counter()
    for group in selected.items:
        loaded[group["category_key"]] += group["member_count"]
    summary = snapshot.components[_COMPONENT]["open_items"]
    return {
        "open_items": dict(summary)
        | {
            "categories": [
                dict(category) | {"loaded_count": loaded[category["key"]]}
                for category in summary["categories"]
            ]
        },
        "collections": {"open_items": wire_page(selected)},
    }


def _member_page(snapshot, read, filters, page):
    return _monthly.collection(snapshot, _COMPONENT, "open_members", filters, page, read)


def member_scope(reader, snapshot, read, obligation_key):
    selected = _member_page(snapshot, read, CollectionFilter(item_id=obligation_key), PageQuery(1))
    if not selected.items:
        raise ContractError("not_found", "本月没有所定位的待收待付款项，请刷新简报。")
    values, _ = member_rows.hydrate(reader, selected.items, snapshot.company_id, read)
    return values[0]["_detail_scope"]


def members(reader, snapshot, read, query):
    group_key = query["group_key"]
    selected = _member_page(
        snapshot,
        read,
        CollectionFilter(group_key=group_key),
        PageQuery(query.get("limit", 20), query.get("cursor")),
    )
    if selected.filtered_count == 0:
        raise ContractError("not_found", "本月没有所选待收待付组，请刷新简报。")
    hydrated, contents = member_rows.hydrate(reader, selected.items, snapshot.company_id, read)
    descriptors = _brief_vouchers.descriptors_for_references(
        reader,
        [item["_detail_scope"]["reference"] for item in hydrated],
        read,
        known_contents=contents,
    )
    values, unique = [], {}
    for item, descriptor in zip(hydrated, descriptors, strict=True):
        value = {key: val for key, val in item.items() if not key.startswith("_")}
        if descriptor is not None:
            template = descriptor["template"]
            value.update(
                voucher_version_id=template["voucher_version_id"],
                voucher_number=int(template["number"]),
            )
            unique.setdefault(template["voucher_version_id"], descriptor)
        values.append(value)
    member_page = wire_page(selected)
    member_page["items"] = values
    vouchers = _brief_vouchers.for_descriptors(reader, unique.values(), read)
    return {
        "section": "open_items",
        "group_key": group_key,
        "collections": {
            "members": member_page,
            "vouchers": _brief_vouchers.complete_page(vouchers, selected.version),
        },
    }
