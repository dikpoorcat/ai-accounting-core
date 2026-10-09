"""Shared A-Z pinyin order for user-facing Chinese text."""

from functools import lru_cache

from pypinyin import Style, lazy_pinyin


@lru_cache(maxsize=4096)
def pinyin_key(text: str) -> tuple[str, str]:
    """Ignore tones and case; retain original text as a stable homophone tie."""
    return "".join(lazy_pinyin(text, style=Style.NORMAL)).casefold(), text
