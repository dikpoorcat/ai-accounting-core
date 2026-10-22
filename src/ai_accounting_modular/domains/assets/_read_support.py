"""One explicit selection and opaque page positions for asset reads."""

import base64
from dataclasses import fields, is_dataclass

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


def selected_scope(scope, read):
    validate_contract("core.schema.json", "LedgerScope", scope)
    if (
        scope["company_id"] != read.company_id
        or scope["period_from"] > scope["period_to"]
        or "object_ids" in scope
    ):
        raise ContractError(
            "invalid_input", "资产范围须属于同一公司并使用有效月份；对象由查询条件指定。"
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
        return {field.name: wire(getattr(value, field.name)) for field in fields(value)}
    if isinstance(value, dict):
        return {key: wire(item) for key, item in value.items()}
    if isinstance(value, tuple | list):
        return [wire(item) for item in value]
    return value


def identity(read, collection, value):
    return content_digest(
        wire(
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "collection": collection,
                "value": value,
            }
        )
    )


def page_values(version, all_items, filtered, page):
    if not isinstance(page, PageQuery):
        raise ContractError("invalid_input", "资产分页须使用共同 PageQuery。")
    if page.expected_version is not None and page.expected_version != version:
        raise ContractError("snapshot_changed", "资产读取范围已变化，请从第一页读取。")
    offset = 0
    if page.cursor is not None:
        try:
            saved = loads_json(
                base64.b64decode(page.cursor, altchars=b"-_", validate=True).decode()
            )
            if (
                type(saved) is not dict
                or set(saved) != {"version", "offset"}
                or type(saved["offset"]) is not int
                or saved["offset"] <= 0
            ):
                raise ValueError
        except (ValueError, TypeError, UnicodeError, ContractError) as error:
            raise ContractError("invalid_input", "资产分页游标无效。") from error
        if saved["version"] != version:
            raise ContractError("snapshot_changed", "资产分页范围已变化，请从第一页读取。")
        offset = saved["offset"]
        if offset >= len(filtered):
            raise ContractError("invalid_input", "资产分页位置超出完整筛选范围。")
    chosen = tuple(filtered[offset : offset + page.limit])
    more = offset + len(chosen) < len(filtered)
    cursor = (
        base64.urlsafe_b64encode(
            canonical_json({"version": version, "offset": offset + len(chosen)}).encode()
        ).decode()
        if more
        else None
    )
    return ReadPage(version, len(all_items), len(filtered), chosen, cursor)


def directory(ledger, scope, kinds, read, **filters):
    cursor = None
    while True:
        page = ledger.query_adopted_directory(scope, kinds, PageQuery(500, cursor), read, **filters)
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def values(ledger, scope, kinds, read):
    cursor = None
    while True:
        page = ledger.query_adopted_values(scope, kinds, PageQuery(500, cursor), read)
        yield from page.items
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def objects(identity, kind, read):
    query = {"action": "find", "company_id": read.company_id, "kind": kind, "limit": 500}
    while True:
        result = identity.find(query, read)
        yield from result["objects"]
        if not result["page"]["has_more"]:
            return
        query["cursor"] = result["page"]["next_cursor"]
