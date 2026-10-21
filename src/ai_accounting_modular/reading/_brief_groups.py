"""Complete group summaries and bounded member pages with page-local full vouchers."""

from collections import Counter, defaultdict

from ai_accounting_modular.contracts.public import ContractError, PageQuery, format_fen, parse_fen

from . import _brief_vouchers, _monthly
from ._collections import CollectionFilter, IndexItem, wire_page
from ._funds_items import date_key

LABELS = {
    "business": "日常业务",
    "cash": "资金收付",
    "people": "人员报酬",
    "assets": "资产与项目",
    "tax": "税费",
    "investment": "融资与投资",
    "correction": "更正",
}


def indexes(members):
    grouped = defaultdict(list)
    for member in members:
        grouped[member["row"]["group_key"]].append(member)
    items, counts = [], defaultdict(Counter)
    for group_key, entries in grouped.items():
        rows = [item["row"] for item in entries]
        first = rows[0]
        values = [row["amount_fen"] for row in rows]
        dates = [row["date"] for row in rows if row["date"]]
        value = {
            "key": group_key,
            "group_key": group_key,
            "group": first["group"],
            "kind": entries[0]["scope"]["kind"],
            "party": first["party"],
            "title": first["title"],
            "member_count": len(rows),
            "voucher_count": len(
                {row["voucher_version_id"] for row in rows if row["voucher_version_id"]}
            ),
            "amount_fen": None if None in values else format_fen(sum(parse_fen(v) for v in values)),
            "amount_label": first["amount_label"],
            "state": first["state"],
            "date_from": min(dates) if dates else None,
            "date_to": max(dates) if dates else None,
            "has_month_recognition": any(
                row["recognition"]["precision"] == "month" for row in rows
            ),
            "is_batch": entries[0]["batch"],
        }
        order = min(
            date_key(row["recognition"]["period"], row["date"], row["party"], row["key"])
            for row in rows
        )
        items.append(IndexItem("activity", group_key, order, value, category=first["group"]))
        counts[first["group"]]["events"] += len(rows)
        counts[first["group"]]["groups"] += 1
        counts[first["group"]][first["title"]] += len(rows)
        for entry in entries:
            row = entry["row"]
            items.append(
                IndexItem(
                    "activity_members",
                    row["key"],
                    date_key(row["recognition"]["period"], row["date"], row["party"], row["key"]),
                    entry,
                    business_id=row["subject_id"],
                    group_key=group_key,
                    category=first["group"],
                )
            )
    summary = [
        {
            "key": category,
            "label": LABELS[category],
            "event_count": counts[category]["events"],
            "group_count": counts[category]["groups"],
            "type_counts": [
                {"label": label, "count": count}
                for label, count in sorted(counts[category].items())
                if label not in {"events", "groups"}
            ],
        }
        for category in LABELS
        if category in counts
    ]
    return items, summary


def review_businesses(members):
    """Summarize every actual activity part, independently of group or member pagination."""
    grouped = defaultdict(list)
    for member in members:
        row, scope = member["row"], member["scope"]
        role = scope["voucher_role"]
        action = "correction" if role != "original" else "business"
        grouped[row["title"], action, role, row["amount_label"]].append(row["amount_fen"])
    return [
        {
            "label": title,
            "action": action,
            "reversal": role == "reversal",
            "count": len(values),
            "amount_label": amount_label,
            "business_amount_fen": None
            if None in values
            else format_fen(sum(parse_fen(value) for value in values)),
        }
        for (title, action, role, amount_label), values in sorted(grouped.items())
    ]


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
