"""Describe actual changed formal inputs before replacing open result children."""

from collections import defaultdict
from dataclasses import dataclass

from ai_accounting_modular.contracts.public import loads_json

from ._formal_output import _line_totals, target_vouchers


@dataclass(frozen=True)
class FormalChange:
    """Input identities, not page tags or calculated replacement balances.

    Open replacement affects its old and new posting months. A frozen original
    stays unchanged: only the real discovery month enters posted_periods.
    """

    business_id: str
    result_slot: str
    business_periods: tuple[str, ...]
    posted_periods: tuple[str, ...]
    result_kinds: tuple[str, ...]
    account_codes: tuple[str, ...] = ()
    obligation_categories: tuple[str, ...] = ()
    cost_categories: tuple[str, ...] = ()
    resource_business_ids: tuple[str, ...] = ()
    movement_periods: tuple[str, ...] = ()


_TABLES = (
    "obligation",
    "cost_lot",
    "settlement",
    "cost_consumption",
    "obligation_reclassification",
    "result_fund_movement",
)


def describe_changes(repo, retired, proposed, old_by_key):
    """Batch changed old outputs; retained results are not read again.

    Ledger owns the old child representation. New signals come directly from
    the already validated public plan, without rebuilding its persistence rows.
    """
    specs = [(old, None, None) for old in retired]
    specs.extend(
        (
            old_by_key.get((item.plan["business_id"], item.plan["result_slot"])),
            item.plan,
            item.posting_period,
        )
        for item in proposed
        if not item.bound.unchanged
    )
    ids = {old["result_id"] for old, _, _ in specs if old is not None}
    heads = {row["result_id"]: row for row in repo.results(ids)} if ids else {}
    journals = target_vouchers(repo, list(heads.values())) if ids else {}
    children = {table: defaultdict(list) for table in _TABLES}
    if ids:
        for table, groups in children.items():
            for row in repo.children(table, ids):
                groups[row["result_id"]].append(row)
    return tuple(
        _describe(
            heads[old["result_id"]] if old else None,
            plan,
            posting_period,
            journals.get(old["result_id"]) if old else None,
            {table: groups[old["result_id"]] if old else [] for table, groups in children.items()},
        )
        for old, plan, posting_period in specs
    )


def _describe(old, plan, posting_period, journal, previous):
    old_lines, new_lines = journal["lines"] if journal else [], plan["lines"] if plan else []
    period_changed = bool(old and plan and old["business_period"] != plan["accounting_period"])
    old_totals = {tuple(key): value for key, value in _line_totals(old_lines)}
    new_totals = {tuple(key): value for key, value in _line_totals(new_lines)}
    codes = {
        key[0]
        for key in old_totals.keys() | new_totals.keys()
        if period_changed or old_totals.get(key) != new_totals.get(key)
    }
    kinds, business_periods, posted = set(), set(), set()
    if old:
        kinds.add(loads_json(old["target_json"])["kind"])
        business_periods.add(old["business_period"])
        if old["state"] == "open":
            posted.add(old["posting_period"])
    resource_ids = {
        row[key]
        for table in ("settlement", "cost_consumption", "obligation_reclassification")
        for row in previous[table]
        for key in ("source_business_id", "target_business_id")
        if row.get(key) is not None
    }
    obligations = {row["category"] for row in previous["obligation"]}
    costs = {row["category"] for row in previous["cost_lot"]}
    movements = {row["actual_on"][:7] for row in previous["result_fund_movement"]}
    if plan is not None:
        kinds.add(plan["target"]["kind"])
        business_periods.add(plan["accounting_period"])
        posted.add(posting_period)
        obligations.update(item["category"] for item in plan["obligations"])
        costs.update(item["category"] for item in plan["cost_lots"])
        movements.update(item["actual_on"][:7] for item in plan["fund_movements"])
        resource_ids.update(
            item["source"]["business_id"]
            for field in ("settlements", "cost_consumptions", "obligation_reclassifications")
            for item in plan[field]
        )
        resource_ids.update(
            item["target"]["business_id"] for item in plan["obligation_reclassifications"]
        )
    identity = plan if plan is not None else old
    return FormalChange(
        identity["business_id"],
        identity["result_slot"],
        tuple(sorted(business_periods)),
        tuple(sorted(posted)),
        tuple(sorted(kinds)),
        tuple(sorted(codes)),
        tuple(sorted(obligations)),
        tuple(sorted(costs)),
        tuple(sorted(resource_ids)),
        tuple(sorted(movements)),
    )
