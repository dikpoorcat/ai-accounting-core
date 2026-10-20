"""Indexed current-period views, with cursors bound to the selected records only."""

from __future__ import annotations

import base64
import binascii
from types import MappingProxyType

from ai_accounting_modular.contracts.public import (
    ContractError,
    canonical_json,
    content_digest,
    loads_json,
    validate_contract,
)

from ._changes import note_current
from ._types import PeriodIssuesPage, PeriodNotesPage


def _range(first, last, limit):
    for month in (first, last):
        validate_contract("core.schema.json", "Month", month)
    if first > last:
        raise ContractError("invalid_input", "期间起月不能晚于末月。")
    if type(limit) is not int or not 1 <= limit <= 500:
        raise ContractError("invalid_input", "资料分页数量须为1至500。")


def _version(read, first, last, collection, metadata):
    return content_digest(
        {
            "company_id": read.company_id,
            "database_id": read.database_id,
            "period_from": first,
            "period_to": last,
            "collection": collection,
            "metadata": [list(row) for row in metadata],
        }
    )


def _after(cursor, version, first, last, *, issues):
    if cursor is None:
        return None
    try:
        if not isinstance(cursor, str) or not 1 <= len(cursor) <= 2000:
            raise ValueError("cursor_length")
        payload = loads_json(base64.b64decode(cursor, altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or set(payload) != {"version", "after"}:
            raise ValueError("cursor_fields")
        validate_contract("core.schema.json", "Digest", payload["version"])
        after = payload["after"]
        if issues:
            if not isinstance(after, list) or len(after) != 3:
                raise ValueError("issue_position")
            if after[0] is not None:
                validate_contract("core.schema.json", "Month", after[0])
                if not first <= after[0] <= last:
                    raise ValueError("issue_period")
            if not isinstance(after[1], str) or not 1 <= len(after[1]) <= 200:
                raise ValueError("issue_scope")
            validate_contract("core.schema.json", "Id", after[2])
        elif not isinstance(after, str) or not ("period:" + first <= after <= "period:" + last):
            raise ValueError("note_position")
        else:
            validate_contract("core.schema.json", "Month", after.removeprefix("period:"))
    except (ContractError, ValueError, TypeError, binascii.Error) as exc:
        raise ContractError("invalid_input", "资料分页游标无效，请从第一页读取。") from exc
    if payload["version"] != version:
        raise ContractError("snapshot_changed", "所选资料集合已经变化，请从第一页读取。")
    return after


def _cursor(version, after):
    return base64.urlsafe_b64encode(
        canonical_json({"version": version, "after": after}).encode("utf-8")
    ).decode("ascii")


def notes(first, last, read, *, limit, cursor):
    _range(first, last, limit)
    repository = read.repository("records")
    metadata = repository.period_note_metadata(first, last)
    version = _version(read, first, last, "notes", metadata)
    after = _after(cursor, version, first, last, issues=False)
    rows = repository.period_note_rows(first, last, after, limit + 1)
    selected = rows[:limit]
    uses = repository.uses_many("note", tuple(row["scope_key"] for row in selected))
    items = tuple(note_current(row, repository, uses[row["scope_key"]]) for row in selected)
    next_cursor = _cursor(version, items[-1]["scope"]) if len(rows) > limit else None
    return PeriodNotesPage(items, len(metadata), next_cursor)


def issues(first, last, read, *, limit, cursor):
    _range(first, last, limit)
    repository = read.repository("records")
    metadata = repository.period_issue_metadata(first, last)
    version = _version(read, first, last, "issues", metadata)
    after = _after(cursor, version, first, last, issues=True)
    rows, heads = repository.period_issue_rows(first, last, after, limit + 1)
    selected = rows[:limit]
    for item in selected:
        validate_contract("core.schema.json", "CurrentIssue", item)
    next_cursor = (
        _cursor(
            version, [selected[-1].get("period"), selected[-1]["scope"], selected[-1]["issue_id"]]
        )
        if len(rows) > limit
        else None
    )
    scopes = {item["scope"]: heads[item["scope"]] for item in selected}
    return PeriodIssuesPage(tuple(selected), MappingProxyType(scopes), len(metadata), next_cursor)
