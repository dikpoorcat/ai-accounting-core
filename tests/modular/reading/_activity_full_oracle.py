"""Independent complete Activity formulas from the previous display factory."""

from collections import Counter, defaultdict

from ai_accounting_modular.contracts.public import format_fen, parse_fen
from ai_accounting_modular.reading._collections import IndexItem
from ai_accounting_modular.reading._funds_items import date_key

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
