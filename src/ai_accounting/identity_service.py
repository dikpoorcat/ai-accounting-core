"""Transactional service for the one local business-owner identity.

This service intentionally has no MCP registration and no credential-store or
command-line dependency.  A future local launcher may pass an opaque session
token to it, but passwords, recovery codes, and tokens are never accepted as
environment values or command-line arguments.
"""

from __future__ import annotations

import secrets
import uuid
from collections.abc import Callable
from datetime import UTC, datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from .identity import (
    ExecutionContext,
    ExecutorIdentity,
    IdentityError,
    hash_password,
    new_recovery_code,
    new_session_token,
    normalize_authentication_password,
    normalized_login_key,
    recovery_code_matches,
    recovery_code_sha256,
    token_sha256,
    validate_password_for_login,
    verify_password,
)
from .identity_schemas import (
    OwnerAuthenticationResult,
    OwnerLoginRequest,
    OwnerPasswordChangeRequest,
    OwnerProvisionRequest,
    OwnerProvisionResult,
    OwnerRecoveryCodeReplacementRequest,
    OwnerRecoveryResetRequest,
    OwnerRecoveryResult,
    OwnerSessionRevokeRequest,
)
from .models import (
    IdentityAuditEvent,
    Organization,
    OwnerAccount,
    OwnerRecoveryCode,
    OwnerSession,
)

SESSION_IDLE_TIMEOUT = timedelta(minutes=30)
SESSION_ABSOLUTE_TIMEOUT = timedelta(hours=8)
AUTH_FAILURE_THRESHOLD = 5
AUTH_FAILURE_BACKOFF_BASE = timedelta(seconds=30)
AUTH_FAILURE_BACKOFF_MAX = timedelta(minutes=15)

# A fixed valid PHC value lets unknown accounts take the same Argon2id verify
# path.  The source password is not a credential and is intentionally absent.
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$998d56tE5yS24cuu8oNNOw$"
    "OhE9v7CVWzFi2OYdDJmq/R6CyvmmSA1OsccIYczieFE"
)


class IdentityService:
    """Apply the frozen local-owner authentication rules in one transaction."""

    def __init__(
        self,
        session: Session,
        *,
        now: Callable[[], datetime] = lambda: datetime.now(UTC),
        randbytes: Callable[[int], bytes] = secrets.token_bytes,
    ) -> None:
        self.session = session
        self._now = now
        self._randbytes = randbytes

    def provision_owner(self, request: OwnerProvisionRequest) -> OwnerProvisionResult:
        """Create the deployment's only owner and issue its only recovery code."""

        now = self._clock()
        password = request.password.get_secret_value()
        validated_password = validate_password_for_login(
            password=password,
            login_name=request.login_name,
        )
        if self.session.get(Organization, request.org_id) is None:
            raise IdentityError("IDENTITY_ORGANIZATION_NOT_FOUND")
        if self.session.scalar(select(OwnerAccount.id).limit(1)) is not None:
            raise IdentityError("IDENTITY_OWNER_ALREADY_PROVISIONED")
        recovery_code = new_recovery_code(self._randbytes)
        account = OwnerAccount(
            org_id=request.org_id,
            login_name=request.login_name,
            login_name_normalized=normalized_login_key(request.login_name),
            password_hash=hash_password(validated_password),
            credential_version=1,
            password_changed_at=now,
            created_at=now,
            updated_at=now,
        )
        recovery = OwnerRecoveryCode(
            org_id=request.org_id,
            owner_account_id=account.id,
            code_sha256=recovery_code_sha256(recovery_code),
            credential_version=1,
            created_at=now,
        )
        # A nested transaction converts a concurrent singleton insert into the
        # same stable result without rolling back the caller's outer work.
        try:
            with self.session.begin_nested():
                self.session.add(account)
                self.session.flush()
                recovery.owner_account_id = account.id
                self.session.add(recovery)
                self._audit(
                    org_id=account.org_id,
                    owner_account_id=account.id,
                    session_id=None,
                    event_type="owner_provisioned",
                    outcome="succeeded",
                    reason_code=None,
                    correlation_id=request.request_correlation_id,
                    occurred_at=now,
                )
                self.session.flush()
        except IntegrityError as exc:
            raise IdentityError("IDENTITY_OWNER_ALREADY_PROVISIONED") from exc
        return OwnerProvisionResult(owner_account_id=account.id, recovery_code=recovery_code)

    def authenticate(self, request: OwnerLoginRequest) -> OwnerAuthenticationResult:
        """Check local credentials and create one hash-only server session."""

        now = self._clock()
        account = self.session.scalar(
            select(OwnerAccount)
            .where(OwnerAccount.login_name_normalized == normalized_login_key(request.login_name))
            .with_for_update()
        )
        password = _normalizable_authentication_password(request.password.get_secret_value())
        if account is None:
            verify_password(password_hash=_DUMMY_PASSWORD_HASH, password=password or "")
            self._audit_unknown_login_failure(
                correlation_id=request.request_correlation_id,
                now=now,
            )
            raise IdentityError("IDENTITY_AUTHENTICATION_FAILED")
        if password is None:
            verify_password(password_hash=_DUMMY_PASSWORD_HASH, password="")
            self._record_auth_failure(account, now)
            self._audit_auth_failure(
                account=account,
                correlation_id=request.request_correlation_id,
                now=now,
                outcome="rejected",
                reason_code="INVALID_CREDENTIALS",
            )
            raise IdentityError("IDENTITY_AUTHENTICATION_FAILED")
        if account.status != "active":
            self._audit_auth_failure(
                account=account,
                correlation_id=request.request_correlation_id,
                now=now,
                outcome="blocked",
                reason_code="ACCOUNT_DISABLED",
            )
            raise IdentityError("IDENTITY_AUTHENTICATION_FAILED")
        if _is_throttled(account.password_throttled_until, now):
            self._audit_auth_failure(
                account=account,
                correlation_id=request.request_correlation_id,
                now=now,
                outcome="blocked",
                reason_code="ACCOUNT_THROTTLED",
            )
            raise IdentityError("IDENTITY_AUTHENTICATION_FAILED")
        if not verify_password(password_hash=account.password_hash, password=password):
            self._record_auth_failure(account, now)
            self._audit_auth_failure(
                account=account,
                correlation_id=request.request_correlation_id,
                now=now,
                outcome="rejected",
                reason_code="INVALID_CREDENTIALS",
            )
            raise IdentityError("IDENTITY_AUTHENTICATION_FAILED")

        self._clear_auth_failures(account, now)
        account.last_authenticated_at = now
        account.updated_at = _not_before(now, account.updated_at)
        token = new_session_token(self._randbytes)
        owner_session = self._new_session(account=account, token=token, now=now)
        self.session.add(owner_session)
        self._audit(
            org_id=account.org_id,
            owner_account_id=account.id,
            session_id=owner_session.id,
            event_type="login_succeeded",
            outcome="succeeded",
            reason_code=None,
            correlation_id=request.request_correlation_id,
            occurred_at=now,
        )
        self.session.flush()
        return OwnerAuthenticationResult(
            owner_account_id=account.id,
            session_id=owner_session.id,
            session_token=token,
        )

    def authorize_execution(
        self,
        *,
        session_token: str,
        executor: ExecutorIdentity,
        request_correlation_id: uuid.UUID,
        expected_org_id: uuid.UUID | None = None,
    ) -> ExecutionContext:
        """Validate a session and return owner authority plus AI/system attribution."""

        owner_session, account = self._require_session(
            token=session_token,
            correlation_id=request_correlation_id,
            expected_org_id=expected_org_id,
        )
        if not isinstance(executor, ExecutorIdentity):
            raise IdentityError("IDENTITY_EXECUTOR_INVALID")
        return ExecutionContext(
            org_id=account.org_id,
            owner_account_id=account.id,
            owner_session_id=owner_session.id,
            owner_credential_version=account.credential_version,
            executor_kind=executor.kind,
            executor_name=executor.executor_name,
            executor_version=executor.executor_version,
            request_correlation_id=request_correlation_id,
        )

    def revoke_session(self, request: OwnerSessionRevokeRequest) -> None:
        """Revoke the supplied session; a repeated logout is safe and opaque."""

        now = self._clock()
        locked = self._lock_session_and_owner(request.session_token.get_secret_value())
        owner_session = locked[0] if locked is not None else None
        if owner_session is None or owner_session.revoked_at is not None:
            return
        owner_session.revoked_at = now
        owner_session.revoke_reason = "logout"
        self._audit(
            org_id=owner_session.org_id,
            owner_account_id=owner_session.owner_account_id,
            session_id=owner_session.id,
            event_type="session_revoked",
            outcome="succeeded",
            reason_code=None,
            correlation_id=request.request_correlation_id,
            occurred_at=now,
        )
        self.session.flush()

    def change_password(self, request: OwnerPasswordChangeRequest) -> OwnerRecoveryResult:
        """Rotate credentials after re-authentication and replace the recovery code."""

        owner_session, account = self._require_session(
            token=request.session_token.get_secret_value(),
            correlation_id=request.request_correlation_id,
        )
        now = self._clock()
        current_password = _normalizable_authentication_password(
            request.current_password.get_secret_value()
        )
        if current_password is None or not verify_password(
            password_hash=account.password_hash, password=current_password
        ):
            self._record_auth_failure(account, now)
            self._audit_auth_failure(
                account=account,
                correlation_id=request.request_correlation_id,
                now=now,
                outcome="rejected",
                reason_code="INVALID_CREDENTIALS",
            )
            raise IdentityError("IDENTITY_AUTHENTICATION_FAILED")
        new_password = validate_password_for_login(
            password=request.new_password.get_secret_value(), login_name=account.login_name
        )
        recovery_code = self._rotate_credentials(
            account=account,
            now=now,
            new_password=new_password,
            revoke_reason="credential_changed",
        )
        self._audit(
            org_id=account.org_id,
            owner_account_id=account.id,
            session_id=owner_session.id,
            event_type="password_changed",
            outcome="succeeded",
            reason_code=None,
            correlation_id=request.request_correlation_id,
            occurred_at=now,
        )
        self.session.flush()
        return OwnerRecoveryResult(owner_account_id=account.id, recovery_code=recovery_code)

    def reset_password_with_recovery(
        self, request: OwnerRecoveryResetRequest
    ) -> OwnerRecoveryResult:
        """Consume the sole recovery code, revoke sessions, and issue a replacement."""

        now = self._clock()
        account = self.session.scalar(
            select(OwnerAccount)
            .where(OwnerAccount.login_name_normalized == normalized_login_key(request.login_name))
            .with_for_update()
        )
        if account is None or account.status != "active":
            raise IdentityError("IDENTITY_RECOVERY_FAILED")
        if _is_throttled(account.recovery_throttled_until, now):
            self._audit_recovery_failure(
                account=account,
                correlation_id=request.request_correlation_id,
                now=now,
                outcome="blocked",
                reason_code="RECOVERY_THROTTLED",
            )
            raise IdentityError("IDENTITY_RECOVERY_FAILED")
        code = self.session.scalar(
            select(OwnerRecoveryCode).where(
                OwnerRecoveryCode.owner_account_id == account.id,
                OwnerRecoveryCode.used_at.is_(None),
                OwnerRecoveryCode.invalidated_at.is_(None),
            ).with_for_update()
        )
        if (
            code is None
            or code.credential_version != account.credential_version
            or not recovery_code_matches(
                expected_sha256=code.code_sha256,
                supplied_code=request.recovery_code.get_secret_value(),
            )
        ):
            self._record_recovery_failure(account, now)
            self._audit_recovery_failure(
                account=account,
                correlation_id=request.request_correlation_id,
                now=now,
                outcome="rejected",
                reason_code="RECOVERY_CODE_INVALID",
            )
            raise IdentityError("IDENTITY_RECOVERY_FAILED")
        code.used_at = now
        password = validate_password_for_login(
            password=request.new_password.get_secret_value(), login_name=account.login_name
        )
        recovery_code = self._rotate_credentials(
            account=account,
            now=now,
            new_password=password,
            revoke_reason="recovery_used",
        )
        self._audit(
            org_id=account.org_id,
            owner_account_id=account.id,
            session_id=None,
            event_type="recovery_succeeded",
            outcome="succeeded",
            reason_code=None,
            correlation_id=request.request_correlation_id,
            occurred_at=now,
        )
        self.session.flush()
        return OwnerRecoveryResult(owner_account_id=account.id, recovery_code=recovery_code)

    def replace_recovery_code(
        self, request: OwnerRecoveryCodeReplacementRequest
    ) -> OwnerRecoveryResult:
        """Replace, never reveal, the current recovery code for a live session."""

        owner_session, account = self._require_session(
            token=request.session_token.get_secret_value(),
            correlation_id=request.request_correlation_id,
        )
        now = self._clock()
        recovery_code = self._replace_recovery_code(account=account, now=now)
        self._audit(
            org_id=account.org_id,
            owner_account_id=account.id,
            session_id=owner_session.id,
            event_type="recovery_code_replaced",
            outcome="succeeded",
            reason_code=None,
            correlation_id=request.request_correlation_id,
            occurred_at=now,
        )
        self.session.flush()
        return OwnerRecoveryResult(owner_account_id=account.id, recovery_code=recovery_code)

    def _require_session(
        self,
        *,
        token: str,
        correlation_id: uuid.UUID,
        expected_org_id: uuid.UUID | None = None,
    ) -> tuple[OwnerSession, OwnerAccount]:
        now = self._clock()
        locked = self._lock_session_and_owner(token)
        if locked is None:
            raise IdentityError("IDENTITY_SESSION_INVALID")
        owner_session, account = locked
        if expected_org_id is not None and account.org_id != expected_org_id:
            raise IdentityError("ORGANIZATION_CONTEXT_MISMATCH")
        expiry_reason = _session_expiry_reason(owner_session, account, now)
        if expiry_reason is not None:
            if owner_session.revoked_at is None:
                owner_session.revoked_at = now
                owner_session.revoke_reason = expiry_reason[0]
                self._audit(
                    org_id=account.org_id,
                    owner_account_id=account.id,
                    session_id=owner_session.id,
                    event_type="session_expired",
                    outcome="rejected",
                    reason_code=expiry_reason[1],
                    correlation_id=correlation_id,
                    occurred_at=now,
                )
                self.session.flush()
            raise IdentityError("IDENTITY_SESSION_INVALID")
        owner_session.last_seen_at = now
        owner_session.idle_expires_at = min(
            now + SESSION_IDLE_TIMEOUT,
            _as_utc(owner_session.absolute_expires_at),
        )
        self.session.flush()
        return owner_session, account

    def _lock_session_and_owner(self, token: str) -> tuple[OwnerSession, OwnerAccount] | None:
        """Lock account then session and recheck the digest under both locks.

        The caller must retain this Session transaction through its associated
        business write and commit.  It is the future business service's
        linearization point with logout, recovery, and credential rotation.
        """

        digest = token_sha256(token)
        owner_account_id = self.session.execute(
            select(OwnerSession.owner_account_id).where(OwnerSession.secret_sha256 == digest)
        ).scalar_one_or_none()
        if owner_account_id is None:
            return None
        account = self.session.scalar(
            select(OwnerAccount).where(OwnerAccount.id == owner_account_id).with_for_update()
        )
        if account is None:
            return None
        owner_session = self.session.scalar(
            select(OwnerSession)
            .where(
                OwnerSession.secret_sha256 == digest,
                OwnerSession.owner_account_id == account.id,
                OwnerSession.org_id == account.org_id,
            )
            .with_for_update()
        )
        return (owner_session, account) if owner_session is not None else None

    def _new_session(self, *, account: OwnerAccount, token: str, now: datetime) -> OwnerSession:
        absolute_expires_at = now + SESSION_ABSOLUTE_TIMEOUT
        return OwnerSession(
            org_id=account.org_id,
            owner_account_id=account.id,
            secret_sha256=token_sha256(token),
            credential_version=account.credential_version,
            created_at=now,
            last_seen_at=now,
            idle_expires_at=now + SESSION_IDLE_TIMEOUT,
            absolute_expires_at=absolute_expires_at,
        )

    def _rotate_credentials(
        self,
        *,
        account: OwnerAccount,
        now: datetime,
        new_password: str,
        revoke_reason: str,
    ) -> str:
        account.password_hash = hash_password(new_password)
        account.credential_version += 1
        account.password_changed_at = _strictly_after(now, account.password_changed_at)
        account.updated_at = _not_before(account.password_changed_at, account.updated_at)
        self._clear_auth_failures(account, now)
        account.recovery_failed_attempts = 0
        account.recovery_throttled_until = None
        self.session.execute(
            update(OwnerSession)
            .where(
                OwnerSession.owner_account_id == account.id,
                OwnerSession.revoked_at.is_(None),
            )
            .values(revoked_at=now, revoke_reason=revoke_reason)
        )
        return self._replace_recovery_code(account=account, now=now)

    def _replace_recovery_code(self, *, account: OwnerAccount, now: datetime) -> str:
        self.session.execute(
            update(OwnerRecoveryCode)
            .where(
                OwnerRecoveryCode.owner_account_id == account.id,
                OwnerRecoveryCode.used_at.is_(None),
                OwnerRecoveryCode.invalidated_at.is_(None),
            )
            .values(invalidated_at=now)
        )
        recovery_code = new_recovery_code(self._randbytes)
        self.session.add(
            OwnerRecoveryCode(
                org_id=account.org_id,
                owner_account_id=account.id,
                code_sha256=recovery_code_sha256(recovery_code),
                credential_version=account.credential_version,
                created_at=now,
            )
        )
        return recovery_code

    def _record_auth_failure(self, account: OwnerAccount, now: datetime) -> None:
        account.password_failed_attempts += 1
        account.password_throttled_until = _failure_backoff(account.password_failed_attempts, now)
        account.updated_at = _not_before(now, account.updated_at)

    def _record_recovery_failure(self, account: OwnerAccount, now: datetime) -> None:
        account.recovery_failed_attempts += 1
        account.recovery_throttled_until = _failure_backoff(account.recovery_failed_attempts, now)
        account.updated_at = _not_before(now, account.updated_at)

    @staticmethod
    def _clear_auth_failures(account: OwnerAccount, now: datetime) -> None:
        account.password_failed_attempts = 0
        account.password_throttled_until = None

    def _audit_auth_failure(
        self,
        *,
        account: OwnerAccount,
        correlation_id: uuid.UUID,
        now: datetime,
        outcome: str,
        reason_code: str,
    ) -> None:
        self._audit(
            org_id=account.org_id,
            owner_account_id=account.id,
            session_id=None,
            event_type="login_failed",
            outcome=outcome,
            reason_code=reason_code,
            correlation_id=correlation_id,
            occurred_at=now,
        )
        self.session.flush()

    def _audit_unknown_login_failure(self, *, correlation_id: uuid.UUID, now: datetime) -> None:
        """Record an opaque failure only when this is an initialized singleton deployment."""

        organizations = self.session.scalars(select(Organization).limit(2)).all()
        if len(organizations) != 1:
            return
        self._audit(
            org_id=organizations[0].id,
            owner_account_id=None,
            session_id=None,
            event_type="login_failed",
            outcome="rejected",
            reason_code="INVALID_CREDENTIALS",
            correlation_id=correlation_id,
            occurred_at=now,
        )
        self.session.flush()

    def _audit_recovery_failure(
        self,
        *,
        account: OwnerAccount,
        correlation_id: uuid.UUID,
        now: datetime,
        outcome: str,
        reason_code: str,
    ) -> None:
        self._audit(
            org_id=account.org_id,
            owner_account_id=account.id,
            session_id=None,
            event_type="recovery_failed",
            outcome=outcome,
            reason_code=reason_code,
            correlation_id=correlation_id,
            occurred_at=now,
        )
        self.session.flush()

    def _audit(
        self,
        *,
        org_id: uuid.UUID,
        owner_account_id: uuid.UUID | None,
        session_id: uuid.UUID | None,
        event_type: str,
        outcome: str,
        reason_code: str | None,
        correlation_id: uuid.UUID,
        occurred_at: datetime,
    ) -> None:
        self.session.add(
            IdentityAuditEvent(
                org_id=org_id,
                owner_account_id=owner_account_id,
                session_id=session_id,
                event_type=event_type,
                outcome=outcome,
                reason_code=reason_code,
                request_correlation_id=correlation_id,
                occurred_at=occurred_at,
            )
        )

    def _clock(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            raise RuntimeError("identity clock must return an aware datetime")
        return value.astimezone(UTC)


def _failure_backoff(failures: int, now: datetime) -> datetime | None:
    if failures < AUTH_FAILURE_THRESHOLD:
        return None
    exponent = failures - AUTH_FAILURE_THRESHOLD
    delay = AUTH_FAILURE_BACKOFF_BASE * (2**exponent)
    return now + min(delay, AUTH_FAILURE_BACKOFF_MAX)


def _normalizable_authentication_password(value: str) -> str | None:
    """Normalize valid password input while collapsing malformed input to login failure."""

    try:
        return normalize_authentication_password(value)
    except IdentityError:
        return None


def _is_throttled(throttled_until: datetime | None, now: datetime) -> bool:
    return throttled_until is not None and now < _as_utc(throttled_until)


def _session_expiry_reason(
    owner_session: OwnerSession, account: OwnerAccount, now: datetime
) -> tuple[str, str] | None:
    if owner_session.revoked_at is not None:
        return ("logout", "SESSION_REVOKED")
    if account.status != "active":
        return ("credential_changed", "SESSION_REVOKED")
    if owner_session.credential_version != account.credential_version:
        return ("credential_version_mismatch", "SESSION_CREDENTIAL_VERSION_MISMATCH")
    if now >= _as_utc(owner_session.absolute_expires_at):
        return ("absolute_expired", "SESSION_ABSOLUTE_EXPIRED")
    if now >= _as_utc(owner_session.idle_expires_at):
        return ("idle_expired", "SESSION_IDLE_EXPIRED")
    return None


def _not_before(candidate: datetime, existing: datetime) -> datetime:
    return max(candidate, _as_utc(existing))


def _strictly_after(candidate: datetime, existing: datetime) -> datetime:
    return max(candidate, _as_utc(existing) + timedelta(microseconds=1))


def _as_utc(value: datetime) -> datetime:
    """Accommodate SQLite's timezone-less test round-trip without ambiguity."""

    return value.replace(tzinfo=UTC) if value.tzinfo is None else value.astimezone(UTC)
