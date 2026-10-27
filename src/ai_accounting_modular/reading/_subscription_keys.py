"""Compile public input selectors into ordered, individually addressable subscriptions."""

from collections import defaultdict
from dataclasses import fields

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadInputs,
    loads_json,
    validate_contract,
)

FLAGS = frozenset({"movements", "all_results", "posted_results", "all_obligations"})
FIELDS = frozenset(field.name for field in fields(ReadInputs))
SPECIAL = frozenset({"_closure_after", "_closure_any", "_working_period", "_issues"})


def corrupt():
    raise ContractError("storage_corrupt", "读取输入订阅的范围、索引或保存内容不完整。")


def key(*values):
    return "".join(value.replace("\0", "\0\1") + "\0\0" for value in values)


def bounds(*values):
    first = key(*values)
    return first, first[:-1] + "\1"


def target(scope_key, component):
    try:
        scope = loads_json(scope_key)
        if set(scope) != {"page", "scope"} or scope["page"] not in {"monthly", "quarterly_report"}:
            corrupt()
        month = scope["scope"]["period_to"]
        validate_contract("core.schema.json", "Month", month)
        if type(component) is not str or not component:
            corrupt()
        return month
    except (ContractError, KeyError, TypeError, ValueError) as error:
        raise ContractError("storage_corrupt", "读取订阅没有明确的页面及月份范围。") from error


def selectors(selected):
    if not isinstance(selected, ReadInputs):
        raise TypeError("reading subscriptions require ReadInputs")
    return frozenset(
        (field.name, "")
        for field in fields(selected)
        if field.name in FLAGS and getattr(selected, field.name)
    ) | frozenset(
        (field.name, identity)
        for field in fields(selected)
        if field.name not in FLAGS
        for identity in getattr(selected, field.name)
    )


def automatic(component, month):
    if component == "brief_context":
        return frozenset({("note_scopes", "period:" + month), ("_issues", "")})
    if component == "brief_open_items":
        return frozenset({("_closure_any", ""), ("_working_period", "")})
    if component in {"brief_activity", "financial"}:
        return frozenset({("_closure_after", "")})
    return frozenset()


def queries(impact):
    """Yield selector prefix, inclusive month lower bound, exclusive upper, component."""
    result = set()

    def add(field, identity="", first="", last=None, component=None):
        result.add((field, identity, first, last, component))

    for scope in impact.note_scopes:
        add("note_scopes", scope)
    for identity, kind in impact.objects:
        add("object_ids", identity)
        add("object_kinds", kind)
    for identity, kind, _ in impact.facts:
        add("current_fact_ids", identity)
        add("fact_kinds", kind)
    for change in impact.formal_changes:
        identities = {change.business_id, *change.resource_business_ids}
        for identity in identities:
            add("current_business_ids", identity)
        # A later adoption can retire the historical Activity's right to use
        # today's description, while leaving its frozen financial rows intact.
        add("note_scopes", "business:" + change.business_id, component="brief_activity")
        if not change.posted_periods:
            continue
        first = min(change.posted_periods)
        add("all_results", first=first)
        for identity in identities:
            add("business_ids", identity, first)
        for field, values in (
            ("result_kinds", change.result_kinds),
            ("account_codes", change.account_codes),
            ("obligation_categories", change.obligation_categories),
            ("cost_categories", change.cost_categories),
        ):
            for identity in values:
                add(field, identity, first)
        if change.obligation_categories:
            add("all_obligations", first=first)
        for period in change.posted_periods:
            add("posted_results", first=period, last=period + "\0")
            for kind in change.result_kinds:
                add("posted_result_kinds", kind, period, period + "\0")
        for period in change.business_periods:
            if period >= first:
                for kind in change.result_kinds:
                    add("business_result_kinds", kind, period, period + "\0")
        for period in change.movement_periods:
            if period >= first:
                add("movements", first=period, last=period + "\0")
    if impact.issue_periods:
        add("_issues", first="" if None in impact.issue_periods else min(impact.issue_periods))
    if impact.working_period_changed:
        add("_working_period")
    if impact.closure_ranges:
        add("_closure_any")
        add("_closure_after", first=min(value.period_from for value in impact.closure_ranges))
    grouped = defaultdict(list)
    for field, identity, first, last, component in result:
        grouped[field, identity, component].append((first, last))
    merged = []
    for (field, identity, component), ranges in grouped.items():
        current = None
        for first, last in sorted(ranges, key=lambda value: value[0]):
            if current is None:
                current = first, last
            elif current[1] is None:
                continue
            elif first <= current[1]:
                current = current[0], None if last is None else max(current[1], last)
            else:
                merged.append((field, identity, *current, component))
                current = first, last
        merged.append((field, identity, *current, component))
    return tuple(merged)
