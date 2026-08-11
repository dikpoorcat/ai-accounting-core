"""Opaque local session-token storage for DEC-031 B.

The token is intentionally accepted only as an in-memory value from the local
login process.  It is never read from an environment variable, argument, MCP
parameter, configuration file, or log record.
"""

from __future__ import annotations

import ctypes
import sys
from typing import Protocol

from pydantic import SecretStr

from .identity import IdentityError

WINDOWS_CREDENTIAL_TARGET = "ai-accounting-core/local-owner-session/v1"
_CRED_TYPE_GENERIC = 1
_CRED_PERSIST_LOCAL_MACHINE = 2
_ERROR_NOT_FOUND = 1168


class CredentialStore(Protocol):
    """Small testable boundary for the one opaque owner-session token."""

    def save_session_token(self, token: SecretStr) -> None: ...

    def load_session_token(self) -> SecretStr | None: ...

    def delete_session_token(self) -> None: ...


class InMemoryCredentialStore:
    """Test-only store; production code must use WindowsCredentialStore."""

    def __init__(self) -> None:
        self._token: SecretStr | None = None

    def save_session_token(self, token: SecretStr) -> None:
        self._token = SecretStr(token.get_secret_value())

    def load_session_token(self) -> SecretStr | None:
        return self._token

    def delete_session_token(self) -> None:
        self._token = None


class WindowsCredentialStore:
    """Current-Windows-user Credential Manager storage, with no fallback."""

    def __init__(self, *, target_name: str = WINDOWS_CREDENTIAL_TARGET) -> None:
        if sys.platform != "win32":
            raise IdentityError("IDENTITY_CREDENTIAL_STORE_UNAVAILABLE")
        if not target_name or len(target_name) > 512 or "\x00" in target_name:
            raise IdentityError("IDENTITY_CREDENTIAL_STORE_UNAVAILABLE")
        _assert_windows_credential_layout()
        self._target = target_name
        self._advapi32 = ctypes.WinDLL("Advapi32.dll", use_last_error=True)
        self._advapi32.CredWriteW.argtypes = [ctypes.POINTER(_CREDENTIALW), ctypes.c_uint32]
        self._advapi32.CredWriteW.restype = ctypes.c_int
        self._advapi32.CredReadW.argtypes = [
            ctypes.c_wchar_p,
            ctypes.c_uint32,
            ctypes.c_uint32,
            ctypes.POINTER(ctypes.POINTER(_CREDENTIALW)),
        ]
        self._advapi32.CredReadW.restype = ctypes.c_int
        self._advapi32.CredDeleteW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32, ctypes.c_uint32]
        self._advapi32.CredDeleteW.restype = ctypes.c_int
        self._advapi32.CredFree.argtypes = [ctypes.c_void_p]
        self._advapi32.CredFree.restype = None

    def save_session_token(self, token: SecretStr) -> None:
        raw = token.get_secret_value().encode("ascii")
        if not raw or len(raw) > 2_560:
            raise IdentityError("IDENTITY_CREDENTIAL_STORE_WRITE_FAILED")
        blob = (ctypes.c_ubyte * len(raw)).from_buffer_copy(raw)
        credential = _CREDENTIALW()
        credential.Type = _CRED_TYPE_GENERIC
        credential.TargetName = self._target
        credential.CredentialBlobSize = len(raw)
        credential.CredentialBlob = ctypes.cast(blob, ctypes.POINTER(ctypes.c_ubyte))
        credential.Persist = _CRED_PERSIST_LOCAL_MACHINE
        credential.UserName = "local-owner-session"
        if not self._advapi32.CredWriteW(ctypes.byref(credential), 0):
            raise IdentityError("IDENTITY_CREDENTIAL_STORE_WRITE_FAILED")

    def load_session_token(self) -> SecretStr | None:
        pointer = ctypes.POINTER(_CREDENTIALW)()
        if not self._advapi32.CredReadW(
            self._target,
            _CRED_TYPE_GENERIC,
            0,
            ctypes.byref(pointer),
        ):
            error = ctypes.get_last_error()
            if error == _ERROR_NOT_FOUND:
                return None
            raise IdentityError("IDENTITY_CREDENTIAL_STORE_READ_FAILED")
        try:
            size = int(pointer.contents.CredentialBlobSize)
            if size <= 0 or size > 2_560:
                raise IdentityError("IDENTITY_CREDENTIAL_STORE_READ_FAILED")
            raw = ctypes.string_at(pointer.contents.CredentialBlob, size)
            try:
                return SecretStr(raw.decode("ascii"))
            except UnicodeDecodeError as exc:
                raise IdentityError("IDENTITY_CREDENTIAL_STORE_READ_FAILED") from exc
        finally:
            self._advapi32.CredFree(ctypes.cast(pointer, ctypes.c_void_p))
            pointer = ctypes.POINTER(_CREDENTIALW)()

    def delete_session_token(self) -> None:
        if not self._advapi32.CredDeleteW(self._target, _CRED_TYPE_GENERIC, 0):
            error = ctypes.get_last_error()
            if error != _ERROR_NOT_FOUND:  # ERROR_NOT_FOUND makes logout idempotent.
                raise IdentityError("IDENTITY_CREDENTIAL_STORE_DELETE_FAILED")


class _FILETIME(ctypes.Structure):
    _fields_ = [("dwLowDateTime", ctypes.c_uint32), ("dwHighDateTime", ctypes.c_uint32)]


class _CREDENTIALW(ctypes.Structure):
    _fields_ = [
        ("Flags", ctypes.c_uint32),
        ("Type", ctypes.c_uint32),
        ("TargetName", ctypes.c_wchar_p),
        ("Comment", ctypes.c_wchar_p),
        ("LastWritten", _FILETIME),
        ("CredentialBlobSize", ctypes.c_uint32),
        ("CredentialBlob", ctypes.POINTER(ctypes.c_ubyte)),
        ("Persist", ctypes.c_uint32),
        ("AttributeCount", ctypes.c_uint32),
        ("Attributes", ctypes.c_void_p),
        ("TargetAlias", ctypes.c_wchar_p),
        ("UserName", ctypes.c_wchar_p),
    ]


def _assert_windows_credential_layout() -> None:
    pointer_size = ctypes.sizeof(ctypes.c_void_p)
    expected_size = 80 if pointer_size == 8 else 52 if pointer_size == 4 else None
    if expected_size is None or ctypes.sizeof(_CREDENTIALW) != expected_size:
        raise IdentityError("IDENTITY_CREDENTIAL_STORE_UNAVAILABLE")
