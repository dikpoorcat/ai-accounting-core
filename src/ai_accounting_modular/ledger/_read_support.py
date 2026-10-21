"""Validation and opaque keyset cursors for ledger-owned read collections."""

import base64
from dataclasses import asdict

from ai_accounting_modular.contracts.public import (
    PageQuery,
    QuerySelection,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from ._values import fail


def bounded(values, definition="Id", *, schema="core.schema.json"):
    if not isinstance(values, tuple | list) or len(values) > 500:
        fail("invalid_input", "查询条件必须是最多500项的有界列表。")
    for value in values:
        if definition == "text":
            if not isinstance(value, str) or not value:
                fail("invalid_input", "查询类别须为非空字符串。")
        else:
            validate_contract(schema, definition, value)
    if len({canonical_json(value) for value in values}) != len(values):
        fail("invalid_input", "查询条件不能重复。")
    return tuple(values)


def scope_selection(scope, read):
    validate_contract("core.schema.json", "LedgerScope", scope)
    if scope["company_id"] != read.company_id or scope["period_from"] > scope["period_to"]:
        fail("invalid_input", "读取范围必须属于同一公司且起月不晚于末月。")
    if "object_ids" in scope:
        fail("invalid_input", "该正式目录以明确业务或结果槽筛选，不接受对象范围替代。")
    if scope["view"] == "current" and "as_of" in scope:
        fail("invalid_input", "当前完整目标查询不接受历史采用截止月。")
    selected = QuerySelection(
        scope["view"],
        scope.get("as_of", scope["period_to"]) if scope["view"] == "as_posted" else None,
    )
    return selected


def version(read, collection, query, selected, identities):
    return content_digest(
        json_value(
            {
                "company_id": read.company_id,
                "database_id": read.database_id,
                "collection": collection,
                "query": query,
                "selection": asdict(selected),
                "identities": identities,
            }
        )
    )


def after_key(page, identity, key_types):
    if not isinstance(page, PageQuery):
        fail("invalid_input", "查询须提供明确的分页条件。")
    if page.expected_version is not None and page.expected_version != identity:
        fail("snapshot_changed", "读取范围已经变化，请从第一页读取。")
    if page.cursor is None:
        return None
    try:
        value = loads_json(base64.b64decode(page.cursor, altchars=b"-_", validate=True).decode())
        if type(value) is not dict or set(value) != {"version", "key"}:
            raise ValueError
        if not isinstance(value["key"], list) or len(value["key"]) != len(key_types):
            raise ValueError
        if any(
            type(item) is not expected
            for item, expected in zip(value["key"], key_types, strict=True)
        ):
            raise ValueError
    except (ValueError, TypeError, UnicodeError):
        fail("invalid_input", "分页游标无效。")
    if value["version"] != identity:
        fail("snapshot_changed", "读取范围已经变化，请从第一页读取。")
    return value["key"]


def cursor(identity, key):
    return base64.urlsafe_b64encode(
        canonical_json({"version": identity, "key": list(key)}).encode()
    ).decode()


def json_value(value):
    if isinstance(value, tuple | list):
        return [json_value(item) for item in value]
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    return value
