"""Fixed JSON and integer encoding used by released v1 source proofs."""

from __future__ import annotations

import hashlib
import json


def canonical(value):
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).digest()


def checked(value):
    if type(value) is not int or not -(2**63) <= value <= 2**63 - 1:
        raise ValueError("amount must be a signed 64-bit integer number of fen")
    return value


def sum_fen(values):
    result = 0
    for value in values:
        result = checked(result + checked(value))
    return result
