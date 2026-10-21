"""One local owner, transient login and native-window requests; no approvals or roles."""

from __future__ import annotations

import hashlib
import secrets
import threading
import time
from dataclasses import dataclass, field
from subprocess import Popen, TimeoutExpired

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError

from ai_accounting_modular.contracts.public import ContractError

_KINDS = {"login", "change_password", "recover", "replace_recovery_code"}
_TERMINAL = {"succeeded", "failed", "cancelled", "expired"}
_HASHER = PasswordHasher()


def password_hash(password: str) -> str:
    if not isinstance(password, str) or not 6 <= len(password) <= 128 or "\x00" in password:
        raise ContractError("IDENTITY_PASSWORD_POLICY_REJECTED", "密码须为6至128个字符。")
    return _HASHER.hash(password)


def recovery_hash(value: str) -> str:
    if not isinstance(value, str) or not value or len(value) > 200:
        raise ContractError("IDENTITY_RECOVERY_FAILED", "恢复码不正确。")
    return hashlib.sha256(value.encode()).hexdigest()


def new_credentials(password: str) -> tuple[str, str, str]:
    code = secrets.token_urlsafe(24)
    return password_hash(password), recovery_hash(code), code


@dataclass
class _Window:
    kind: str
    created: float
    status: str = "starting"
    committed: bool = False
    acknowledged: bool = False
    login_completed: bool = False
    error: str | None = None
    pending_recovery: bool = False
    process: Popen | None = field(default=None, repr=False)


class Security:
    def __init__(self, storage, *, window_opener=None, clock=time.monotonic):
        self.storage = storage
        self.catalog_id = storage.catalog_identity()
        self.window_opener = window_opener
        self.clock = clock
        self._lock = threading.RLock()
        self._windows: dict[str, _Window] = {}
        self._session: str | None = None
        self._credentials = None
        self._failures = 0
        self._blocked_until = 0.0

    def _active(self):
        if self._credentials is not None and self.storage.owner_profile() != self._credentials:
            self._session, self._credentials = None, None
        return self._session is not None

    def authorize(self, token=None, *, local=False):
        with self._lock:
            if (
                not self._active()
                or not local
                and (not isinstance(token, str) or not secrets.compare_digest(token, self._session))
            ):
                raise ContractError("owner_session_required", "请先在本机窗口登录负责人。")

    def token(self):
        with self._lock:
            self.authorize(local=True)
            return self._session

    def status(self, token=None, *, local=False):
        with self._lock:
            owner = self.storage.owner_profile()
            active = self._active()
            return {
                "schema_version": 1,
                "catalog_instance_id": self.catalog_id,
                "provisioned": True,
                "login_name": owner.display_name,
                "active": True,
                "authenticated": active and (local or token == self._session),
                **({"owner_id": owner.owner_id} if active else {}),
            }

    def _record(self, request_id):
        if not isinstance(request_id, str) or request_id not in self._windows:
            raise ContractError("OWNER_SECURITY_REQUEST_UNKNOWN", "本机窗口请求不存在。")
        window = self._windows[request_id]
        if window.status not in _TERMINAL and self.clock() - window.created >= 1800:
            window.status = "expired"
        return window

    def _public(self, request_id, window):
        return {
            "schema_version": 1,
            "request_id": request_id,
            "kind": window.kind,
            "status": window.status,
            "catalog_instance_id": self.catalog_id,
            "error_code": window.error,
            "operation_committed": window.committed,
            "login_completed": window.login_completed,
            "recovery_code_acknowledged": window.acknowledged,
        }

    def request(self, payload):
        if not isinstance(payload, dict) or set(payload) != {"kind"}:
            raise ContractError("invalid_input", "本机窗口请求只接受操作种类。")
        kind = payload["kind"]
        if not isinstance(kind, str) or kind not in _KINDS:
            raise ContractError("unsupported_business", "没有此本机身份操作。")
        with self._lock:
            if kind in {"change_password", "replace_recovery_code"}:
                self.authorize(local=True)
            for request_id in tuple(self._windows):
                existing = self._record(request_id)
                if existing.status not in _TERMINAL:
                    if existing.kind == kind:
                        return self._public(request_id, existing)
                    raise ContractError("OWNER_SECURITY_WINDOW_BUSY", "请先完成已打开的本机窗口。")
            if self.window_opener is None:
                raise ContractError(
                    "OWNER_SECURITY_WINDOW_UNAVAILABLE", "当前环境无法打开本机密码窗口。"
                )
            request_id = secrets.token_hex(16)
            window = _Window(kind, self.clock())
            self._windows[request_id] = window
            try:
                window.process = self.window_opener(request_id)
            except Exception:
                window.status, window.error = "failed", "OWNER_SECURITY_WINDOW_UNAVAILABLE"
                raise ContractError(window.error, "本机密码窗口未能打开。") from None
            for old in tuple(self._windows)[:-128]:
                if self._windows[old].status in _TERMINAL:
                    del self._windows[old]
            return self._public(request_id, window)

    def public(self, operation, payload, *, token=None, local=False):
        if operation == "session_status" and payload == {}:
            return self.status(token, local=local)
        if operation == "request":
            return self.request(payload)
        if (
            not isinstance(operation, str)
            or operation not in {"status", "cancel"}
            or not isinstance(payload, dict)
            or set(payload) != {"request_id"}
        ):
            raise ContractError("invalid_input", "本机窗口命令内容不正确。")
        with self._lock:
            request_id = payload["request_id"]
            window = self._record(request_id)
            if operation == "cancel" and window.status not in _TERMINAL:
                window.status = "failed" if window.committed else "cancelled"
                if window.pending_recovery:
                    window.error = "OWNER_SECURITY_RECOVERY_CODE_NOT_ACKNOWLEDGED"
            return self._public(request_id, window)

    def _check_password(self, owner, password):
        if self.clock() < self._blocked_until:
            raise ContractError("IDENTITY_AUTHENTICATION_FAILED", "尝试过于频繁，请稍后重试。")
        try:
            if not isinstance(password, str) or len(password) > 128:
                raise VerificationError
            _HASHER.verify(owner.password_hash, password)
        except (VerificationError, InvalidHashError):
            self._failed()
        self._failures = 0

    def _failed(self):
        self._failures += 1
        if self._failures >= 5:
            self._blocked_until = self.clock() + 60
        raise ContractError("IDENTITY_AUTHENTICATION_FAILED", "密码或恢复码不正确，请核对后重试。")

    def native(self, operation, payload):
        """This method is reachable only from the private local capability channel."""
        with self._lock:
            if not isinstance(payload, dict) or "request_id" not in payload:
                raise ContractError("invalid_input", "缺少本机窗口身份。")
            request_id = payload["request_id"]
            window = self._record(request_id)
            if window.status in _TERMINAL:
                raise ContractError("OWNER_SECURITY_REQUEST_FINISHED", "此本机窗口已经结束。")
            owner = self.storage.owner_profile()
            if operation == "unavailable" and set(payload) == {"request_id"}:
                window.status = "failed"
                window.error = "OWNER_SECURITY_WINDOW_UNAVAILABLE"
                return self._public(request_id, window)
            if operation == "inspect" and set(payload) == {"request_id"}:
                window.status = "waiting_for_user"
                return self._public(request_id, window) | {"display_name": owner.display_name}
            if operation == "acknowledge" and set(payload) == {"request_id"}:
                if not window.committed or not window.pending_recovery:
                    raise ContractError("invalid_input", "没有等待保存的恢复码。")
                window.acknowledged, window.pending_recovery = True, False
                window.status = "succeeded"
                return self._public(request_id, window)
            if operation != "execute" or window.committed:
                raise ContractError("invalid_input", "本机窗口操作不正确或已完成。")
            fields = {
                "login": {"password"},
                "change_password": {"password", "new_password", "repeat_password"},
                "recover": {"recovery_code", "new_password", "repeat_password"},
                "replace_recovery_code": set(),
            }[window.kind] | {"request_id"}
            if set(payload) != fields:
                raise ContractError("invalid_input", "本机窗口含无关字段。")
            window.status = "running"
            if window.kind in {"login", "change_password"}:
                self._check_password(owner, payload.get("password"))
            if window.kind in {"change_password", "replace_recovery_code"}:
                self.authorize(local=True)
            if window.kind == "recover":
                if (
                    self.clock() < self._blocked_until
                    or not owner.recovery_hash
                    or not secrets.compare_digest(
                        recovery_hash(payload.get("recovery_code")), owner.recovery_hash
                    )
                ):
                    self._failed()
            code = None
            if window.kind == "login":
                self._credentials, self._session = owner, secrets.token_urlsafe(32)
                window.login_completed = True
            else:
                if window.kind == "replace_recovery_code":
                    password_digest = owner.password_hash
                    code = secrets.token_urlsafe(24)
                    recovery_digest = recovery_hash(code)
                else:
                    if payload.get("new_password") != payload.get("repeat_password"):
                        raise ContractError(
                            "IDENTITY_PASSWORD_CONFIRMATION_MISMATCH", "两次输入的新密码不一致。"
                        )
                    password_digest, recovery_digest, code = new_credentials(
                        payload.get("new_password")
                    )
                updated = self.storage.replace_owner_credentials(
                    owner, password_hash=password_digest, recovery_hash=recovery_digest
                )
                if window.kind == "replace_recovery_code":
                    self._credentials = updated
                else:
                    self._session, self._credentials = None, None
                window.pending_recovery = True
            window.committed = True
            window.status = "running" if code else "succeeded"
            return self._public(request_id, window) | {"recovery_code": code}

    def close(self):
        """Native children must not outlive the resident that handles their passwords."""
        with self._lock:
            children = [
                window.process
                for window in self._windows.values()
                if window.process is not None and window.process.poll() is None
            ]
            for process in children:
                process.terminate()
            for process in children:
                try:
                    process.wait(timeout=5)
                except TimeoutExpired:
                    process.kill()
                    process.wait()
