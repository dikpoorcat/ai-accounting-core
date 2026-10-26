"""Rebuildable open-group folds retain unknown counts and the original status rules."""

from ai_accounting_modular.contracts.public import ContractError, format_fen, parse_fen, sum_fen

from ._business_descriptions import common_text

FIELDS = (
    "source_amount_fen",
    "paid_fen",
    "other_settled_fen",
    "outstanding_fen",
    "current_outstanding_fen",
)


def empty():
    return {
        "count": 0,
        "amounts": {field: {"sum": "0", "unknown": 0} for field in FIELDS},
        "states": {"status": {}, "current_status": {}},
        "descriptions": {},
    }


def _count(value, delta):
    result = value + delta
    if result < 0:
        raise ContractError("storage_corrupt", "待收待付汇总的项目数量不完整，请重新生成。")
    return result


def _amount(total, value, sign):
    if value is None:
        total["unknown"] = _count(total["unknown"], sign)
    else:
        # An unknown total must not reject large known subtotals which the full
        # projection would never add. Check the published known sum only.
        total["sum"] = str(int(total["sum"]) + sign * parse_fen(value))


def adjust(fold, member, sign):
    fold["count"] = _count(fold["count"], sign)
    text = member.get("_description") or member["_matter_title"]
    count = _count(fold["descriptions"].get(text, 0), sign)
    if count:
        fold["descriptions"][text] = count
    else:
        fold["descriptions"].pop(text, None)
    for field in FIELDS:
        _amount(fold["amounts"][field], member[field], sign)
    for field, counts in fold["states"].items():
        state = member[field] if member[field] is not None else "_unknown"
        amount = _count(counts.get(state, 0), sign)
        if amount:
            counts[state] = amount
        else:
            counts.pop(state, None)


def amount(total):
    return None if total["unknown"] else format_fen(sum_fen((int(total["sum"]),)))


def _status(counts):
    states = set(counts)
    if "_unknown" in states:
        return None
    if "over_settled" in states:
        return "over_settled"
    if states == {"current"}:
        return "current"
    if states - {"open", "partial", "settled", "reversed", "withdrawn"}:
        return "checking"
    if "open" in states or "partial" in states:
        return "partial" if len(states) > 1 or "partial" in states else "open"
    if "reversed" in states:
        return "reversed"
    if "withdrawn" in states:
        return "withdrawn"
    return "settled"


def group(first, fold):
    return {
        "id": first["group_key"],
        "group_key": first["group_key"],
        "category_key": first["category_key"],
        "party": first["party"],
        "description": common_text(fold["descriptions"], first["_matter_title"]),
        "member_count": fold["count"],
        **{field: amount(total) for field, total in fold["amounts"].items()},
        "status": _status(fold["states"]["status"]),
        "current_status": _status(fold["states"]["current_status"]),
    }


def category_adjust(folds, value, sign):
    key = value["category_key"]
    fold = folds.setdefault(key, {"groups": 0, "count": 0, "sum": "0", "unknown": 0})
    fold["groups"] = _count(fold["groups"], sign)
    fold["count"] = _count(fold["count"], sign * value["member_count"])
    _amount(fold, value["outstanding_fen"], sign)
    if not fold["groups"]:
        if fold["count"] or fold["unknown"] or int(fold["sum"]):
            raise ContractError("storage_corrupt", "待收待付分类汇总与实际组不一致。")
        folds.pop(key)


def summary(folds, categories, cutoff, current, complete):
    values = [
        {
            "key": key,
            "label": label,
            "direction": direction,
            "unit": "笔",
            "count": folds[key]["count"],
            "group_count": folds[key]["groups"],
            "loaded_count": 0,
            "outstanding_fen": amount(folds[key]),
        }
        for key, (label, direction) in categories.items()
        if key in folds
    ]
    result = {
        "group_count": sum(value["group_count"] for value in values),
        "total_count": sum(value["count"] for value in values),
        "complete": complete,
        "cutoff_period": cutoff,
        "current_cutoff_period": current,
        "categories": values,
    }
    for direction in ("receivable", "payable"):
        own = [value for value in values if value["direction"] == direction]
        amounts = [value["outstanding_fen"] for value in own]
        result[direction + "_count"] = sum(value["count"] for value in own)
        result[direction + "_fen"] = (
            None
            if not complete or None in amounts
            else format_fen(sum_fen(parse_fen(value) for value in amounts))
        )
    return result
