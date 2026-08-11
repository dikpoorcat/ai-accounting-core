"""Private identity-service contracts, with secrets redacted by default."""

from __future__ import annotations

import uuid
from typing import Annotated

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, SecretStr, model_validator

from .identity import IdentityError, normalize_login_name, validate_password_for_login


def _normalize_login(value: object) -> object:
    if not isinstance(value, str):
        return value
    return normalize_login_name(value)


LoginName = Annotated[
    str,
    BeforeValidator(_normalize_login),
    Field(min_length=3, max_length=100),
]
ExecutorIdentifier = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9._:-]{1,100}$"),
]


class _PasswordPolicyRequest(BaseModel):
    """Shared password-policy validation while preserving pydantic redaction."""

    model_config = ConfigDict(extra="forbid")

    login_name: LoginName

    def _validate_password(self, password: SecretStr) -> None:
        try:
            validate_password_for_login(
                password=password.get_secret_value(),
                login_name=self.login_name,
            )
        except IdentityError as exc:
            raise ValueError(exc.code) from exc


class OwnerProvisionRequest(_PasswordPolicyRequest):
    org_id: uuid.UUID
    password: SecretStr
    request_correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)

    @model_validator(mode="after")
    def valid_password(self) -> OwnerProvisionRequest:
        self._validate_password(self.password)
        return self


class OwnerLoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    login_name: LoginName
    password: SecretStr
    request_correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)


class OwnerPasswordChangeRequest(_PasswordPolicyRequest):
    session_token: SecretStr
    current_password: SecretStr
    new_password: SecretStr
    request_correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)

    @model_validator(mode="after")
    def valid_new_password(self) -> OwnerPasswordChangeRequest:
        self._validate_password(self.new_password)
        return self


class OwnerRecoveryResetRequest(_PasswordPolicyRequest):
    recovery_code: SecretStr
    new_password: SecretStr
    request_correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)

    @model_validator(mode="after")
    def valid_new_password(self) -> OwnerRecoveryResetRequest:
        self._validate_password(self.new_password)
        return self


class OwnerRecoveryCodeReplacementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_token: SecretStr
    request_correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)


class OwnerSessionRevokeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_token: SecretStr
    request_correlation_id: uuid.UUID = Field(default_factory=uuid.uuid4)


class OwnerProvisionResult(BaseModel):
    """The sole recovery code is redacted in representations and JSON logs."""

    model_config = ConfigDict(extra="forbid")

    owner_account_id: uuid.UUID
    recovery_code: SecretStr


class OwnerAuthenticationResult(BaseModel):
    """Opaque token for a future local credential-store integration only."""

    model_config = ConfigDict(extra="forbid")

    owner_account_id: uuid.UUID
    session_id: uuid.UUID
    session_token: SecretStr


class OwnerRecoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    owner_account_id: uuid.UUID
    recovery_code: SecretStr
