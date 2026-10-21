"""Selected open obligations and their precise current matches, without wide-page promises."""

from sqlite3 import OperationalError

from ai_accounting_modular.contracts.public import (
    QuerySelection,
    validate_contract,
    validate_contract_items,
)

from ._read_origin_headers import checked_items, source_headers
from ._read_positions import _amounts
from ._types import (
    ObligationDirectory,
    ObligationDirectoryItem,
    ObligationDirectoryQuery,
)
from ._values import fail

_RESOURCE_FIELDS = ("direction", "category", "business_period", "party_id")


def _query(query):
    if not isinstance(query, ObligationDirectoryQuery):
        fail("invalid_input", "轻往来目录须提供明确的查询条件。")
    validate_contract("core.schema.json", "Month", query.cutoff_period)
    validate_contract("core.schema.json", "Month", query.current_cutoff_period)
    if not isinstance(query.selected_view, str) or query.selected_view not in {
        "as_posted",
        "current",
    }:
        fail("invalid_input", "轻往来目录须选择当时入账或当前采用口径。")
    if type(query.include_origins) is not bool:
        fail("invalid_input", "轻往来目录须明确是否读取来源字段。")
    if query.business_ids is not None:
        if not isinstance(query.business_ids, tuple):
            fail("invalid_input", "业务范围须为明确的身份元组，None表示全部业务。")
        validate_contract_items("core.schema.json", "Id", list(query.business_ids))
        if len(set(query.business_ids)) != len(query.business_ids):
            fail("invalid_input", "业务范围不能包含重复身份。")
    if not isinstance(query.categories, tuple):
        fail("invalid_input", "往来类别须为明确的类别元组。")
    validate_contract_items(
        "core.schema.json",
        "Obligation",
        [{"category": item} for item in query.categories],
        fields=("category",),
    )
    if len(set(query.categories)) != len(query.categories):
        fail("invalid_input", "往来类别不能重复。")
    if query.direction is not None:
        validate_contract_items(
            "core.schema.json",
            "Obligation",
            [{"direction": query.direction}],
            fields=("direction",),
        )


def _positions(repo, query):
    month, current_month = query.cutoff_period, query.current_cutoff_period
    same = month == current_month and (
        query.selected_view == "current" or not repo.has_adoptions_after(month)
    )
    # Current selections at one economic cutoff are identical. Posted selections
    # coincide only without later adoptions: frozen retirement writes a formal
    # withdrawal, and an open replacement has only its current generation.
    selection = (
        QuerySelection("current")
        if same or query.selected_view == "current"
        else QuerySelection("as_posted", month)
    )
    rows = repo.read_obligation_directory_rows(
        month,
        selection,
        business_ids=query.business_ids or (),
        categories=query.categories,
        direction=query.direction,
    )
    selected = [(row, _amounts(row, "obligation")) for row in rows]
    selected = [(row, amount) for row, amount in selected if amount.remaining_fen != 0]
    if same or not selected:
        return selected, selected if same else [], same
    references = tuple(
        {"business_id": row["business_id"], "slot": row["public_slot"]} for row, _ in selected
    )
    current = repo.read_obligation_directory_rows(
        current_month, QuerySelection("current"), references=references
    )
    return selected, [(row, _amounts(row, "obligation")) for row in current], False


def capture(query, read):
    _query(query)
    if query.business_ids == ():
        return ObligationDirectory((), (), (), {} if query.include_origins else None)
    try:
        selected, current, same = _positions(read.repository("ledger"), query)
    except OperationalError as error:
        if "integer overflow" in str(error):
            fail("invalid_input", "所选往来资源的清偿金额超出64位整数分范围。")
        raise
    all_rows = selected if same else (*selected, *current)
    sources, headers = source_headers(
        row | {"business_period": row["owner_business_period"]} for row, _ in all_rows
    )
    by_source = {entry.source["result_id"]: entry.source for entry in sources}
    definitions, signatures = {}, {}
    for row, _ in all_rows:
        key = row["result_id"], row["public_slot"]
        value = {name: row[name] for name in _RESOURCE_FIELDS if row[name] is not None}
        signature = tuple((type(row[name]), row[name]) for name in _RESOURCE_FIELDS)
        if key in signatures and signatures[key] != signature:
            fail("storage_corrupt", "同一正式资源的目录字段相互冲突。")
        if key not in definitions:
            definitions[key], signatures[key] = value, signature
    checked_items(
        "core.schema.json", "Obligation", list(definitions.values()), fields=_RESOURCE_FIELDS
    )
    references = {
        key: {"business_id": by_source[key[0]]["business_id"], "slot": key[1]}
        for key in definitions
    }
    checked_items("core.schema.json", "BusinessRef", list(references.values()))

    def item(row, amounts):
        key = row["result_id"], row["public_slot"]
        value = definitions[key]
        return ObligationDirectoryItem(
            references[key],
            by_source[key[0]],
            value["direction"],
            value["category"],
            value["business_period"],
            value.get("party_id"),
            amounts,
        )

    selected_items = tuple(item(row, amount) for row, amount in selected)
    current_items = selected_items if same else tuple(item(row, amount) for row, amount in current)
    origins = None
    if query.include_origins:
        from ._read_origins import capture as capture_origins

        origins = capture_origins(sources, headers, read)
    return ObligationDirectory(selected_items, current_items, sources, origins)
