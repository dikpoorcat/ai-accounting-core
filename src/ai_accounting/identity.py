"""Local single-owner identity primitives.

This module deliberately has no environment, command-line, logging, MCP, or
database side effects.  It defines the fixed cryptographic and normalisation
rules used by the identity service; callers must never persist the returned
plaintext session token or recovery code.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import hmac
import secrets
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from argon2.low_level import Type

ARGON2_MEMORY_COST_KIB = 65_536
ARGON2_TIME_COST = 3
ARGON2_PARALLELISM = 4
ARGON2_HASH_LEN = 32
ARGON2_SALT_LEN = 16
SESSION_TOKEN_BYTES = 32
RECOVERY_CODE_BYTES = 16

# A small, explicit blocklist catches the passwords an inexperienced owner is
# most likely to choose.  It is checked after NFC normalisation and case-folding
# so visually identical input cannot bypass it.
BLOCKED_PASSWORDS = frozenset(
    {
        "123456789012345",
        "111111111111111",
        "abcdefghijklmno",
        "letmeinletmein1",
        "passwordpassword",
        "qwertyuiopasdfg",
        "welcome12345678",
        "iloveyouiloveyou",
    }
)


class IdentityError(ValueError):
    """A stable, caller-safe identity failure.

    ``code`` is intentionally the only machine-readable detail.  In
    particular, it never contains a password, token, recovery code, account
    name, or exception text from a hashing library.
    """

    def __init__(self, code: str) -> None:
        self.code = code
        super().__init__(code)


class ExecutorKind(StrEnum):
    AI_AGENT = "ai_agent"
    DETERMINISTIC_KERNEL = "deterministic_kernel"
    SYSTEM_JOB = "system_job"


@dataclass(frozen=True, slots=True)
class ExecutorIdentity:
    """Fixed, server-owned executor identity; never supplied by an MCP request."""

    kind: ExecutorKind
    executor_name: str
    executor_version: str

    def __post_init__(self) -> None:
        for value in (self.executor_name, self.executor_version):
            if (
                not value
                or len(value) > 100
                or not value.isascii()
                or any(not (character.isalnum() or character in ".-:_") for character in value)
            ):
                raise IdentityError("IDENTITY_EXECUTOR_INVALID")


@dataclass(frozen=True, slots=True)
class ExecutionContext:
    """Authenticated owner authority and a distinct non-human executor.

    It represents the DEC-015 boundary: the owner session authorizes work, but
    the AI or deterministic system that executes it must remain separately
    attributable.  It is a value object only; persistence and business-MCP
    integration are intentionally outside this module.
    """

    org_id: uuid.UUID
    owner_account_id: uuid.UUID
    owner_session_id: uuid.UUID
    owner_credential_version: int
    executor_kind: ExecutorKind
    executor_name: str
    executor_version: str
    request_correlation_id: uuid.UUID


def normalize_login_name(value: str) -> str:
    """Return the ASCII display form enforced by the database contract."""

    if not isinstance(value, str):
        raise IdentityError("IDENTITY_LOGIN_NAME_INVALID")
    normalized = value.strip()
    if (
        not 3 <= len(normalized) <= 100
        or not normalized.isascii()
        or not normalized[0].isalnum()
        or any(not (character.isalnum() or character in ".-_") for character in normalized)
    ):
        raise IdentityError("IDENTITY_LOGIN_NAME_INVALID")
    return normalized


def normalized_login_key(value: str) -> str:
    """Return the exact lower(trim(login_name)) database lookup key."""

    return normalize_login_name(value).lower()


def normalize_password(value: str) -> str:
    """Apply new-password policy including the explicit weak-password blocklist."""

    normalized = normalize_authentication_password(value)
    if normalized.casefold() in BLOCKED_PASSWORDS:
        raise IdentityError("IDENTITY_PASSWORD_POLICY_REJECTED")
    return normalized


def normalize_authentication_password(value: str) -> str:
    """NFC-normalize existing credentials without applying mutable new-password policy."""

    if not isinstance(value, str):
        raise IdentityError("IDENTITY_PASSWORD_POLICY_REJECTED")
    normalized = unicodedata.normalize("NFC", value)
    if not 15 <= len(normalized) <= 128 or "\x00" in normalized:
        raise IdentityError("IDENTITY_PASSWORD_POLICY_REJECTED")
    return normalized


def validate_password_for_login(*, password: str, login_name: str) -> str:
    """Validate password policy without retaining or echoing the secret."""

    normalized_password = normalize_password(password)
    if normalized_password.casefold() == normalized_login_key(login_name):
        raise IdentityError("IDENTITY_PASSWORD_POLICY_REJECTED")
    return normalized_password


def password_hasher() -> PasswordHasher:
    """Return the only permitted password-hash configuration for this pilot."""

    return PasswordHasher(
        time_cost=ARGON2_TIME_COST,
        memory_cost=ARGON2_MEMORY_COST_KIB,
        parallelism=ARGON2_PARALLELISM,
        hash_len=ARGON2_HASH_LEN,
        salt_len=ARGON2_SALT_LEN,
        type=Type.ID,
    )


def hash_password(password: str) -> str:
    """Hash a previously policy-validated password using Argon2id v19."""

    return password_hasher().hash(password)


def verify_password(*, password_hash: str, password: str) -> bool:
    """Return a boolean for a password check without exposing hash failures."""

    try:
        return password_hasher().verify(password_hash, password)
    except (InvalidHashError, VerificationError):
        return False


def token_sha256(token: str) -> str:
    """Hash an opaque session token before storage or database lookup."""

    return hashlib.sha256(token.encode("ascii")).hexdigest()


def new_session_token(randbytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    """Create an opaque 256-bit session token from an injectable CSPRNG."""

    value = randbytes(SESSION_TOKEN_BYTES)
    if len(value) != SESSION_TOKEN_BYTES:
        raise RuntimeError("identity random source returned an unexpected session length")
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def normalize_recovery_code(value: str) -> str:
    """Canonicalise a grouped Base32 recovery code without storing plaintext."""

    if not isinstance(value, str):
        raise IdentityError("IDENTITY_RECOVERY_CODE_INVALID")
    compact = "".join(character for character in value.upper() if character not in " -")
    if len(compact) != 26 or any(
        character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567" for character in compact
    ):
        raise IdentityError("IDENTITY_RECOVERY_CODE_INVALID")
    try:
        decoded = base64.b32decode(f"{compact}======", casefold=False)
    except (ValueError, binascii.Error) as exc:
        raise IdentityError("IDENTITY_RECOVERY_CODE_INVALID") from exc
    if len(decoded) != RECOVERY_CODE_BYTES:
        raise IdentityError("IDENTITY_RECOVERY_CODE_INVALID")
    return compact


def new_recovery_code(randbytes: Callable[[int], bytes] = secrets.token_bytes) -> str:
    """Create one 128-bit CSPRNG recovery code in owner-readable groups."""

    value = randbytes(RECOVERY_CODE_BYTES)
    if len(value) != RECOVERY_CODE_BYTES:
        raise RuntimeError("identity random source returned an unexpected recovery length")
    compact = base64.b32encode(value).rstrip(b"=").decode("ascii")
    return "-".join(
        (
            compact[:5],
            compact[5:10],
            compact[10:15],
            compact[15:20],
            compact[20:25],
            compact[25:],
        )
    )


def recovery_code_sha256(code: str) -> str:
    """Return the server-side hash of one canonical recovery code."""

    return hashlib.sha256(normalize_recovery_code(code).encode("ascii")).hexdigest()


def recovery_code_matches(*, expected_sha256: str, supplied_code: str) -> bool:
    """Compare the sole persisted recovery-code hash in constant time."""

    try:
        supplied_hash = recovery_code_sha256(supplied_code)
    except IdentityError:
        return False
    return hmac.compare_digest(expected_sha256, supplied_hash)
