"""One company selection and the shared bounded page contract."""

import base64
from dataclasses import asdict, is_dataclass

from ai_accounting_modular.contracts.public import (
    ContractError,
    PageQuery,
    QuerySelection,
    ReadPage,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)


def scope_selection(scope, read):
    validate_contract("core.schema.json", "LedgerScope", scope)
    if (
        scope["company_id"] != read.company_id
        or scope["period_from"] > scope["period_to"]
        or "object_ids" in scope
    ):
        raise ContractError(
            "invalid_input", "资金范围须属于同一公司，起月不晚于末月；账户由查询条件指定。"
        )
    selected = QuerySelection(
        scope["view"],
        scope.get("as_of", scope["period_to"] if scope["view"] == "as_posted" else None),
    )
    normalized = {key: value for key, value in scope.items() if key != "expected_head"}
    if selected.as_of is not None:
        normalized["as_of"] = selected.as_of
    return normalized, selected


def wire(value):
    if is_dataclass(value):
        return wire(asdict(value))
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [wire(item) for item in value]
    return value


def version(scope, read, collection, data):
    normalized, selected = scope_selection(scope, read)
    value = content_digest(
        wire(
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "scope": normalized,
                "selection": selected,
                "collection": collection,
                "data": data,
            }
        )
    )
    if scope.get("expected_head", value) != value:
        raise ContractError("snapshot_changed", "资金范围已经变化，请重新读取。")
    return value


def page_values(identity, values, page, *, filtered=None):
    if not isinstance(page, PageQuery):
        raise ContractError("invalid_input", "资金分页须使用共同 PageQuery。")
    if page.expected_version is not None and page.expected_version != identity:
        raise ContractError("snapshot_changed", "资金查询范围或读取版本已变化，请从第一页读取。")
    filtered = values if filtered is None else filtered
    after = None
    if page.cursor is not None:
        try:
            saved = loads_json(
                base64.urlsafe_b64decode(page.cursor.encode("ascii")).decode("utf-8")
            )
            if set(saved) != {"version", "key"} or not isinstance(saved["key"], list):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, ContractError) as error:
            raise ContractError("invalid_input", "资金分页游标无效。") from error
        if saved["version"] != identity:
            raise ContractError("snapshot_changed", "资金分页范围或版本已变化，请从第一页读取。")
        after = tuple(saved["key"])
        if not any(key == after for key, _ in filtered):
            raise ContractError("snapshot_changed", "资金分页位置已变化，请从第一页读取。")
    chosen = [(key, item) for key, item in filtered if after is None or key > after][
        : page.limit + 1
    ]
    more, chosen = len(chosen) > page.limit, chosen[: page.limit]
    cursor = (
        base64.urlsafe_b64encode(
            canonical_json({"version": identity, "key": list(chosen[-1][0])}).encode("utf-8")
        ).decode("ascii")
        if more
        else None
    )
    return ReadPage(identity, len(values), len(filtered), tuple(item for _, item in chosen), cursor)


def adopted_contents(ledger, scope, kinds, read):
    """Directory metadata may precede the month; only this range's bodies are read."""
    normalized, _ = scope_selection(scope, read)
    cursor = None
    while True:
        page = ledger.query_adopted_directory(normalized, kinds, PageQuery(500, cursor), read)
        refs = [
            item.reference
            for item in page.items
            if scope["period_from"] <= item.reference["business_period"] <= scope["period_to"]
        ]
        if refs:
            yield from ledger.get_adopted_contents(refs, read)
        if page.next_cursor is None:
            return
        cursor = page.next_cursor
