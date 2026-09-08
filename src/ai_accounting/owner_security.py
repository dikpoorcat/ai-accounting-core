"""Typed local security operations. Secrets stay inside the native-window process."""

from __future__ import annotations

import hashlib
import json
import uuid
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import select

from .accounting_period_schemas import PreviewAccountingPeriodCloseRequest
from .accounting_period_service import AccountingPeriodService
from .company_router import CompanyDatabaseRouter, CompanyRoutingError
from .config import get_settings
from .credential_store import WindowsCredentialStore
from .database import SessionLocal
from .identity import ExecutorIdentity, ExecutorKind, IdentityError, validate_password_for_login
from .identity_schemas import (
    LoginName,
    OwnerLoginRequest,
    OwnerPasswordChangeRequest,
    OwnerProvisionRequest,
    OwnerRecoveryCodeReplacementRequest,
    OwnerRecoveryResetRequest,
    OwnerSessionRevokeRequest,
)
from .identity_service import IdentityService
from .models import (
    AccountingPeriod,
    AccountingPeriodCloseApproval,
    CatalogMetadata,
    Organization,
    OrganizationDatabaseMetadata,
    OwnerAccount,
)

SecurityKind = Literal[
    "bootstrap_owner",
    "login",
    "approve_period_close",
    "change_password",
    "recover",
    "replace_recovery_code",
]
WINDOW_TITLES = {
    "bootstrap_owner": "AI 记账内核 - 首次负责人设置",
    "login": "AI 记账内核 - 负责人登录",
    "approve_period_close": "AI 记账内核 - 关账密码确认",
    "change_password": "AI 记账内核 - 修改负责人密码",
    "recover": "AI 记账内核 - 恢复负责人账号",
    "replace_recovery_code": "AI 记账内核 - 更换恢复码",
}
_EXECUTOR = ExecutorIdentity(
    kind=ExecutorKind.SYSTEM_JOB, executor_name="owner-security-window", executor_version="1"
)


class OwnerSecurityWindowRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    kind: SecurityKind
    org_id: uuid.UUID | None = None
    login_name: LoginName | None = None
    period_id: uuid.UUID | None = None
    calculation_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def operation_fields(self) -> OwnerSecurityWindowRequest:
        if self.kind in {"bootstrap_owner", "approve_period_close"} and self.org_id is None:
            raise ValueError("OWNER_SECURITY_ORGANIZATION_REQUIRED")
        if self.kind == "approve_period_close":
            if self.period_id is None or self.calculation_hash is None:
                raise ValueError("OWNER_SECURITY_CLOSE_CONTEXT_REQUIRED")
        elif self.period_id is not None or self.calculation_hash is not None:
            raise ValueError("OWNER_SECURITY_UNEXPECTED_CLOSE_CONTEXT")
        return self


class OwnerSecurityStatusRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    request_id: uuid.UUID


def database_scope(session, settings) -> str:
    """Do not use credentials in the scope, or share a token with a cloned endpoint."""
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url)
    if settings.multi_company_enabled:
        marker = session.get(CatalogMetadata, 1)
        if marker is None:
            raise IdentityError("IDENTITY_CATALOG_UNAVAILABLE")
        identity = str(marker.catalog_instance_id)
    else:
        marker = session.get(OrganizationDatabaseMetadata, 1)
        if marker is not None:
            identity = str(marker.database_identity)
        elif settings.finance_environment == "development":
            identity = str(session.scalar(select(Organization.id).limit(1)) or "uninitialized")
        else:
            raise IdentityError("OWNER_SECURITY_TARGET_NOT_INITIALIZED")
    endpoint = [url.get_backend_name(), url.host, url.port, url.database, identity]
    return hashlib.sha256(json.dumps(endpoint, ensure_ascii=True).encode()).hexdigest()


class OwnerSecurityOperations:
    def __init__(self, *, settings=None, factory=None, store=None):
        self.settings = settings or get_settings()
        self.factory = factory or SessionLocal
        self.router = CompanyDatabaseRouter(self.settings)
        self._store = store
        self.operation_committed = False
        self.login_completed = False
        self._expected_scope = None

    @contextmanager
    def transaction(self):
        try:
            with self.factory.begin() as session:
                session.info["catalog_mode"] = self.settings.multi_company_enabled
                if (
                    self._expected_scope is not None
                    and database_scope(session, self.settings) != self._expected_scope
                ):
                    raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")
                yield session
        except CompanyRoutingError as exc:
            raise IdentityError(exc.code) from None

    def scope(self) -> str:
        with self.transaction() as session:
            return database_scope(session, self.settings)

    @property
    def store(self):
        if self._store is None:
            self._store = WindowsCredentialStore(
                target_name=f"ai-accounting-core/local-owner-session/v2/{self.scope()}"
            )
        return self._store

    def assert_target(self, expected_scope: str) -> None:
        if self.scope() != expected_scope:
            raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")

    def _token(self):
        token = self.store.load_session_token()
        if token is None:
            raise IdentityError("IDENTITY_LOCAL_SESSION_REQUIRED")
        return token

    def inspect(self, request: OwnerSecurityWindowRequest, expected_scope: str) -> dict:
        self.assert_target(expected_scope)
        self._expected_scope = expected_scope
        with self.transaction() as session:
            owner = session.scalar(select(OwnerAccount).limit(1))
            if request.kind == "bootstrap_owner":
                if owner is not None:
                    raise IdentityError("IDENTITY_OWNER_ALREADY_PROVISIONED")
                login_name = request.login_name or "owner"
                org_id = request.org_id
            else:
                if owner is None or owner.status != "active":
                    raise IdentityError("AUTHENTICATION_REQUIRED")
                if request.login_name is not None and request.login_name != owner.login_name:
                    raise IdentityError("IDENTITY_AUTHENTICATION_FAILED")
                login_name = owner.login_name
                org_id = request.org_id or owner.org_id
            if self.settings.multi_company_enabled:
                company = self.router.resolve(
                    session,
                    org_id,
                    for_write=request.kind in {"bootstrap_owner", "approve_period_close"},
                )
                name = company.display_name
                if request.kind == "bootstrap_owner" and not company.is_primary:
                    raise IdentityError("OWNER_SECURITY_PRIMARY_COMPANY_REQUIRED")
            else:
                company = session.get(Organization, org_id)
                if company is None or (owner is not None and owner.org_id != org_id):
                    raise IdentityError("ORGANIZATION_CONTEXT_MISMATCH")
                name = company.name
            if request.kind in {"change_password", "replace_recovery_code"}:
                IdentityService(session).authorize_execution(
                    session_token=self._token().get_secret_value(),
                    executor=_EXECUTOR,
                    request_correlation_id=uuid.uuid4(),
                    expected_org_id=org_id,
                )
            from sqlalchemy.engine import make_url

            target_database = make_url(self.settings.database_url).database
            result = {
                "login_name": login_name,
                "company_name": name,
                "org_id": str(org_id),
                "target_label": f"{target_database} / {expected_scope[:12]}",
            }
            if request.kind == "approve_period_close":
                if self.settings.multi_company_enabled:
                    with self.router.factory_for(company)() as business:
                        self._assert_business_target(
                            business, company, session.get(CatalogMetadata, 1).catalog_instance_id
                        )
                        result["period_month"] = self._preview(business, request)
                else:
                    result["period_month"] = self._preview(session, request)
            return result

    @staticmethod
    def _preview(session, request) -> str:
        period = session.scalar(
            select(AccountingPeriod)
            .where(
                AccountingPeriod.org_id == request.org_id,
                AccountingPeriod.id == request.period_id,
            )
            .with_for_update()
        )
        if period is None or period.status != "open":
            raise IdentityError("ACCOUNTING_PERIOD_NOT_OPEN")
        preview = AccountingPeriodService(session).preview_accounting_period_close(
            PreviewAccountingPeriodCloseRequest(
                org_id=request.org_id,
                period_id=period.id,
                closing_date=period.end_date,
            )
        )
        if (
            preview.status.value != "calculated"
            or preview.calculation_hash != request.calculation_hash
        ):
            raise IdentityError("ACCOUNTING_PERIOD_CALCULATION_STALE")
        return f"{period.calendar_year:04d}-{period.calendar_month:02d}"

    def _identity_operation(self, operation):
        failure = None
        result = None
        try:
            with self.transaction() as session:
                try:
                    result = operation(IdentityService(session))
                except IdentityError as exc:
                    failure = exc
        except Exception:
            if result is not None and self.operation_committed is False:
                # A lost commit acknowledgement cannot safely be treated as a rollback.
                self.operation_committed = None
            raise
        if failure is not None:
            raise failure
        return result

    @staticmethod
    def _assert_business_target(session, registry, catalog_id):
        marker = session.get(OrganizationDatabaseMetadata, 1)
        if (
            marker is None
            or marker.org_id != registry.org_id
            or marker.database_identity != registry.database_identity
            or marker.current_catalog_instance_id != catalog_id
        ):
            raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")

    def _authenticate(self, login_name, password):
        return self._identity_operation(
            lambda service: service.authenticate(
                OwnerLoginRequest(login_name=login_name, password=password)
            )
        )

    def _publish_login(self, identity) -> None:
        previous = None
        previous_loaded = False
        try:
            previous = self.store.load_session_token()
            previous_loaded = True
            self.store.save_session_token(identity.session_token)
        except Exception:
            if previous_loaded:
                try:
                    if previous is None:
                        self.store.delete_session_token()
                    else:
                        self.store.save_session_token(previous)
                except Exception:
                    pass  # Best effort only; the newly issued server session is revoked below.
            self._identity_operation(
                lambda service: service.revoke_session(
                    OwnerSessionRevokeRequest(session_token=identity.session_token)
                )
            )
            raise IdentityError("IDENTITY_CREDENTIAL_STORE_WRITE_FAILED") from None
        self.login_completed = True
        if previous is not None:
            self._identity_operation(
                lambda service: service.revoke_session(
                    OwnerSessionRevokeRequest(session_token=previous)
                )
            )

    def execute(
        self,
        request,
        expected_scope,
        *,
        password=None,
        new_password=None,
        repeat_password=None,
        recovery_code=None,
    ):
        """Called only by the GUI; never expose this secret-bearing method as MCP/CLI."""
        facts = self.inspect(request, expected_scope)
        login_name = facts["login_name"]
        if request.kind in {"bootstrap_owner", "change_password", "recover"}:
            if new_password is None or new_password != repeat_password:
                raise IdentityError("IDENTITY_PASSWORD_CONFIRMATION_MISMATCH")
            validate_password_for_login(
                password=new_password.get_secret_value(), login_name=login_name
            )
        if request.kind == "bootstrap_owner":
            result = self._identity_operation(
                lambda service: service.provision_owner(
                    OwnerProvisionRequest(
                        org_id=request.org_id, login_name=login_name, password=new_password
                    )
                )
            )
        elif request.kind == "login":
            identity = self._authenticate(login_name, password)
            self.operation_committed = True
            self._publish_login(identity)
            return None
        elif request.kind == "approve_period_close":
            self._approve_close(request, login_name, password, expected_scope)
            return None
        elif request.kind == "recover":
            result = self._identity_operation(
                lambda service: service.reset_password_with_recovery(
                    OwnerRecoveryResetRequest(
                        login_name=login_name,
                        recovery_code=recovery_code,
                        new_password=new_password,
                    )
                )
            )
        elif request.kind == "change_password":
            token = self._token()
            result = self._identity_operation(
                lambda service: service.change_password(
                    OwnerPasswordChangeRequest(
                        login_name=login_name,
                        session_token=token,
                        current_password=password,
                        new_password=new_password,
                    )
                )
            )
        else:
            token = self._token()
            result = self._identity_operation(
                lambda service: service.replace_recovery_code(
                    OwnerRecoveryCodeReplacementRequest(session_token=token)
                )
            )
        self.operation_committed = True
        return result.recovery_code

    def finish_recovery_display(self, request, expected_scope, *, new_password=None):
        self.assert_target(expected_scope)
        if request.kind == "bootstrap_owner":
            # The account is already durable; failure here must never repeat provisioning.
            identity = self._authenticate(request.login_name or "owner", new_password)
            self._publish_login(identity)
        elif request.kind in {"recover", "change_password"}:
            self.store.delete_session_token()

    def _approve_close(self, request, login_name, password, expected_scope):
        identity = self._authenticate(login_name, password)
        try:
            self.assert_target(expected_scope)
            with self.transaction() as catalog:
                context = IdentityService(catalog).authorize_execution(
                    session_token=identity.session_token.get_secret_value(),
                    executor=_EXECUTOR,
                    request_correlation_id=uuid.uuid4(),
                    expected_org_id=request.org_id,
                )
                if self.settings.multi_company_enabled:
                    registry = self.router.resolve(catalog, request.org_id, for_write=True)
                    with self.router.factory_for(registry).begin() as business:
                        self._assert_business_target(
                            business, registry, context.catalog_instance_id
                        )
                        self._create_approval(business, request, context)
                else:
                    self._create_approval(catalog, request, context)
            self.operation_committed = True
            self._publish_login(identity)
        except Exception:
            if not self.login_completed:
                self._identity_operation(
                    lambda service: service.revoke_session(
                        OwnerSessionRevokeRequest(session_token=identity.session_token)
                    )
                )
            raise

    def _create_approval(self, session, request, context):
        self._preview(session, request)
        now = datetime.now(UTC)
        session.add(
            AccountingPeriodCloseApproval(
                org_id=request.org_id,
                period_id=request.period_id,
                catalog_instance_id=context.catalog_instance_id,
                owner_account_id=context.owner_account_id,
                owner_session_id=context.owner_session_id,
                owner_credential_version=context.owner_credential_version,
                calculation_hash=request.calculation_hash,
                confirmation_method="local_password_reauthentication",
                confirmed_at=now,
                expires_at=now + timedelta(minutes=30),
            )
        )
        session.flush()

    def logout(self):
        token = self.store.load_session_token()
        if token is not None:
            self._identity_operation(
                lambda service: service.revoke_session(
                    OwnerSessionRevokeRequest(session_token=token)
                )
            )
        self.store.delete_session_token()
