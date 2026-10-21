"""Whole-scope open-balance summaries stay separate from their member pages."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import content_digest, format_fen, parse_fen, sum_fen

from ._collections import IndexItem
from ._funds_items import name_key

CATEGORIES = {
    "customer_receivables": ("待收客户款", "receivable"),
    "supplier_advances": ("待冲抵供应商预付款", "receivable"),
    "refundable_deposit_receivables": ("待收回保证金", "receivable"),
    "other_receivables": ("其他应收事项", "receivable"),
    "supplier_payables": ("待付供应商款", "payable"),
    "employee_payables": ("待付员工报销款", "payable"),
    "payroll_payables": ("待付工资、社保与个税", "payable"),
    "labor_payables": ("待付个人劳务及个税", "payable"),
    "other_payables": ("其他应付事项", "payable"),
}


def group_key(company_id, direction, category, matter, party_id, reference):
    identity = party_id if party_id is not None else reference
    return "open:" + content_digest([company_id, direction, category, matter, identity])


def status(remaining, paid=0, other=0):
    if remaining < 0:
        return "over_settled"
    if remaining == 0:
        return "settled"
    return "partial" if paid or other else "open"


def _sum(rows, field):
    values = [row[field] for row in rows]
    return (
        None
        if any(value is None for value in values)
        else format_fen(sum_fen(parse_fen(value) for value in values))
    )


def _combined(rows, *, current=False):
    field = "current_status" if current else "status"
    states = [row[field] for row in rows]
    if any(state is None for state in states):
        return None
    if "over_settled" in states:
        return "over_settled"
    if set(states) == {"current"}:
        return "current"
    if any(
        state not in {"open", "partial", "settled", "reversed", "withdrawn"} for state in states
    ):
        return "checking"
    if "open" in states or "partial" in states:
        return "partial" if len(set(states)) > 1 or "partial" in states else "open"
    if "reversed" in states:
        return "reversed"
    if "withdrawn" in states:
        return "withdrawn"
    return "settled"


def prepare(rows, cutoff_period, current_cutoff, *, complete):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row["group_key"]].append(row)
    groups = []
    for key, members in grouped.items():
        first = members[0]
        groups.append(
            {
                "id": key,
                "group_key": key,
                "category_key": first["category_key"],
                "party": first["party"],
                "description": first["_matter_title"],
                "member_count": len(members),
                **{
                    field: _sum(members, field)
                    for field in (
                        "source_amount_fen",
                        "paid_fen",
                        "other_settled_fen",
                        "outstanding_fen",
                        "current_outstanding_fen",
                    )
                },
                "status": _combined(members),
                "current_status": _combined(members, current=True),
            }
        )
    groups.sort(
        key=lambda row: (name_key(row["party"]), name_key(row["description"]), row["group_key"])
    )
    categories = []
    for key, (label, direction) in CATEGORIES.items():
        own = [group for group in groups if group["category_key"] == key]
        if own:
            categories.append(
                {
                    "key": key,
                    "label": label,
                    "direction": direction,
                    "unit": "笔",
                    "count": sum(row["member_count"] for row in own),
                    "group_count": len(own),
                    "loaded_count": 0,
                    "outstanding_fen": _sum(own, "outstanding_fen"),
                }
            )
    summary = {
        "group_count": len(groups),
        "total_count": len(rows),
        "complete": complete,
        "cutoff_period": cutoff_period,
        "current_cutoff_period": current_cutoff,
        "categories": categories,
    }
    for direction in ("receivable", "payable"):
        own = [row for row in categories if row["direction"] == direction]
        summary[direction + "_count"] = sum(row["count"] for row in own)
        summary[direction + "_fen"] = _sum(own, "outstanding_fen") if complete else None
    entries = [
        IndexItem(
            "open_items",
            row["group_key"],
            f"{index:020d}",
            row,
            group_key=row["group_key"],
            category=row["category_key"],
        )
        for index, row in enumerate(groups)
    ]
    return summary, entries
