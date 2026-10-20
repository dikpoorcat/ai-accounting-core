"""Bounded keyset cursors, tied to the actual company snapshot and search range."""

from __future__ import annotations

import base64
import binascii

from ai_accounting_modular.contracts.public import (
    ContractError,
    ReadContext,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from ._repository import sort_key


def collection_version(query, context: ReadContext) -> str:
    return content_digest(
        {
            "version": 1,
            "company_id": context.company_id,
            "database_id": context.database_id,
            "head": context.head,
            "kind": query["kind"],
            "name_contains": query.get("name_contains"),
        }
    )


def decode_cursor(token: str, query, context: ReadContext):
    try:
        payload = loads_json(base64.b64decode(token, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or set(payload) != {"version", "after"}:
            raise ValueError("invalid cursor fields")
        validate_contract("core.schema.json", "Digest", payload["version"])
        after = payload["after"]
        if (
            not isinstance(after, list)
            or len(after) != 3
            or any(type(value) is not str for value in after)
            or not 1 <= len(after[1]) <= 200
            or after[0] != sort_key(after[1])
        ):
            raise ValueError("invalid cursor position")
        validate_contract("core.schema.json", "Id", after[2])
    except (ContractError, ValueError, TypeError, binascii.Error) as exc:
        raise ContractError("invalid_input", "对象分页游标无效，请重新检索。") from exc
    if payload["version"] != collection_version(query, context):
        raise ContractError("snapshot_changed", "对象范围或公司快照已变化，请从第一页重新检索。")
    return after


def encode_cursor(row, version: str) -> str:
    payload = {"version": version, "after": [row["sort_key"], row["name"], row["object_id"]]}
    return base64.urlsafe_b64encode(canonical_json(payload).encode("utf-8")).decode("ascii")
