from __future__ import annotations

import re

_UNIFIED_SOCIAL_CREDIT_CODE_ALPHABET = "0123456789ABCDEFGHJKLMNPQRTUWXY"
_UNIFIED_SOCIAL_CREDIT_CODE_WEIGHTS = (
    1,
    3,
    9,
    27,
    19,
    26,
    16,
    17,
    20,
    29,
    25,
    13,
    8,
    24,
    10,
    30,
    28,
)
_UNIFIED_SOCIAL_CREDIT_CODE_PATTERN = re.compile(
    rf"[{_UNIFIED_SOCIAL_CREDIT_CODE_ALPHABET}]{{18}}"
)


def normalize_taxpayer_identification_number(value: str) -> str:
    """Normalize and validate a Chinese unified social credit code."""

    normalized = value.strip().upper()
    if _UNIFIED_SOCIAL_CREDIT_CODE_PATTERN.fullmatch(normalized) is None:
        raise ValueError("INVALID_TAXPAYER_IDENTIFICATION_NUMBER")

    weighted_sum = sum(
        _UNIFIED_SOCIAL_CREDIT_CODE_ALPHABET.index(character) * weight
        for character, weight in zip(
            normalized[:17], _UNIFIED_SOCIAL_CREDIT_CODE_WEIGHTS, strict=True
        )
    )
    expected_check_character = _UNIFIED_SOCIAL_CREDIT_CODE_ALPHABET[
        (31 - weighted_sum % 31) % 31
    ]
    if normalized[-1] != expected_check_character:
        raise ValueError("INVALID_TAXPAYER_IDENTIFICATION_NUMBER")
    return normalized
