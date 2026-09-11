"""One native security window, owned by the resident local service.

Public operations only open/read/cancel windows. Secret-bearing operations are
available exclusively to the service-capability authenticated native channel.
"""

from __future__ import annotations

import re
import threading
import uuid
from dataclasses import dataclass, field
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, model_validator

from ..types import YearMonth
from .batches import CloseBatchTarget, batch_approval_exists, read_batch_approval
from .credentials import WindowsCredentialStore
from .primitives import IdentityError, normalize_login_name
from .service import SECOND, secret_text

WINDOW_TITLES = {
    "bootstrap_owner": "AI 记账内核 - 首次负责人设置",
    "login": "AI 记账内核 - 负责人登录",
    "approve_period_close": "AI 记账内核 - 关账密码确认",
    "approve_close_batches": "AI 记账内核 - 历史关账批次密码确认",
    "change_password": "AI 记账内核 - 修改负责人密码",
    "recover": "AI 记账内核 - 恢复负责人账号",
    "replace_recovery_code": "AI 记账内核 - 更换恢复码",
}
TERMINAL = {"succeeded", "failed", "cancelled", "expired"}


class NativeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True, hide_input_in_errors=True)
    kind: Literal[
        "bootstrap_owner",
        "login",
        "approve_period_close",
        "approve_close_batches",
        "change_password",
        "recover",
        "replace_recovery_code",
    ]
    login_name: str | None = None
    company_id: str | None = None
    database_id: str | None = None
    period: str | None = None
    calculation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")
    epochs: dict[str, int] | None = None
    batches: list[CloseBatchTarget] | None = Field(default=None, min_length=1, max_length=32)

    @model_validator(mode="after")
    def close_fields(self):
        supplied = (
            self.company_id,
            self.database_id,
            self.period,
            self.calculation_hash,
            self.epochs,
        )
        if self.kind == "approve_period_close":
            if self.batches is not None:
                raise ValueError("unexpected batch context")
            if any(value is None for value in supplied):
                raise ValueError("close context required")
            YearMonth(self.period)
            if set(self.epochs) != {"accounting", "material", "management"} or any(
                type(value) is not int or value < 0 for value in self.epochs.values()
            ):
                raise ValueError("invalid state versions")
        elif self.kind == "approve_close_batches":
            if any(value is not None for value in supplied) or not self.batches:
                raise ValueError("explicit batch contexts required")
            if len({item.company_id for item in self.batches}) != len(self.batches):
                raise ValueError("one exact range per company")
        elif any(value is not None for value in supplied) or self.batches is not None:
            raise ValueError("unexpected close context")
        return self


@dataclass
class WindowRecord:
    request_id: str
    request: NativeRequest
    created_at: int
    status: str = "starting"
    error_code: str | None = None
    operation_committed: bool | None = False
    login_completed: bool = False
    recovery_pending: bool = False
    recovery_code_acknowledged: bool = False
    result: dict = field(default_factory=dict)


class NativeSecurityController:
    def __init__(
        self,
        service,
        *,
        credential_store=None,
        window_opener=None,
        close_issuer=None,
        inspect_close=None,
        batch_issuer=None,
        inspect_batches=None,
    ):
        self.service = service
        self.store = credential_store or WindowsCredentialStore(
            target_name=service.credential_target
        )
        self.window_opener = window_opener
        self.close_issuer = close_issuer
        self.inspect_close = inspect_close
        self.batch_issuer = batch_issuer
        self.inspect_batches = inspect_batches
        self._lock = threading.RLock()
        self._records: dict[str, WindowRecord] = {}

    def _record(self, request_id):
        if not isinstance(request_id, str) or re.fullmatch(r"[0-9a-f]{32}", request_id) is None:
            raise IdentityError("IDENTITY_REQUEST_INVALID")
        record = self._records.get(str(request_id))
        if record is None:
            raise IdentityError("OWNER_SECURITY_REQUEST_UNKNOWN")
        if (
            record.status not in TERMINAL
            and self.service.now() >= record.created_at + 30 * 60 * SECOND
        ):
            record.status, record.error_code = "expired", "OWNER_SECURITY_REQUEST_EXPIRED"
        return record

    def _token(self):
        token = self.store.load_session_token()
        if token is None:
            raise IdentityError("IDENTITY_LOCAL_SESSION_REQUIRED")
        self.service.authorize(token)
        return token

    def _public(self, record):
        return {
            "request_id": record.request_id,
            "kind": record.request.kind,
            "status": record.status,
            "catalog_instance_id": self.service.catalog_instance_id,
            "error_code": record.error_code,
            "operation_committed": record.operation_committed,
            "login_completed": record.login_completed,
            "recovery_code_acknowledged": record.recovery_code_acknowledged,
            **record.result,
        }

    def session_status(self):
        result = self.service.status()
        try:
            authority = self.service.authorize(self.store.load_session_token())
        except IdentityError:
            return {**result, "authenticated": False}
        return {**result, "authenticated": True, "owner_id": authority.owner_id}

    def request(self, request=None, **fields):
        with self._lock:
            try:
                parsed = NativeRequest.model_validate(request if request is not None else fields)
            except ValidationError:
                raise IdentityError("IDENTITY_REQUEST_INVALID") from None
            status = self.service.status()
            if parsed.kind == "bootstrap_owner":
                if status["provisioned"]:
                    raise IdentityError("IDENTITY_OWNER_ALREADY_PROVISIONED")
                if not parsed.login_name:
                    raise IdentityError("IDENTITY_LOGIN_NAME_REQUIRED")
            elif not status["provisioned"]:
                raise IdentityError("IDENTITY_OWNER_NOT_PROVISIONED")
            name = normalize_login_name(parsed.login_name or status["login_name"])
            parsed = parsed.model_copy(update={"login_name": name})
            if parsed.kind in {
                "change_password",
                "replace_recovery_code",
                "approve_period_close",
                "approve_close_batches",
            }:
                self._token()
            if parsed.kind == "approve_period_close":
                if self.close_issuer is None or self.inspect_close is None:
                    raise IdentityError("OWNER_SECURITY_CLOSE_UNAVAILABLE")
                self.inspect_close(parsed.model_dump())
            if parsed.kind == "approve_close_batches":
                if self.batch_issuer is None or self.inspect_batches is None:
                    raise IdentityError("OWNER_SECURITY_CLOSE_UNAVAILABLE")
                self.inspect_batches(parsed.model_dump(mode="json"))
            for existing in tuple(self._records.values()):
                self._record(existing.request_id)
                if existing.status not in TERMINAL:
                    if existing.request == parsed:
                        return self._public(existing)
                    raise IdentityError("OWNER_SECURITY_WINDOW_BUSY")
            if self.window_opener is None:
                raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE")
            record = WindowRecord(uuid.uuid4().hex, parsed, self.service.now())
            self._records[record.request_id] = record
            try:
                self.window_opener(record.request_id)
            except Exception:
                record.status, record.error_code = "failed", "OWNER_SECURITY_WINDOW_UNAVAILABLE"
                raise IdentityError("OWNER_SECURITY_WINDOW_UNAVAILABLE") from None
            # Keep bounded completed status history; active requests are never removed.
            for key in tuple(self._records)[:-128]:
                if self._records[key].status in TERMINAL:
                    del self._records[key]
            return self._public(record)

    def status(self, request_id):
        with self._lock:
            known = self._records.get(request_id) if isinstance(request_id, str) else None
            if (
                isinstance(request_id, str)
                and re.fullmatch(r"[0-9a-f]{32}", request_id)
                and (known is None or known.request.kind == "approve_close_batches")
                and batch_approval_exists(self.service, request_id)
            ):
                # A committed batch survives a lost native response or daemon restart.
                authority = self.service.authorize(self._token())
                result = read_batch_approval(self.service, batch_id=request_id, authority=authority)
                return {
                    "request_id": request_id,
                    "kind": "approve_close_batches",
                    "status": "succeeded",
                    "catalog_instance_id": self.service.catalog_instance_id,
                    "operation_committed": True,
                    "login_completed": False,
                    "recovery_code_acknowledged": False,
                    "error_code": None,
                    **result,
                }
            return self._public(self._record(request_id))

    def cancel(self, request_id):
        with self._lock:
            record = self._record(request_id)
            if record.status not in TERMINAL:
                record.status = "failed" if record.operation_committed else "cancelled"
                if record.recovery_pending:
                    record.error_code = "OWNER_SECURITY_RECOVERY_CODE_NOT_ACKNOWLEDGED"
            return self._public(record)

    def _publish_login(self, record, password):
        result = self.service.login(
            record.request.login_name, password, request_id=record.request_id
        )
        try:
            self.store.save_session_token(result.session_token)
        except Exception:
            self.service.logout(result.session_token, request_id=record.request_id)
            raise IdentityError("IDENTITY_CREDENTIAL_STORE_WRITE_FAILED") from None
        record.login_completed = True

    def _execute(self, record, payload):
        if record.operation_committed is not False:
            raise IdentityError("OWNER_SECURITY_OPERATION_ALREADY_COMMITTED")
        request, kind = record.request, record.request.kind
        allowed = {"password", "new_password", "repeat_password", "recovery_code"}
        if set(payload) - allowed:
            raise IdentityError("IDENTITY_REQUEST_INVALID")
        secrets = {key: SecretStr(secret_text(value)) for key, value in payload.items()}
        if kind in {"bootstrap_owner", "change_password", "recover"}:
            if secrets.get("new_password") is None or secrets.get("new_password") != secrets.get(
                "repeat_password"
            ):
                raise IdentityError("IDENTITY_PASSWORD_CONFIRMATION_MISMATCH")
        recovery = None
        if kind == "bootstrap_owner":
            recovery = self.service.provision(
                request.login_name, secrets.get("new_password"), request_id=record.request_id
            )
        elif kind == "login":
            self._publish_login(record, secrets.get("password"))
        elif kind == "change_password":
            recovery = self.service.change_password(
                self._token(),
                secrets.get("password"),
                secrets.get("new_password"),
                request_id=record.request_id,
            )
        elif kind == "recover":
            recovery = self.service.recover(
                request.login_name,
                secrets.get("recovery_code"),
                secrets.get("new_password"),
                request_id=record.request_id,
            )
        elif kind == "replace_recovery_code":
            recovery = self.service.replace_recovery_code(
                self._token(), request_id=record.request_id
            )
        elif kind == "approve_period_close":
            result = self.close_issuer(request.model_dump(), self._token(), secrets.get("password"))
            record.result = {
                key: result[key] for key in ("approval_id", "expires_at") if key in result
            }
        elif kind == "approve_close_batches":
            record.result = self.batch_issuer(
                request.model_dump(mode="json"),
                self._token(),
                secrets.get("password"),
                record.request_id,
            )
        record.operation_committed = True
        record.recovery_pending = recovery is not None
        if kind in {"change_password", "recover"}:
            self.store.delete_session_token()
        return {"recovery_code": recovery.recovery_code.get_secret_value() if recovery else None}

    def dispatch(self, operation, payload=None, *, private=False):
        data = dict(payload or {})
        data.pop("operation", None)
        if operation == "request":
            if "request" in data and set(data) != {"request"}:
                raise IdentityError("IDENTITY_REQUEST_INVALID")
            return self.request(data.get("request", data))
        if operation in {"status", "cancel"} and set(data) != {"request_id"}:
            raise IdentityError("IDENTITY_REQUEST_INVALID")
        if operation == "status":
            return self.status(data.get("request_id"))
        if operation == "cancel":
            return self.cancel(data.get("request_id"))
        if operation == "session_status":
            if data:
                raise IdentityError("IDENTITY_REQUEST_INVALID")
            return self.session_status()
        if not private:
            raise IdentityError("OWNER_SECURITY_PRIVATE_CHANNEL_REQUIRED")
        with self._lock:
            record = self._record(data.pop("request_id", None))
            if record.status in TERMINAL:
                raise IdentityError("OWNER_SECURITY_REQUEST_FINISHED")
            result = {}
            if operation == "native_inspect":
                facts = {
                    "company_name": "本地资料目录",
                    "login_name": record.request.login_name,
                    "target_label": self.service.catalog_instance_id,
                }
                if record.request.kind == "approve_period_close":
                    facts.update(self.inspect_close(record.request.model_dump()))
                elif record.request.kind == "approve_close_batches":
                    facts.update(self.inspect_batches(record.request.model_dump(mode="json")))
                result = {"request": record.request.model_dump(), "facts": facts}
            elif operation == "native_update":
                if set(data) - {
                    "status",
                    "error_code",
                    "operation_committed",
                    "login_completed",
                    "recovery_code_acknowledged",
                }:
                    raise IdentityError("IDENTITY_REQUEST_INVALID")
                # Native UI may report visibility/cancellation, never forge commitment.
                next_status = data.get("status")
                if next_status not in {
                    "waiting_for_user",
                    "running",
                    "failed",
                    "cancelled",
                    "succeeded",
                }:
                    raise IdentityError("IDENTITY_REQUEST_INVALID")
                if next_status == "succeeded" and (
                    not record.operation_committed or record.recovery_pending
                ):
                    raise IdentityError("OWNER_SECURITY_OPERATION_INCOMPLETE")
                if next_status == "cancelled" and record.operation_committed:
                    raise IdentityError("OWNER_SECURITY_OPERATION_ALREADY_COMMITTED")
                code = data.get("error_code")
                if code is not None and (
                    not isinstance(code, str)
                    or len(code) > 100
                    or not all(c.isupper() or c == "_" or c.isdigit() for c in code)
                ):
                    raise IdentityError("IDENTITY_REQUEST_INVALID")
                record.status = next_status
                record.error_code = code
            elif operation == "native_execute":
                result = self._execute(record, data)
            elif operation == "native_finish":
                if not record.operation_committed or not record.recovery_pending:
                    raise IdentityError("OWNER_SECURITY_OPERATION_INCOMPLETE")
                if record.request.kind == "bootstrap_owner":
                    self._publish_login(record, SecretStr(secret_text(data.get("new_password"))))
                record.recovery_pending = False
                record.recovery_code_acknowledged = True
            else:
                raise IdentityError("IDENTITY_REQUEST_INVALID")
            return {**self._public(record), **result}
