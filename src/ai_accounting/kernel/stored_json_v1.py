"""Fixed released-v1 stored JSON object-member interpretation."""

from __future__ import annotations

import json


class DuplicateStoredKey(ValueError):
    """A saved v1 JSON object repeats a decoded member name."""


def _unique_object(pairs):
    value = {}
    for key, item in pairs:
        if key in value:
            raise DuplicateStoredKey("duplicate stored JSON object key")
        value[key] = item
    return value


def loads_unique(raw):
    return json.loads(raw, object_pairs_hook=_unique_object)


def load_outcome(raw):
    from .contracts import KernelError

    try:
        return loads_unique(raw)
    except DuplicateStoredKey as exc:
        raise KernelError("content_integrity_failed", "已保存的 v1 核算结果有重复字段") from exc
