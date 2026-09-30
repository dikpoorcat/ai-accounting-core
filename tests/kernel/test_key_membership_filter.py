"""Fixed private negative-filter wire and conservative lookup boundaries."""

import base64
import hashlib

import pytest

from ai_accounting.kernel.key_membership_filter import (
    FORMAT,
    build_keys_filter,
    decode_keys_filter,
    may_contain,
)
from ai_accounting.kernel.types import canonical


def test_filter_is_deterministic_and_never_loses_a_source_key():
    keys = {canonical(["origin", index, "凭据\x00", index * 100]) for index in range(1000)}
    first = build_keys_filter(keys)
    assert first == build_keys_filter(reversed(sorted(keys)))
    assert first["format"] == FORMAT
    assert first["bit_count"] == 12_000
    decoded = decode_keys_filter(first)
    assert all(may_contain(decoded, key) for key in keys)
    assert sum(may_contain(decoded, f"missing-{index}") for index in range(1000)) < 30


def test_empty_filter_has_no_hits():
    wire = build_keys_filter(())
    assert wire["key_count"] == 0
    assert not may_contain(decode_keys_filter(wire), "unrelated")


@pytest.mark.parametrize(
    "change",
    [
        {"format": "future"},
        {"key_count": True},
        {"key_count": -1},
        {"key_count": 2},
        {"bit_count": 13},
        {"hash_count": 7},
        {"bits_base64": "not base64"},
        {"bits_base64": "AA==\n"},
        {"extra": 1},
    ],
)
def test_filter_rejects_bad_wire(change):
    wire = {**build_keys_filter(("a",)), **change}
    with pytest.raises(ValueError):
        decode_keys_filter(wire)


def test_filter_rejects_noncanonical_padding_and_unused_bits():
    wire = build_keys_filter(("a",))
    raw = bytearray(base64.b64decode(wire["bits_base64"]))
    raw[-1] |= 0x80
    with pytest.raises(ValueError):
        decode_keys_filter({**wire, "bits_base64": base64.b64encode(raw).decode("ascii")})
    assert hashlib.sha256(canonical(wire).encode()).digest() != hashlib.sha256(
        canonical({**wire, "bits_base64": "AAAA"}).encode()
    ).digest()


def test_filter_rejects_non_string_keys():
    with pytest.raises(ValueError):
        build_keys_filter(("ok", None))
    with pytest.raises(ValueError):
        may_contain(decode_keys_filter(build_keys_filter(())), None)
