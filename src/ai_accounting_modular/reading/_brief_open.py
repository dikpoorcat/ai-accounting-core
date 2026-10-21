"""Publish complete open-balance groups, hydrating only a visible member page."""

from collections import Counter

from ai_accounting_modular.contracts.public import ContractError, PageQuery

from . import _brief_open_groups as groups
from . import _brief_open_positions as positions
from . import _brief_open_rows as rows
from . import _brief_open_sources as sources
from . import _brief_vouchers, _monthly
from ._collections import CollectionFilter, IndexItem, prepare_items, prepare_model, wire_page

_COMPONENT = "brief_open_items"


def prepare(reader, scope, read):
    captured = sources.capture(reader, scope, read)
    members = rows.project(captured, scope["company_id"], reader.identity, read)
    selected_positions = reader.investment.cooperation_positions(scope, read).positions
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
    members.extend(
        positions.project(
            selected_positions,
            current_positions,
            company_id=scope["company_id"],
            cutoff_period=scope["period_to"],
            identity=reader.identity,
            read=read,
        )
    )
    summary, entries = groups.prepare(
        members, scope["period_to"], captured.current_cutoff, complete=captured.complete
    )
    entries.extend(
        IndexItem(
            "open_members",
            member["id"],
            f"{index:020d}",
            {key: value for key, value in member.items() if key != "_matter_title"},
            business_id=member["subject_id"],
            group_key=member["group_key"],
            category=member["category_key"],
        )
        for index, member in enumerate(members)
    )
    return prepare_model(
        {
            "open_items": summary,
            "_voucher_display_head": _brief_vouchers.display_head(
                reader, [member["_detail_scope"]["reference"] for member in members], read
            ),
        },
        items=prepare_items(entries, collections=("open_items", "open_members")),
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


def member_scope(snapshot, read, obligation_key):
    selected = _member_page(snapshot, read, CollectionFilter(item_id=obligation_key), PageQuery(1))
    if not selected.items:
        raise ContractError("not_found", "本月没有所定位的待收待付款项，请刷新简报。")
    return selected.items[0]["_detail_scope"]


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
    descriptors = _brief_vouchers.descriptors_for_references(
        reader, [item["_detail_scope"]["reference"] for item in selected.items], read
    )
    values, unique = [], {}
    for item, descriptor in zip(selected.items, descriptors, strict=True):
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
