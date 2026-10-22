"""Bounded activity groups and member pages with page-local full vouchers."""

from ai_accounting_modular.contracts.public import ContractError, PageQuery

from . import _brief_vouchers, _monthly
from ._collections import CollectionFilter, wire_page


def activity_page(snapshot, read, query):
    category = query.get("activity_group")
    filters = CollectionFilter(categories=(category,)) if category else CollectionFilter()
    if query.get("activity_key"):
        member = member_scope(snapshot, read, query["activity_key"])
        filters = CollectionFilter(item_id=member["row"]["group_key"])
    return wire_page(
        _monthly.collection(
            snapshot,
            "brief_activity",
            "activity",
            filters,
            PageQuery(query.get("limit", 20), query.get("cursor")),
            read,
        )
    )


def member_scope(snapshot, read, activity_key):
    page = _monthly.collection(
        snapshot,
        "brief_activity",
        "activity_members",
        CollectionFilter(item_id=activity_key),
        PageQuery(1),
        read,
    )
    if not page.items:
        raise ContractError("not_found", "本月没有所定位的业务活动，请刷新简报。")
    return page.items[0]


def build(reader, snapshot, read, query):
    group_key = query["group_key"]
    group = _monthly.collection(
        snapshot,
        "brief_activity",
        "activity",
        CollectionFilter(item_id=group_key),
        PageQuery(1),
        read,
    )
    if not group.items:
        raise ContractError("not_found", "本月没有所选业务组，请刷新简报。")
    page = _monthly.collection(
        snapshot,
        "brief_activity",
        "activity_members",
        CollectionFilter(group_key=group_key),
        PageQuery(query.get("limit", 20), query.get("cursor")),
        read,
    )
    tokens = list(
        dict.fromkeys(
            item["row"]["voucher_version_id"]
            for item in page.items
            if item["row"]["voucher_version_id"]
        )
    )
    descriptors = _monthly.collection_items(
        snapshot, "brief_activity", "brief_vouchers", tokens, read
    )
    members = wire_page(page)
    members["items"] = [item["row"] for item in page.items]
    vouchers = _brief_vouchers.for_descriptors(reader, descriptors, read)
    return {
        "selected_period": snapshot.selected_period,
        "snapshot_version": snapshot.version,
        "data": {
            "section": "activity",
            "group_key": group_key,
            "collections": {
                "members": members,
                "vouchers": _brief_vouchers.complete_page(vouchers, page.version),
            },
        },
    }
