"""Keep exact digest encoding and bounded Python work at consumed directories."""

import random
import sys

import pytest

from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.settlement_freeze import _validate_directory
from ai_accounting.kernel.types import is_sha256_hex


def test_sha256_text_acceptance_preserves_lowercase_ascii_contract():
    class DigestText(str):
        pass

    values = [
        None, False, True, 64, b"a" * 64, DigestText("a" * 64), "",
        "a" * 63, "a" * 65, "A" * 64, "０" * 64, "ａ" * 64,
        "a" * 63 + "\n", "a" * 63 + "\x00", "a" * 63 + "\ud800",
        "0123456789abcdef" * 4,
    ]
    generator = random.Random(91)
    alphabet = "0123456789abcdefABCDEFgＧ\n\r\x00\ud800"
    values.extend(
        "".join(generator.choices(alphabet, k=generator.randrange(60, 69)))
        for _ in range(5000)
    )
    for value in values:
        expected = isinstance(value, str) and len(value) == 64 and all(
            char in "0123456789abcdef" for char in value
        )
        assert is_sha256_hex(value) is expected


@pytest.mark.parametrize("size", [12, 120, 1200, 12000])
@pytest.mark.parametrize("damaged", [False, True])
def test_directory_format_work_scales_with_headers_not_digest_characters(size, damaged):
    # Ordered, distinct obligations require every header to be checked. Extra
    # history must not introduce 64 Python callback invocations per digest.
    entries = [[i, f"key-{i:06d}", f"key-{i:06d}", 1, "ab" * 32]
               for i in range(size)]
    if damaged:
        entries[-1][4] = "ab" * 31 + "AG"
    calls = 0

    def count_calls(_frame, event, _arg):
        nonlocal calls
        calls += event == "call"

    old_profile = sys.getprofile()
    sys.setprofile(count_calls)
    try:
        if damaged:
            with pytest.raises(KernelError) as failure:
                _validate_directory(entries, 24300)
            assert failure.value.code == "content_integrity_failed"
        else:
            _validate_directory(entries, 24300)
    finally:
        sys.setprofile(old_profile)
    assert calls < size * 8 + 150
