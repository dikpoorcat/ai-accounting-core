"""Fixed, deterministic negative-key filter for close-bound private roots.

An absent answer is useful only after the containing root has been authenticated.
Possible hits must still use their original exact source-directory lookup.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
from collections.abc import Iterable
from dataclasses import dataclass

FORMAT = "ai-accounting-kernel/2/key-membership-filter/1"
BITS_PER_KEY = 12
HASH_COUNT = 8


@dataclass(frozen=True)
class DecodedKeysFilter:
    key_count: int
    bit_count: int
    bits: bytes


def _positions(key: str, bit_count: int):
    if type(key) is not str:
        raise ValueError("filter key must be a string")
    raw = hashlib.sha256(key.encode("utf-8")).digest()
    first = int.from_bytes(raw[:16], "big")
    step = int.from_bytes(raw[16:], "big") | 1
    return ((first + index * step) % bit_count for index in range(HASH_COUNT))


def build_keys_filter(keys: Iterable[str]) -> dict[str, object]:
    """Build from the complete unique key set of one authoritative source cut."""
    unique = set(keys)
    bit_count = max(8, len(unique) * BITS_PER_KEY)
    bits = bytearray((bit_count + 7) // 8)
    for key in unique:
        for position in _positions(key, bit_count):
            bits[position >> 3] |= 1 << (position & 7)
    return {
        "format": FORMAT,
        "key_count": len(unique),
        "bit_count": bit_count,
        "hash_count": HASH_COUNT,
        "bits_base64": base64.b64encode(bits).decode("ascii"),
    }


def decode_keys_filter(value: object) -> DecodedKeysFilter:
    """Reject unknown or noncanonical wire values before any negative answer."""
    if not isinstance(value, dict) or set(value) != {
        "format", "key_count", "bit_count", "hash_count", "bits_base64"
    }:
        raise ValueError("filter shape invalid")
    key_count = value["key_count"]
    bit_count = value["bit_count"]
    encoded = value["bits_base64"]
    if (
        value["format"] != FORMAT
        or type(key_count) is not int
        or key_count < 0
        or type(bit_count) is not int
        or bit_count != max(8, key_count * BITS_PER_KEY)
        or type(value["hash_count"]) is not int
        or value["hash_count"] != HASH_COUNT
        or type(encoded) is not str
        or len(encoded) != 4 * (((bit_count + 7) // 8 + 2) // 3)
    ):
        raise ValueError("filter fields invalid")
    try:
        bits = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error) as exc:
        raise ValueError("filter base64 invalid") from exc
    if len(bits) != (bit_count + 7) // 8 or base64.b64encode(bits).decode("ascii") != encoded:
        raise ValueError("filter bytes invalid")
    if bit_count % 8 and bits[-1] & (0xFF << (bit_count % 8)):
        raise ValueError("filter unused bits not zero")
    if key_count == 0 and any(bits):
        raise ValueError("empty filter has set bits")
    return DecodedKeysFilter(key_count, bit_count, bits)


def may_contain(value: DecodedKeysFilter, key: str) -> bool:
    """False proves absence only under an authenticated, fully rebuilt root."""
    if not isinstance(value, DecodedKeysFilter):
        raise ValueError("decoded filter required")
    for position in _positions(key, value.bit_count):
        if not value.bits[position >> 3] & (1 << (position & 7)):
            return False
    return True
