"""Whole-scope open-balance summaries stay separate from their member pages."""

from collections import defaultdict

from ai_accounting_modular.contracts.public import content_digest

from . import _brief_open_totals as totals
from ._collections import IndexItem
from ._funds_items import key, name_key

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


def group_item(value):
    return IndexItem(
        "open_items",
        value["group_key"],
        key(*name_key(value["party"]), *name_key(value["description"]), value["group_key"]),
        value,
        group_key=value["group_key"],
        category=value["category_key"],
    )


def prepare(member_entries, cutoff_period, current_cutoff, *, complete):
    grouped = defaultdict(list)
    for entry in member_entries:
        grouped[entry.group_key].append(entry)
    folds, categories, entries = {}, {}, []
    for identity, members in grouped.items():
        fold = folds[identity] = totals.empty()
        for member in members:
            totals.adjust(fold, member.value, 1)
        first = min(members, key=lambda item: (item.sort_key, item.item_id))
        fold["first"] = {"id": first.item_id, "sort": first.sort_key}
        value = totals.group(first.value, fold)
        totals.category_adjust(categories, value, 1)
        entries.append(group_item(value))
    return (
        totals.summary(categories, CATEGORIES, cutoff_period, current_cutoff, complete),
        entries,
        folds,
        categories,
    )
