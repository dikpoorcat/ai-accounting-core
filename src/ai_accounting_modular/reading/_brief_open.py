"""Publish complete open-balance groups, hydrating only a visible member page."""

from collections import Counter

from ai_accounting_modular.contracts.public import ContractError, PageQuery, ReadInputs

from . import _brief_open_displays as displays
from . import _brief_open_groups as groups
from . import _brief_open_index as index
from . import _brief_open_inputs as inputs
from . import _brief_open_members as member_rows
from . import _brief_open_positions as positions
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


def bind_displays(reader, members, captured, read):
    return displays.capture(
        reader,
        [rows.reference(member) for member in members],
        read,
        known_origins=captured.contents.values(),
    )


def prepare(reader, scope, read):
    captured = sources.capture(reader, scope, read)
    members = rows.project(captured, scope["company_id"], reader.identity, read)
    cooperation = reader.investment.cooperation_positions(scope, read)
    selected_positions = cooperation.positions
    current_scope = {
        "company_id": scope["company_id"],
        "period_from": captured.current_cutoff,
        "period_to": captured.current_cutoff,
        "view": "current",
    }
    current_positions = []
    contracts = sorted(
        {
            item.contract_id
            for item in selected_positions
            if item.operator_due_fen or item.company_due_fen
        }
    )
    for batch in sources.batches(contracts):
        current_positions.extend(
            reader.investment.cooperation_positions(
                current_scope, read, contract_ids=batch
            ).positions
        )
    extra = positions.project(
        selected_positions,
        current_positions,
        company_id=scope["company_id"],
        cutoff_period=scope["period_to"],
        identity=reader.identity,
        read=read,
    )
    ordinary_count = len(members)
    members.extend(extra)
    summary, entries, folds, categories = groups.prepare(
        members, scope["period_to"], captured.current_cutoff, complete=captured.complete
    )
    member_entries = [member_item(member) for member in members[:ordinary_count]]
    member_entries.extend(
        member_item(member, position_order=position) for position, member in enumerate(extra)
    )
    first = {}
    for value in member_entries:
        candidate = value.sort_key, value.item_id
        current = first.get(value.group_key)
        if current is None or candidate < current:
            first[value.group_key] = candidate
    for identity, fold in folds.items():
        order, member_id = first[identity]
        fold["first"] = {"id": member_id, "sort": order}
    entries.extend(member_entries)
    bindings = bind_displays(reader, members, captured, read)
    records = inputs.records(captured, members, bindings)
    display_ids = {identity for value in records.values() for identity in value["objects"]}
    businesses = tuple(
        sorted({body.reference["business_id"] for body in captured.contents.values()})
    )
    dependencies = cooperation.inputs.merge(
        ReadInputs(
            result_kinds=("opening.package",),
            all_obligations=True,
            business_ids=businesses,
            current_business_ids=tuple(sorted(set(businesses) | set(contracts))),
            object_ids=tuple(sorted(display_ids)),
        )
    )
    records = inputs.full(dependencies, records, cooperation.inputs.object_ids)
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
            "_inputs": inputs.static(dependencies).wire(),
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
