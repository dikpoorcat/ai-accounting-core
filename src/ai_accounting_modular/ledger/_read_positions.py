"""Complete selected resource quantities precede filtering and page selection."""

from collections import defaultdict
from dataclasses import asdict, fields
from sqlite3 import OperationalError

from ai_accounting_modular.contracts.public import ReadPage, query_selection, validate_contract

from ._balances import obligation_remaining, sum_amounts
from ._planning import stored_source_reference
from ._read_support import after_key, bounded, cursor, version
from ._resource_view import resource_definition
from ._types import (
    CostPositionQuery,
    ObligationPositionQuery,
    PositionAmounts,
    PositionPage,
    PositionTotal,
    ResourcePosition,
)
from ._values import fail

_AMOUNTS = tuple(field.name for field in fields(PositionAmounts))


def _amounts(row, kind):
    totals = {key: row[key] for key in ("money", "agency", "offset")}
    totals.update(transfer=row["transferred"], reclassify_excess=row["excess"])
    remaining = (
        obligation_remaining(row["amount_fen"], totals)
        if kind == "obligation"
        else sum_amounts((row["amount_fen"], -row["consumed"]))
    )
    return PositionAmounts(
        row["amount_fen"],
        row["money"],
        row["agency"],
        row["offset"],
        row["transferred"],
        row["excess"],
        row["consumed"],
        remaining,
    )


def _totals(rows):
    groups = defaultdict(list)
    for row, amounts in rows:
        groups[row.get("direction"), row["category"]].append(amounts)
    return tuple(
        PositionTotal(
            direction,
            category,
            len(values),
            PositionAmounts(
                **{name: sum_amounts(getattr(value, name) for value in values) for name in _AMOUNTS}
            ),
        )
        for (direction, category), values in sorted(
            groups.items(), key=lambda item: (item[0][0] or "", item[0][1])
        )
    )


def get_positions(cutoff, read, selection=None, *, kind, business_ids=(), references=()):
    """Read only known keys; omitted or unselected resources do not become zero."""
    validate_contract("core.schema.json", "Month", cutoff)
    business_ids = bounded(business_ids)
    references = bounded(references, "BusinessRef")
    selected = query_selection(read, selection)
    if not business_ids and not references:
        return ()
    try:
        rows = read.repository("ledger").read_position_rows(
            kind, cutoff, selected, business_ids=business_ids, references=references
        )
    except OperationalError as error:
        if "integer overflow" in str(error):
            fail("invalid_input", "指定资源使用金额超出64位整数分范围。")
        raise
    return tuple(
        ResourcePosition(
            resource_definition(kind, row, row, stored_source_reference(row)), _amounts(row, kind)
        )
        for row in rows
    )


def query_positions(query, page, read, selection=None, *, kind, economic_from=None):
    expected = ObligationPositionQuery if kind == "obligation" else CostPositionQuery
    if not isinstance(query, expected):
        fail("invalid_input", "资源位置须使用对应的明确查询条件。")
    validate_contract("core.schema.json", "Month", query.cutoff_period)
    bounded(query.business_ids)
    bounded(query.categories, "text")
    if kind == "obligation":
        bounded(query.party_ids)
        if (
            query.direction not in (None, "receivable", "payable")
            or type(query.include_settled) is not bool
        ):
            fail("invalid_input", "应收应付方向或已清偿筛选无效。")
    else:
        bounded(query.object_ids)
        if type(query.include_consumed) is not bool:
            fail("invalid_input", "成本来源须明确是否包含已消耗完的项目。")
    selected = query_selection(read, selection)
    repo = read.repository("ledger")
    try:
        raw = repo.read_position_rows(kind, query.cutoff_period, selected)
    except OperationalError as error:
        if "integer overflow" in str(error):
            fail("invalid_input", "完整资源使用金额超出64位整数分范围。")
        raise
    rows = [(row, _amounts(row, kind)) for row in raw]
    filtered = []
    for row, amounts in rows:
        if economic_from is not None and row["owner_business_period"] < economic_from:
            continue
        if query.business_ids and row["business_id"] not in query.business_ids:
            continue
        if query.categories and row["category"] not in query.categories:
            continue
        if kind == "obligation":
            if query.party_ids and row["party_id"] not in query.party_ids:
                continue
            if query.direction is not None and row["direction"] != query.direction:
                continue
            if not query.include_settled and amounts.remaining_fen == 0:
                continue
        else:
            if query.object_ids and row["object_id"] not in query.object_ids:
                continue
            if not query.include_consumed and amounts.remaining_fen == 0:
                continue
        filtered.append((row, amounts))
    identity = version(
        read, kind + "_positions", {**asdict(query), "economic_from": economic_from}, selected, raw
    )
    after = after_key(page, identity, (str, str))
    picked = [
        item
        for item in filtered
        if after is None or (item[0]["business_id"], item[0]["public_slot"]) > tuple(after)
    ][: page.limit + 1]
    more, picked = len(picked) > page.limit, picked[: page.limit]
    items = tuple(
        ResourcePosition(resource_definition(kind, row, row, stored_source_reference(row)), amounts)
        for row, amounts in picked
    )
    next_cursor = (
        cursor(identity, (picked[-1][0]["business_id"], picked[-1][0]["public_slot"]))
        if more
        else None
    )
    return PositionPage(
        ReadPage(identity, len(rows), len(filtered), items, next_cursor),
        _totals(rows),
        _totals(filtered),
    )
