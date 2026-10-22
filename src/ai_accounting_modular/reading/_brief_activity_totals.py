"""Current Activity folds preserve representative order, unknowns and exact member meanings."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import content_digest, format_fen, parse_fen

from ._brief_activity_dependencies import corrupt
from ._collections import IndexItem
from ._funds_items import date_key, key

LABELS = {
    "business": "日常业务",
    "cash": "资金收付",
    "people": "人员报酬",
    "assets": "资产与项目",
    "tax": "税费",
    "investment": "融资与投资",
    "correction": "更正",
}
CANDIDATES = "_activity_candidates"
FOLDS = "_activity_folds"


def member_order(member):
    row, scope = member["row"], member["scope"]
    reference = scope["reference"]
    return (
        0 if row["voucher_version_id"] else 1,
        row["voucher_number"],
        reference["posting_period"],
        reference["business_id"],
        reference["result_slot"],
        reference["result_id"],
        0 if scope["voucher_role"] == "reversal" else 1,
        scope["part"],
    )


def member_item(member):
    row = member["row"]
    return IndexItem(
        "activity_members",
        row["key"],
        date_key(row["recognition"]["period"], row["date"], row["party"], row["key"]),
        member,
        business_id=row["subject_id"],
        group_key=row["group_key"],
        category=row["group"],
    )


def candidate_id(business, group):
    return content_digest([business, group])


def candidates(members):
    grouped = defaultdict(list)
    for member in members:
        grouped[member["row"]["subject_id"], member["row"]["group_key"]].append(member)
    result = []
    for (business, group), values in grouped.items():
        first = min(values, key=member_order)
        order = member_order(first)
        result.append(
            IndexItem(
                CANDIDATES,
                candidate_id(business, group),
                key(
                    str(order[0]),
                    f"{order[1]:020d}",
                    *order[2:6],
                    str(order[6]),
                    f"{order[7]:020d}",
                ),
                {"member_id": first["row"]["key"]},
                business_id=business,
                group_key=group,
            )
        )
    return result


def empty():
    return {"count": 0, "sum": "0", "unknown": 0, "vouchers": 0, "month": 0, "dates": {}}


def _sum(value):
    try:
        if type(value) is not str or str(int(value)) != value:
            corrupt()
        return int(value)
    except (TypeError, ValueError):
        corrupt()


def _count(value, delta):
    if type(value) is not int or value < 0 or value + delta < 0:
        corrupt()
    return value + delta


def adjust(fold, members, sign):
    """Add or remove complete root/group contributions, including shared voucher tokens."""
    tokens = set()
    for member in members:
        row = member["row"]
        fold["count"] = _count(fold["count"], sign)
        if row["amount_fen"] is None:
            fold["unknown"] = _count(fold["unknown"], sign)
        else:
            fold["sum"] = str(int(fold["sum"]) + sign * parse_fen(row["amount_fen"]))
        if row["voucher_version_id"]:
            tokens.add(row["voucher_version_id"])
        fold["month"] = _count(fold["month"], sign * (row["recognition"]["precision"] == "month"))
        if row["date"]:
            count = _count(fold["dates"].get(row["date"], 0), sign)
            if count:
                fold["dates"][row["date"]] = count
            else:
                fold["dates"].pop(row["date"], None)
    fold["vouchers"] = _count(fold["vouchers"], sign * len(tokens))


def validate_fold(value):
    if type(value) is not dict or set(value) != {
        "count",
        "sum",
        "unknown",
        "vouchers",
        "month",
        "dates",
        "first",
        "order",
    }:
        corrupt()
    count = value["count"]
    if type(count) is not int or count <= 0:
        corrupt()
    for field in ("unknown", "vouchers", "month"):
        if type(value[field]) is not int or not 0 <= value[field] <= count:
            corrupt()
    _sum(value["sum"])
    dates = value["dates"]
    if (
        type(dates) is not dict
        or any(
            type(day) is not str or not day or type(number) is not int or number <= 0
            for day, number in dates.items()
        )
        or sum(dates.values()) > count
    ):
        corrupt()
    for field in ("first", "order"):
        if (
            type(value[field]) is not list
            or len(value[field]) != 2
            or any(type(item) is not str or not item for item in value[field])
        ):
            corrupt()
    return value


def group(first, fold, order):
    row, scope = first["row"], first["scope"]
    return group_value(
        {
            "key": row["group_key"],
            "group_key": row["group_key"],
            "group": row["group"],
            "kind": scope["kind"],
            "party": row["party"],
            "title": row["title"],
            "amount_label": row["amount_label"],
            "state": row["state"],
            "is_batch": first["batch"],
        },
        fold,
        order,
    )


def group_value(first, fold, order):
    value = {
        field: first[field]
        for field in (
            "key",
            "group_key",
            "group",
            "kind",
            "party",
            "title",
            "amount_label",
            "state",
            "is_batch",
        )
    } | {
        "member_count": fold["count"],
        "voucher_count": fold["vouchers"],
        "amount_fen": None if fold["unknown"] else format_fen(int(fold["sum"])),
        "date_from": min(fold["dates"]) if fold["dates"] else None,
        "date_to": max(fold["dates"]) if fold["dates"] else None,
        "has_month_recognition": bool(fold["month"]),
    }
    return IndexItem("activity", value["group_key"], order, value, category=value["group"])


def category_adjust(folds, value, sign):
    fold = folds.setdefault(value["group"], {"events": 0, "groups": 0, "types": {}})
    fold["events"] = _count(fold["events"], sign * value["member_count"])
    fold["groups"] = _count(fold["groups"], sign)
    title = value["title"]
    count = _count(fold["types"].get(title, 0), sign * value["member_count"])
    if count:
        fold["types"][title] = count
    else:
        fold["types"].pop(title, None)
    if not fold["groups"]:
        if fold["events"] or fold["types"]:
            corrupt()
        folds.pop(value["group"])


def review_adjust(folds, members, sign):
    for member in members:
        row, role = member["row"], member["scope"]["voucher_role"]
        label = (
            row["title"],
            "business" if role == "original" else "correction",
            role,
            row["amount_label"],
        )
        identity = content_digest(list(label))
        fold = folds.setdefault(
            identity, {"label": list(label), "count": 0, "sum": "0", "unknown": 0}
        )
        fold["count"] = _count(fold["count"], sign)
        if row["amount_fen"] is None:
            fold["unknown"] = _count(fold["unknown"], sign)
        else:
            fold["sum"] = str(int(fold["sum"]) + sign * parse_fen(row["amount_fen"]))
        if not fold["count"]:
            if fold["unknown"] or int(fold["sum"]):
                corrupt()
            folds.pop(identity)


def summaries(categories, reviews):
    if type(categories) is not dict or type(reviews) is not dict:
        corrupt()
    for category, fold in categories.items():
        if (
            category not in LABELS
            or type(fold) is not dict
            or set(fold) != {"events", "groups", "types"}
        ):
            corrupt()
        if (
            any(type(fold[field]) is not int or fold[field] <= 0 for field in ("events", "groups"))
            or fold["groups"] > fold["events"]
        ):
            corrupt()
        if (
            type(fold["types"]) is not dict
            or any(
                type(title) is not str or type(count) is not int or count <= 0
                for title, count in fold["types"].items()
            )
            or sum(fold["types"].values()) != fold["events"]
        ):
            corrupt()
    for identity, fold in reviews.items():
        if type(fold) is not dict or set(fold) != {"label", "count", "sum", "unknown"}:
            corrupt()
        label = fold["label"]
        if (
            type(label) is not list
            or len(label) != 4
            or any(type(value) is not str for value in label)
            or identity != content_digest(label)
        ):
            corrupt()
        if label[1] != ("business" if label[2] == "original" else "correction"):
            corrupt()
        if (
            type(fold["count"]) is not int
            or fold["count"] <= 0
            or type(fold["unknown"]) is not int
            or not 0 <= fold["unknown"] <= fold["count"]
        ):
            corrupt()
        _sum(fold["sum"])
    return [
        {
            "key": category,
            "label": LABELS[category],
            "event_count": categories[category]["events"],
            "group_count": categories[category]["groups"],
            "type_counts": [
                {"label": title, "count": count}
                for title, count in sorted(categories[category]["types"].items())
            ],
        }
        for category in LABELS
        if category in categories
    ], [
        {
            "label": fold["label"][0],
            "action": fold["label"][1],
            "reversal": fold["label"][2] == "reversal",
            "count": fold["count"],
            "amount_label": fold["label"][3],
            "business_amount_fen": None if fold["unknown"] else format_fen(int(fold["sum"])),
        }
        for fold in sorted(reviews.values(), key=lambda value: value["label"])
    ]


def complete(members):
    grouped = defaultdict(list)
    for member in members:
        grouped[member["row"]["group_key"]].append(member)
    entries, categories, reviews = [], {}, {}
    entries.extend(member_item(member) for member in members)
    selected = candidates(members)
    entries.extend(selected)
    by_group = defaultdict(list)
    for candidate in selected:
        by_group[candidate.group_key].append(candidate)
    for identity, values in grouped.items():
        fold = empty()
        adjust(fold, values, 1)
        representative = min(by_group[identity], key=lambda value: (value.sort_key, value.item_id))
        ordered = min((member_item(value).sort_key, value["row"]["key"]) for value in values)
        fold["first"] = [representative.sort_key, representative.item_id]
        fold["order"] = list(ordered)
        first = min(values, key=member_order)
        item = group(first, fold, ordered[0])
        entries.extend((item, IndexItem(FOLDS, identity, identity, fold)))
        category_adjust(categories, item.value, 1)
    review_adjust(reviews, members, 1)
    return entries, categories, reviews
