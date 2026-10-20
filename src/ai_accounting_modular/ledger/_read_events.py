"""Actual selected resource uses retain their own cash allocation and identities."""

from dataclasses import asdict

from ai_accounting_modular.contracts.public import ReadPage, query_selection, validate_contract

from ._planning import stored_source_reference
from ._read_support import after_key, bounded, cursor, version
from ._resource_use import resource_use
from ._resource_view import resource_definition
from ._types import SettlementEvent, SettlementEventQuery
from ._values import fail


def _key(row):
    return (
        row["event_period"],
        int(row["event_day"] is None),
        row["event_day"] or "",
        row["use_table"],
        row["event_id"],
        row["result_id"],
    )


def query_settlement_events(query, page, read, selection=None):
    if not isinstance(query, SettlementEventQuery):
        fail("invalid_input", "清偿事件须使用明确的期间及来源查询。")
    for month in (query.period_from, query.period_to):
        validate_contract("core.schema.json", "Month", month)
    if query.period_from > query.period_to:
        fail("invalid_input", "清偿查询起月不能晚于末月。")
    bounded(query.source_refs, "BusinessRef")
    bounded(query.consumer_business_ids)
    bounded(query.movement_ids)
    bounded(query.components, "text")
    bounded(query.source_business_kinds, "text")
    if set(query.components) - {"money", "agency", "offset", "transfer", "reclassify_excess"}:
        fail("invalid_input", "清偿事件仅支持实际资金、代理、抵销和义务重分类。")
    selected = query_selection(read, selection)
    repo = read.repository("ledger")
    sql, args = repo.read_event_scope(query, selected)
    rows = repo.read_event_identities(sql, args)
    base = SettlementEventQuery(query.period_from, query.period_to)
    if query == base:
        full = rows
    else:
        base_sql, base_args = repo.read_event_scope(base, selected)
        full = repo.read_event_identities(base_sql, base_args)
    identity = version(
        read, "settlement_events", asdict(query), selected, {"full": full, "filtered": rows}
    )
    after = after_key(page, identity, (str, int, str, str, str, str))
    picked = repo.read_event_page(sql, args, after=after, limit=page.limit + 1)
    more, picked = len(picked) > page.limit, picked[: page.limit]
    payloads = repo.read_event_payloads(picked)
    picked = [
        dict(payloads[row["use_table"], row["result_id"], row["event_id"]], **row) for row in picked
    ]
    movement_ids = {
        row["movement_id"]
        for row in picked
        if row["use_table"] == "settlement" and row["method"] == "money"
    }
    movements = repo.movements(movement_ids, as_of=selected.as_of) if movement_ids else []
    if len(movements) != len({row["movement_id"] for row in movements}):
        fail("storage_corrupt", "同一实际资金身份被重复采用。")
    mapped = {row["movement_id"]: row for row in movements}
    heads = {
        row["result_id"]: row for row in repo.read_heads({row["result_id"] for row in movements})
    }
    items = []
    for row in picked:
        use = resource_use(row, row["use_table"], row["business_id"], stored_source_reference(row))
        movement = None
        if use.component == "money":
            actual = mapped.get(row["movement_id"])
            if actual is None or actual["result_id"] != row["result_id"]:
                fail("storage_corrupt", "实际资金清偿缺少同一正式采用的真实款项。")
            head = heads[actual["result_id"]]
            movement = resource_definition("funds", head, actual, stored_source_reference(head))
        items.append(SettlementEvent(use, movement))
    next_cursor = cursor(identity, _key(picked[-1])) if more else None
    return ReadPage(identity, len(full), len(rows), tuple(items), next_cursor)
