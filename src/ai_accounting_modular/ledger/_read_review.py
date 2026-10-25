"""Narrow current review identities precede page payloads and share ledger arithmetic."""

from dataclasses import asdict
from sqlite3 import OperationalError

from ai_accounting_modular.contracts.public import (
    QuerySelection,
    ReadPage,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
    validate_contract_items,
)

from . import _read_contents, _read_events, _read_positions
from ._read_origin_headers import source_headers
from ._read_support import after_key, bounded, cursor, json_value, version
from ._resource_view import resource_definition
from ._review_types import (
    ReviewMovementHeader,
    ReviewMovementQuery,
    ReviewMovementSelection,
    ReviewObligationQuery,
    ReviewObligationSelection,
    ReviewSettlementHeader,
    ReviewSettlementSelection,
    ReviewSourceMetadata,
)
from ._types import ObligationDirectoryItem, ResourcePosition, SettlementEventQuery
from ._values import fail

_CURRENT = QuerySelection("current")


def _snapshot(read):
    return read.company_id, read.database_id, read.head


def _selection(selection, expected, read):
    if not isinstance(selection, expected):
        fail("invalid_input", "核查分页须使用对应的本次捕获范围。")
    if selection._snapshot != _snapshot(read):
        fail("snapshot_changed", "核查捕获不属于当前读快照，请重新捕获相关范围。")


def _periods(first, last):
    for value in (first, last):
        validate_contract("core.schema.json", "Month", value)
    if first > last:
        fail("invalid_input", "核查起月不能晚于末月。")


def _queries(query, query_type):
    queries = (query,) if isinstance(query, query_type) else query
    if not isinstance(queries, tuple | list) or not queries:
        fail("invalid_input", "核查须指定一个或一组明确的业务查询。")
    if any(not isinstance(current, query_type) for current in queries):
        fail("invalid_input", "核查查询类型与所选集合不一致。")
    return tuple(
        value for _, value in sorted({_query_key(current): current for current in queries}.items())
    )


def _query_key(query):
    return canonical_json(json_value(asdict(query)))


def _selections(selections, expected, read):
    if not isinstance(selections, tuple | list) or not selections:
        fail("invalid_input", "合并核查须提供本次捕获的一组完整范围。")
    for selection in selections:
        _selection(selection, expected, read)
    return selections


def _merged_scopes(selections):
    scopes = sorted(
        {
            _query_key(query): (query, identity)
            for selection in selections
            for query, identity in zip(selection.query, selection._scope_versions, strict=True)
        }.items()
    )
    return tuple(query for _, (query, _) in scopes), tuple(identity for _, (_, identity) in scopes)


def capture_settlements(query, read):
    queries = _queries(query, SettlementEventQuery)
    for current in queries:
        _read_events.validate_query(current, business_scope=True)
    repo = read.repository("ledger")
    selected = {}
    for current in queries:
        sql, args = repo.read_event_scope(current, _CURRENT, relations=True)
        for row in repo.read_event_identities(sql, args):
            selected[row["use_table"], row["event_id"], row["result_id"]] = row
    return _settlement_selection(queries, selected.values(), read)


def merge_settlements(selections, read):
    selections = _selections(selections, ReviewSettlementSelection, read)
    queries = _queries(
        [query for selection in selections for query in selection.query], SettlementEventQuery
    )
    rows = {
        (row["use_table"], row["event_id"], row["result_id"]): row
        for selection in selections
        for row in selection._rows
    }
    return _settlement_selection(queries, rows.values(), read)


def _settlement_selection(queries, rows, read):
    rows = sorted(rows, key=_read_events._key)
    sources, _ = source_headers(rows)
    by_id = {item.reference["result_id"]: item for item in sources}
    items = tuple(
        ReviewSettlementHeader(
            by_id[row["result_id"]].source,
            by_id[row["result_id"]].reference,
            {"business_id": row["source_business_id"], "slot": row["source_public_slot"]},
            {"business_id": row["business_id"], "slot": row["consumer_public_slot"]},
            row["event_id"],
            row["component"],
            row["event_period"],
            row["event_day"],
            row["movement_id"],
            {"business_id": row["target_business_id"], "slot": row["target_public_slot"]}
            if row["target_business_id"] is not None
            else None,
        )
        for row in rows
    )
    return ReviewSettlementSelection(
        queries,
        version(
            read, "review_settlements", [asdict(current) for current in queries], _CURRENT, rows
        ),
        items,
        tuple(rows),
        _snapshot(read),
    )


def settlement_page(selection, page, read):
    _selection(selection, ReviewSettlementSelection, read)
    after = after_key(page, selection.version, (str, int, str, str, str, str))
    chosen = [
        row for row in selection._rows if after is None or _read_events._key(row) > tuple(after)
    ][: page.limit + 1]
    more, chosen = len(chosen) > page.limit, chosen[: page.limit]
    values = _read_events.materialize(read.repository("ledger"), chosen, _CURRENT)
    return ReadPage(
        selection.version,
        selection.total_count,
        selection.total_count,
        values,
        cursor(selection.version, _read_events._key(chosen[-1])) if more else None,
    )


def _obligation_rows(query, read):
    validate_contract("core.schema.json", "Month", query.cutoff_period)
    bounded(query.business_ids)
    bounded(query.references, "BusinessRef")
    bounded(query.party_ids)
    bounded(query.categories, "text")
    validate_contract_items(
        "core.schema.json",
        "Obligation",
        [{"category": key} for key in query.categories],
        fields=("category",),
    )
    if query.direction not in (None, "receivable", "payable") or query.status not in {
        "unsettled",
        "settled",
        "all",
    }:
        fail("invalid_input", "义务核查的方向或清偿状态无效。")
    if not (query.business_ids or query.references or query.categories or query.party_ids):
        fail("invalid_input", "义务核查须指定已知身份、类别或对象范围。")
    if query.period_from is not None:
        validate_contract("core.schema.json", "Month", query.period_from)
    if query.period_to is not None:
        validate_contract("core.schema.json", "Month", query.period_to)
    if query.period_from is not None and query.period_to is not None:
        _periods(query.period_from, query.period_to)
    try:
        rows = read.repository("ledger").read_review_obligation_rows(query)
    except OperationalError as error:
        if "integer overflow" in str(error):
            fail("invalid_input", "所选义务的清偿金额超出64位整数分范围。")
        raise
    rows.sort(key=lambda row: (row["business_id"], row["public_slot"]))
    selected = [(row, _read_positions._amounts(row, "obligation")) for row in rows]
    selected = [
        (row, amounts)
        for row, amounts in selected
        if query.status == "all" or (amounts.remaining_fen == 0) == (query.status == "settled")
    ]
    return selected


def capture_obligations(query, read):
    queries = _queries(query, ReviewObligationQuery)
    if len({current.cutoff_period for current in queries}) != 1:
        fail("invalid_input", "同一义务核查范围须使用同一个经济截止月。")
    selected, scope_versions = {}, []
    for current in queries:
        captured = _obligation_rows(current, read)
        scope_versions.append(content_digest(json_value([row for row, _ in captured])))
        for row, amounts in captured:
            selected[row["business_id"], row["public_slot"]] = row, amounts
    selected = [selected[key] for key in sorted(selected)]
    rows = [row for row, _ in selected]
    sources, _ = source_headers(
        row | {"business_period": row["owner_business_period"]} for row in rows
    )
    by_id = {item.reference["result_id"]: item for item in sources}
    items = tuple(
        ObligationDirectoryItem(
            {"business_id": row["business_id"], "slot": row["public_slot"]},
            by_id[row["result_id"]].source,
            row["direction"],
            row["category"],
            row["business_period"],
            row["party_id"],
            amounts,
        )
        for row, amounts in selected
    )
    # Equal totals can hide a replaced payment or a transfer to a different debt.
    # Bind complete scoped use identities, not just the aggregate and current page.
    uses = []
    for start in range(0, len(items), 500):
        query_uses = SettlementEventQuery(
            read.start_period,
            queries[0].cutoff_period,
            source_refs=tuple(item.reference for item in items[start : start + 500]),
        )
        uses.append(query_uses)
    settlements = capture_settlements(uses, read) if uses else None
    return _obligation_selection(queries, tuple(scope_versions), items, sources, settlements, read)


def merge_obligations(selections, read):
    selections = _selections(selections, ReviewObligationSelection, read)
    queries, scope_versions = _merged_scopes(selections)
    if len({query.cutoff_period for query in queries}) != 1:
        fail("invalid_input", "同一义务核查范围须使用同一个经济截止月。")
    items = {
        (item.reference["business_id"], item.reference["slot"]): item
        for selection in selections
        for item in selection.items
    }
    items = tuple(items[key] for key in sorted(items))
    by_result = {
        source.source["result_id"]: source
        for selection in selections
        for source in selection.sources
    }
    # Match capture order: each formal source appears at its first selected slot.
    sources = tuple(
        by_result[result_id]
        for result_id in dict.fromkeys(item.source["result_id"] for item in items)
    )
    uses = [selection.settlements for selection in selections if selection.settlements is not None]
    settlements = merge_settlements(uses, read) if uses else None
    return _obligation_selection(queries, scope_versions, items, sources, settlements, read)


def _obligation_selection(queries, scope_versions, items, sources, settlements, read):
    identity = version(
        read,
        "review_obligations",
        [asdict(current) for current in queries],
        _CURRENT,
        {
            "scopes": scope_versions,
            "uses": settlements._rows if settlements else (),
        },
    )
    return ReviewObligationSelection(
        queries, identity, items, sources, scope_versions, _snapshot(read), settlements
    )


def obligation_page(selection, page, read):
    _selection(selection, ReviewObligationSelection, read)
    after = after_key(page, selection.version, (str, str))
    chosen = [
        item
        for item in selection.items
        if after is None or (item.reference["business_id"], item.reference["slot"]) > tuple(after)
    ][: page.limit + 1]
    more, chosen = len(chosen) > page.limit, chosen[: page.limit]
    keys = [
        {"result_id": item.source["result_id"], "slot": item.reference["slot"]} for item in chosen
    ]
    rows = read.repository("ledger").read_review_obligation_definitions(keys) if keys else []
    definitions = {(row["result_id"], row["public_slot"]): row for row in rows}
    heads = {item.source["result_id"]: item.reference for item in selection.sources}
    values = []
    for item in chosen:
        row = definitions.get((item.source["result_id"], item.reference["slot"]))
        if row is None:
            fail("storage_corrupt", "义务核查正文遗漏了所选资源。")
        values.append(
            ResourcePosition(
                resource_definition("obligation", heads[row["result_id"]], row, item.source),
                item.amounts,
            )
        )
    return ReadPage(
        selection.version,
        selection.total_count,
        selection.total_count,
        tuple(values),
        cursor(
            selection.version, (chosen[-1].reference["business_id"], chosen[-1].reference["slot"])
        )
        if more
        else None,
    )


def _movement_rows(query, read):
    _periods(query.period_from, query.period_to)
    bounded(query.movement_ids)
    bounded(query.business_ids)
    if query.account_id is not None:
        validate_contract("core.schema.json", "Id", query.account_id)
    if query.direction not in (None, "inflow", "outflow"):
        fail("invalid_input", "资金核查方向无效。")
    if not (query.account_id or query.movement_ids or query.business_ids):
        fail("invalid_input", "资金核查须指定真实账户、资金或业务身份。")
    return read.repository("ledger").read_review_movement_rows(query)


def capture_movements(query, read):
    queries = _queries(query, ReviewMovementQuery)
    selected, scope_versions = {}, []
    for current in queries:
        captured = _movement_rows(current, read)
        if len({row["movement_id"] for row in captured}) != len(captured):
            fail("storage_corrupt", "同一真实资金身份被重复采用。")
        scope_versions.append(content_digest(json_value(captured)))
        selected.update((row["movement_id"], row) for row in captured)
    rows = sorted(selected.values(), key=lambda row: (row["actual_on"], row["movement_id"]))
    sources, _ = source_headers(rows)
    by_id = {item.reference["result_id"]: item for item in sources}
    items = []
    for row in rows:
        row_ids = (
            loads_json(row["platform_rows_json"]) if row["platform_rows_json"] is not None else []
        )
        validate_contract_items("core.schema.json", "Id", row_ids)
        if len(set(row_ids)) != len(row_ids):
            fail("storage_corrupt", "正式资金来源重复消费同一平台原行。")
        items.append(
            ReviewMovementHeader(
                {"business_id": row["business_id"], "slot": row["public_slot"]},
                by_id[row["result_id"]].source,
                by_id[row["result_id"]].reference,
                row["movement_id"],
                row["account_id"],
                row["actual_on"],
                "inflow" if row["direction"] == "in" else "outflow",
                row["amount_fen"],
                row["party_id"],
                row["transfer_group_id"],
                tuple(row_ids),
            )
        )
    return _movement_selection(queries, tuple(scope_versions), tuple(items), read)


def merge_movements(selections, read):
    selections = _selections(selections, ReviewMovementSelection, read)
    queries, scope_versions = _merged_scopes(selections)
    items = {item.movement_id: item for selection in selections for item in selection.items}
    ordered = tuple(sorted(items.values(), key=lambda item: (item.actual_on, item.movement_id)))
    return _movement_selection(queries, scope_versions, ordered, read)


def _movement_selection(queries, scope_versions, items, read):
    return ReviewMovementSelection(
        queries,
        version(
            read,
            "review_movements",
            [asdict(current) for current in queries],
            _CURRENT,
            scope_versions,
        ),
        items,
        scope_versions,
        _snapshot(read),
    )


def movement_page(selection, page, read):
    _selection(selection, ReviewMovementSelection, read)
    after = after_key(page, selection.version, (str, str))
    chosen = [
        item
        for item in selection.items
        if after is None or (item.actual_on, item.movement_id) > tuple(after)
    ][: page.limit + 1]
    more, chosen = len(chosen) > page.limit, chosen[: page.limit]
    repo = read.repository("ledger")
    rows = repo.movements([item.movement_id for item in chosen]) if chosen else []
    mapped = {row["movement_id"]: row for row in rows}
    values = []
    for item in chosen:
        row = mapped.get(item.movement_id)
        if row is None or row["result_id"] != item.source["result_id"]:
            fail("storage_corrupt", "资金核查正文缺少对应的正式发生。")
        values.append(resource_definition("funds", item.result_reference, row, item.source))
    return ReadPage(
        selection.version,
        selection.total_count,
        selection.total_count,
        tuple(values),
        cursor(selection.version, (chosen[-1].actual_on, chosen[-1].movement_id)) if more else None,
    )


def source_metadata(sources, read):
    headers = _read_contents.get_adopted_sources(sources, read)
    rows = read.repository("ledger").read_review_source_locations(
        [item.reference["result_id"] for item in headers]
    )
    locations = {item.reference["result_id"]: [] for item in headers}
    for row in rows:
        locations[row["result_id"]].append(
            {
                "source_id": row["source_id"],
                **({"location": row["location"]} if row["location"] else {}),
            }
        )
    validate_contract_items(
        "business-facts.schema.json",
        "SourceLocation",
        [value for group in locations.values() for value in group],
    )
    return tuple(
        ReviewSourceMetadata(
            item.reference, item.source, tuple(locations[item.reference["result_id"]])
        )
        for item in headers
    )
