"""Decode persisted JSON without ambiguous object keys.

SQLite JSON1 selects the first duplicate object member, while Python's ordinary
decoder keeps the last. Stored values used by both interpreters must therefore
reject duplicates at every nesting level, including escaped equivalent keys.
"""

from __future__ import annotations

import hashlib
import json


class DuplicateStoredKey(ValueError):
    """A saved JSON object repeats a decoded member name."""


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateStoredKey("duplicate stored JSON object key")
        value[key] = item
    return value


def loads_unique(raw):
    """Preserve normal JSON value semantics while rejecting duplicate keys."""

    return json.loads(raw, object_pairs_hook=_unique_object)


def load_outcome(raw):
    from .contracts import KernelError

    try:
        return loads_unique(raw)
    except DuplicateStoredKey as exc:
        raise KernelError("content_integrity_failed", "已保存的核算结果有重复字段") from exc


def verify_outcome_bytes(raw, expected_digest, ident, *, source_digest=None):
    """Check a stored outcome's exact JSON before a JSON1-derived value is used."""
    from .contracts import KernelError
    from .types import digest

    encoded = raw.encode("utf-8") if isinstance(raw, str) else raw
    try:
        decoded = loads_unique(encoded)
        # This row's digest alone is not an independently authenticated anchor:
        # both it and the body might be damaged together. Always inspect object
        # members before JSON1 interprets the body; the raw-byte match still
        # avoids a second canonical serialization for normal writer output.
        valid = hashlib.sha256(encoded).digest() == expected_digest or (
            source_digest or digest
        )(decoded) == expected_digest
    except (ValueError, TypeError, OverflowError) as exc:
        raise KernelError(
            "content_integrity_failed", "已保存的核算结果 JSON 有重复字段或格式错误",
            component="calculation", record_id=ident,
        ) from exc
    if not valid:
        raise KernelError(
            "content_integrity_failed", "已保存的核算结果摘要不一致",
            component="calculation", record_id=ident,
        )
    return decoded


def verify_sql_outcomes(connection, identifiers, *, source_digest=None):
    """Prove selected saved results before SQLite JSON1 interprets them.

    The writer stores canonical JSON, whose raw SHA is the result digest.
    Equivalent noncanonical JSON retains its existing acceptance only after
    an exact-byte decode that rejects ambiguous object members.
    """
    from .contracts import KernelError
    from .types import canonical

    selected = set(identifiers)
    if not selected:
        return
    actual = set()
    for row in connection.execute(
        "SELECT c.id,c.outcome,c.digest FROM json_each(?) ids "
        "CROSS JOIN calculation c ON c.id=ids.value",
        (canonical(sorted(selected)),),
    ):
        ident = row["id"]
        actual.add(ident)
        verify_outcome_bytes(
            row["outcome"], row["digest"], ident, source_digest=source_digest
        )
    if actual != selected:
        raise KernelError("content_integrity_failed", "已保存的核算结果缺失")
