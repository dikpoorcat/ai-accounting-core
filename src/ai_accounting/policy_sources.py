"""Shared validation for primary official policy source URLs."""

from __future__ import annotations

import re
import unicodedata
from typing import Annotated
from urllib.parse import urlsplit

from pydantic import AfterValidator


def validate_official_policy_source_url(value: str) -> str:
    """Require an official HTTPS gov.cn URL without rewriting the supplied text."""

    if not isinstance(value, str):
        raise ValueError("policy source must be a string")
    if any(
        character.isspace() or unicodedata.category(character) in {"Cc", "Cf", "Cs"}
        for character in value
    ):
        raise ValueError("policy source must not contain whitespace or control characters")
    if "\\" in value:
        raise ValueError("policy source must not contain backslashes")
    if any(character in '<>"{}|^`' for character in value):
        raise ValueError("policy source contains an illegal URL character")
    if re.search(r"%(?![0-9a-fA-F]{2})", value):
        raise ValueError("policy source contains an invalid percent escape")
    try:
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").lower()
        # Accessing port also rejects malformed bracketed hosts and invalid ports.
        _port = parsed.port
    except ValueError as exc:
        raise ValueError("policy source must be a valid URL") from exc
    labels = hostname.split(".")
    if len(hostname) > 253 or any(
        re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label) is None for label in labels
    ):
        raise ValueError("policy source must contain a valid DNS hostname")
    if parsed.scheme != "https" or not (hostname == "gov.cn" or hostname.endswith(".gov.cn")):
        raise ValueError("a primary official HTTPS gov.cn policy source is required")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("policy source must not contain credentials")
    return value


OfficialPolicySourceURL = Annotated[str, AfterValidator(validate_official_policy_source_url)]
