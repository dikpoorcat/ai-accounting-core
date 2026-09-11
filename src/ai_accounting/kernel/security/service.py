"""Single-owner catalogue authentication, independent of business calculations.

Secret results are for the native window/service channel only. Public MCP and
CLI commands must never serialize them. The host retains ``authorized`` (or the
same authorization_gate plus validate_authority) through a business commit.
"""

from __future__ import annotations

import hashlib
import re
import threading
import uuid
from contextlib import closing, contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import SecretStr

from ..runtime import connect
from .primitives import (
    IdentityError,
    hash_password,
    new_recovery_code,
    new_session_token,
    normalize_authentication_password,
    normalize_login_name,
    normalized_login_key,
    recovery_code_matches,
    recovery_code_sha256,
    token_sha256,
    verify_password,
)

SECOND = 1_000_000
SESSION_IDLE_TIMEOUT = 7 * 24 * 60 * 60 * SECOND
SESSION_ABSOLUTE_TIMEOUT = 30 * 24 * 60 * 60 * SECOND
AUTH_FAILURE_THRESHOLD = 5
AUTH_FAILURE_BACKOFF_BASE = 30 * SECOND
AUTH_FAILURE_BACKOFF_MAX = 15 * 60 * SECOND
_GATES: dict[str, threading.RLock] = {}
_GATES_LOCK = threading.Lock()
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)
# An invalid account still follows the same Argon2 verification path. This is a
# public, synthetic comparison value, never a credential of any owner.
_DUMMY_PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$998d56tE5yS24cuu8oNNOw$"
    "OhE9v7CVWzFi2OYdDJmq/R6CyvmmSA1OsccIYczieFE"
)


@dataclass(frozen=True, slots=True)
class Authority:
    catalog_instance_id: str
    owner_id: str
    session_id: str
    credential_version: int


@dataclass(frozen=True, slots=True)
class LoginResult:
    authority: Authority
    session_token: SecretStr


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    owner_id: str
    recovery_code: SecretStr


def secret_text(value) -> str:
    if isinstance(value, SecretStr):
        return value.get_secret_value()
    if isinstance(value, str):
        return value
    raise IdentityError("IDENTITY_SECRET_INVALID")


def utc_microseconds(value: datetime) -> int:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise IdentityError("IDENTITY_CLOCK_INVALID")
    return (value.astimezone(UTC) - _EPOCH) // timedelta(microseconds=1)


def credential_target(catalog_path, catalog_instance_id: str) -> str:
    # A copied catalogue at another path cannot silently reuse the old session.
    binding = str(Path(catalog_path).resolve()).casefold() + "\0" + catalog_instance_id
    return (
        "ai-accounting-core/local-owner-session/v3/" + hashlib.sha256(binding.encode()).hexdigest()
    )


def _request_id(value):
    if value is None:
        return uuid.uuid4().hex
    if not isinstance(value, str) or not 1 <= len(value) <= 200:
        raise IdentityError("IDENTITY_REQUEST_INVALID")
    return value


class SecurityService:
    def __init__(self, catalog_path, *, clock=None, random_bytes=None, catalog_validator=None):
        self.path = Path(catalog_path).resolve()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.random_bytes = random_bytes
        if catalog_validator is None:
            from ..versions import verify_schema

            def catalog_validator(connection):
                verify_schema(connection, kind="catalog")

        self.catalog_validator = catalog_validator
        with _GATES_LOCK:
            self.authorization_gate = _GATES.setdefault(
                str(self.path).casefold(), threading.RLock()
            )
        # Opening a missing/unmigrated catalogue is an error, never provisioning.
        try:
            with closing(connect(self.path, read_only=True)) as connection:
                self.catalog_validator(connection)
                row = connection.execute(
                    "SELECT instance_id FROM catalog_identity WHERE id=1"
                ).fetchone()
                if row is None or not row[0]:
                    raise IdentityError("IDENTITY_CATALOG_UNAVAILABLE")
                self.catalog_instance_id = row[0]
                for table in (
                    "security_owner",
                    "security_session",
                    "security_recovery",
                    "security_audit",
                ):
                    connection.execute(f"SELECT 1 FROM {table} LIMIT 0")
        except IdentityError:
            raise
        except Exception:
            raise IdentityError("IDENTITY_CATALOG_UNAVAILABLE") from None

    @property
    def credential_target(self):
        return credential_target(self.path, self.catalog_instance_id)

    def now(self):
        return utc_microseconds(self.clock())

    def _check_catalog(self, connection):
        self.catalog_validator(connection)
        row = connection.execute("SELECT instance_id FROM catalog_identity WHERE id=1").fetchone()
        if row is None or row[0] != self.catalog_instance_id:
            raise IdentityError("OWNER_SECURITY_TARGET_MISMATCH")

    @contextmanager
    def _transaction(self):
        with (
            self.authorization_gate,
            closing(connect(self.path, validator=self._check_catalog)) as connection,
        ):
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._check_catalog(connection)
                yield connection
            except IdentityError as exc:
                # Authentication rejection may have incremented throttle counters.
                # Other validation failures must roll back partial successful work.
                if exc.code not in {
                    "IDENTITY_AUTHENTICATION_FAILED",
                    "IDENTITY_RECOVERY_FAILED",
                    "IDENTITY_SESSION_INVALID",
                }:
                    connection.rollback()
                    raise
                try:
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise
                raise
            except BaseException:
                connection.rollback()
                raise
            else:
                try:
                    connection.commit()
                except BaseException:
                    connection.rollback()
                    raise

    @staticmethod
    def _owner(connection):
        return connection.execute("SELECT * FROM security_owner WHERE singleton=1").fetchone()

    def status(self):
        with self.authorization_gate, closing(connect(self.path, read_only=True)) as connection:
            self._check_catalog(connection)
            row = self._owner(connection)
            return {
                "catalog_instance_id": self.catalog_instance_id,
                "provisioned": row is not None,
                "login_name": row["login_name"] if row else None,
                "active": row is not None and row["status"] == "active",
            }

    def _audit(
        self,
        connection,
        event,
        now,
        *,
        owner=None,
        session=None,
        outcome="succeeded",
        reason=None,
        request_id=None,
    ):
        connection.execute(
            "INSERT INTO security_audit(occurred_at,owner_id,session_id,event,outcome,reason,"
            "request_id) VALUES(?,?,?,?,?,?,?)",
            (now, owner, session, event, outcome, reason, _request_id(request_id)),
        )

    def _new_recovery(self, connection, owner_id, version, now):
        code = new_recovery_code(self.random_bytes) if self.random_bytes else new_recovery_code()
        connection.execute(
            "UPDATE security_recovery SET invalidated_at=max(?,created_at) WHERE owner_id=? AND "
            "used_at IS NULL AND invalidated_at IS NULL",
            (now, owner_id),
        )
        connection.execute(
            "INSERT INTO security_recovery(id,owner_id,code_hash,credential_version,created_at) "
            "VALUES(?,?,?,?,?)",
            (uuid.uuid4().hex, owner_id, bytes.fromhex(recovery_code_sha256(code)), version, now),
        )
        return RecoveryResult(owner_id, SecretStr(code))

    def provision(self, login_name: str, password, *, request_id=None):
        name = normalize_login_name(login_name)
        prepared = normalize_authentication_password(secret_text(password))
        encoded = hash_password(prepared)
        with self._transaction() as connection:
            if self._owner(connection):
                raise IdentityError("IDENTITY_OWNER_ALREADY_PROVISIONED")
            now, owner_id = self.now(), uuid.uuid4().hex
            connection.execute(
                "INSERT INTO security_owner(id,singleton,login_name,login_key,status,"
                "password_hash,credential_version,password_failures,recovery_failures,created_at,"
                "password_changed_at) VALUES(?,1,?,?,'active',?,1,0,0,?,?)",
                (owner_id, name, normalized_login_key(name), encoded, now, now),
            )
            result = self._new_recovery(connection, owner_id, 1, now)
            self._audit(connection, "owner_provisioned", now, owner=owner_id, request_id=request_id)
            return result

    def _failure(self, connection, owner, now, *, recovery=False, request_id=None):
        prefix = "recovery" if recovery else "password"
        count = owner[f"{prefix}_failures"] + 1
        # Cap the exponent before computing it; arbitrarily many failed attempts
        # must not allocate an enormous integer just to apply the same delay cap.
        delay = (
            None
            if count < AUTH_FAILURE_THRESHOLD
            else min(
                AUTH_FAILURE_BACKOFF_BASE * (2 ** min(count - AUTH_FAILURE_THRESHOLD, 5)),
                AUTH_FAILURE_BACKOFF_MAX,
            )
        )
        blocked = now + delay if delay is not None else None
        connection.execute(
            f"UPDATE security_owner SET {prefix}_failures=?,{prefix}_blocked_until=? WHERE id=?",
            (count, blocked, owner["id"]),
        )
        self._audit(
            connection,
            "recovery_failed" if recovery else "login_failed",
            now,
            owner=owner["id"],
            outcome="rejected",
            reason="INVALID_CREDENTIALS",
            request_id=request_id,
        )

    def _password_check(self, connection, owner, password, now, *, request_id=None):
        try:
            prepared = normalize_authentication_password(secret_text(password))
        except IdentityError:
            prepared = None
        blocked = owner is not None and (
            owner["status"] != "active" or (owner["password_blocked_until"] or 0) > now
        )
        if blocked:
            self._audit(
                connection,
                "login_failed",
                now,
                owner=owner["id"],
                outcome="blocked",
                reason="AUTHENTICATION_THROTTLED",
                request_id=request_id,
            )
            raise IdentityError("IDENTITY_AUTHENTICATION_FAILED")
        encoded = (
            owner["password_hash"]
            if owner is not None and prepared is not None
            else _DUMMY_PASSWORD_HASH
        )
        matched = verify_password(password_hash=encoded, password=prepared or "")
        if owner is None or prepared is None or not matched:
            if owner is not None:
                self._failure(connection, owner, now, request_id=request_id)
            else:
                self._audit(
                    connection,
                    "login_failed",
                    now,
                    outcome="rejected",
                    reason="INVALID_CREDENTIALS",
                    request_id=request_id,
                )
            raise IdentityError("IDENTITY_AUTHENTICATION_FAILED")
        connection.execute(
            "UPDATE security_owner SET password_failures=0,password_blocked_until=NULL,"
            "last_authenticated_at=? WHERE id=?",
            (now, owner["id"]),
        )

    def login(self, login_name: str, password, *, request_id=None):
        try:
            key = normalized_login_key(login_name)
        except IdentityError:
            key = None
        with self._transaction() as connection:
            owner = self._owner(connection)
            if owner is not None and owner["login_key"] != key:
                owner = None
            now = self.now()
            self._password_check(connection, owner, password, now, request_id=request_id)
            token = (
                new_session_token(self.random_bytes) if self.random_bytes else new_session_token()
            )
            session_id = uuid.uuid4().hex
            connection.execute(
                "INSERT INTO security_session(id,owner_id,secret_hash,credential_version,"
                "created_at,last_seen_at,idle_expires_at,absolute_expires_at) VALUES(?,?,?,?,?,?,"
                "?,?)",
                (
                    session_id,
                    owner["id"],
                    bytes.fromhex(token_sha256(token)),
                    owner["credential_version"],
                    now,
                    now,
                    now + SESSION_IDLE_TIMEOUT,
                    now + SESSION_ABSOLUTE_TIMEOUT,
                ),
            )
            authority = Authority(
                self.catalog_instance_id, owner["id"], session_id, owner["credential_version"]
            )
            self._audit(
                connection,
                "login_succeeded",
                now,
                owner=owner["id"],
                session=session_id,
                request_id=request_id,
            )
            return LoginResult(authority, SecretStr(token))

    authenticate = login

    @staticmethod
    def _token_hash(token):
        value = secret_text(token)
        if not re.fullmatch(r"[A-Za-z0-9_-]{43}", value):
            raise IdentityError("IDENTITY_SESSION_INVALID")
        return bytes.fromhex(token_sha256(value))

    def _check_session(self, connection, session, owner, now, *, request_id=None):
        if session is None or owner is None or session["owner_id"] != owner["id"]:
            raise IdentityError("IDENTITY_SESSION_INVALID")
        reason = (
            "logout"
            if session["revoked_at"] is not None
            else "credential_changed"
            if owner["status"] != "active"
            else "credential_version_mismatch"
            if session["credential_version"] != owner["credential_version"]
            else "absolute_expired"
            if now >= session["absolute_expires_at"]
            else "idle_expired"
            if now >= session["idle_expires_at"]
            else None
        )
        if reason:
            if session["revoked_at"] is None:
                connection.execute(
                    "UPDATE security_session SET revoked_at=max(?,created_at),revoke_reason=? "
                    "WHERE id=?",
                    (now, reason, session["id"]),
                )
                self._audit(
                    connection,
                    "session_expired",
                    now,
                    owner=owner["id"],
                    session=session["id"],
                    outcome="rejected",
                    reason=reason,
                    request_id=request_id,
                )
            raise IdentityError("IDENTITY_SESSION_INVALID")
        seen = max(now, session["last_seen_at"])
        connection.execute(
            "UPDATE security_session SET last_seen_at=?,idle_expires_at=? WHERE id=?",
            (seen, min(seen + SESSION_IDLE_TIMEOUT, session["absolute_expires_at"]), session["id"]),
        )
        return Authority(
            self.catalog_instance_id, owner["id"], session["id"], owner["credential_version"]
        )

    def _authorize(self, connection, token, *, request_id=None):
        session = connection.execute(
            "SELECT * FROM security_session WHERE secret_hash=?", (self._token_hash(token),)
        ).fetchone()
        return self._check_session(
            connection, session, self._owner(connection), self.now(), request_id=request_id
        )

    def authorize(self, token, *, request_id=None):
        with self._transaction() as connection:
            return self._authorize(connection, token, request_id=request_id)

    @contextmanager
    def authorized(self, token, *, request_id=None):
        """Keep revocation/password rotation ordered with the caller's commit."""
        with self.authorization_gate:
            yield self.authorize(token, request_id=request_id)

    def validate_authority(self, authority: Authority):
        if (
            not isinstance(authority, Authority)
            or authority.catalog_instance_id != self.catalog_instance_id
        ):
            raise IdentityError("IDENTITY_SESSION_INVALID")
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT * FROM security_session WHERE id=?", (authority.session_id,)
            ).fetchone()
            if session is None or (
                session["owner_id"] != authority.owner_id
                or session["credential_version"] != authority.credential_version
            ):
                raise IdentityError("IDENTITY_SESSION_INVALID")
            actual = self._check_session(connection, session, self._owner(connection), self.now())
            if actual != authority:
                raise IdentityError("IDENTITY_SESSION_INVALID")
            return actual

    def logout(self, token, *, request_id=None):
        try:
            hashed = self._token_hash(token)
        except IdentityError:
            return {"status": "logged_out"}
        with self._transaction() as connection:
            session = connection.execute(
                "SELECT * FROM security_session WHERE secret_hash=?", (hashed,)
            ).fetchone()
            if session is not None and session["revoked_at"] is None:
                now = max(self.now(), session["created_at"])
                connection.execute(
                    "UPDATE security_session SET revoked_at=?,revoke_reason='logout' WHERE id=?",
                    (now, session["id"]),
                )
                self._audit(
                    connection,
                    "session_revoked",
                    now,
                    owner=session["owner_id"],
                    session=session["id"],
                    request_id=request_id,
                )
        return {"status": "logged_out"}

    def _rotate(self, connection, owner, new_hash, now, reason, request_id):
        now = max(now, owner["password_changed_at"] + 1)
        version = owner["credential_version"] + 1
        connection.execute(
            "UPDATE security_owner SET password_hash=?,credential_version=?,"
            "password_changed_at=?,password_failures=0,password_blocked_until=NULL,"
            "recovery_failures=0,recovery_blocked_until=NULL WHERE id=?",
            (new_hash, version, now, owner["id"]),
        )
        connection.execute(
            "UPDATE security_session SET revoked_at=max(?,created_at),revoke_reason=? WHERE "
            "owner_id=? AND revoked_at IS NULL",
            (now, reason, owner["id"]),
        )
        result = self._new_recovery(connection, owner["id"], version, now)
        self._audit(
            connection,
            "password_recovered" if reason == "recovery_used" else "password_changed",
            now,
            owner=owner["id"],
            request_id=request_id,
        )
        return result

    def change_password(self, token, current_password, new_password, *, request_id=None):
        prepared = hash_password(normalize_authentication_password(secret_text(new_password)))
        with self._transaction() as connection:
            self._authorize(connection, token, request_id=request_id)
            owner, now = self._owner(connection), self.now()
            self._password_check(connection, owner, current_password, now, request_id=request_id)
            return self._rotate(connection, owner, prepared, now, "credential_changed", request_id)

    def recover(self, login_name, recovery_code, new_password, *, request_id=None):
        prepared = hash_password(normalize_authentication_password(secret_text(new_password)))
        try:
            key = normalized_login_key(login_name)
        except IdentityError:
            key = None
        with self._transaction() as connection:
            owner, now = self._owner(connection), self.now()
            if owner is None or owner["login_key"] != key or owner["status"] != "active":
                self._audit(
                    connection,
                    "recovery_failed",
                    now,
                    outcome="rejected",
                    reason="INVALID_CREDENTIALS",
                    request_id=request_id,
                )
                raise IdentityError("IDENTITY_RECOVERY_FAILED")
            if (owner["recovery_blocked_until"] or 0) > now:
                self._audit(
                    connection,
                    "recovery_failed",
                    now,
                    owner=owner["id"],
                    outcome="blocked",
                    reason="RECOVERY_THROTTLED",
                    request_id=request_id,
                )
                raise IdentityError("IDENTITY_RECOVERY_FAILED")
            code = connection.execute(
                "SELECT * FROM security_recovery WHERE owner_id=? AND used_at IS NULL AND "
                "invalidated_at IS NULL",
                (owner["id"],),
            ).fetchone()
            if (
                code is None
                or code["credential_version"] != owner["credential_version"]
                or not recovery_code_matches(
                    expected_sha256=code["code_hash"].hex(),
                    supplied_code=secret_text(recovery_code),
                )
            ):
                self._failure(connection, owner, now, recovery=True, request_id=request_id)
                raise IdentityError("IDENTITY_RECOVERY_FAILED")
            connection.execute(
                "UPDATE security_recovery SET used_at=max(?,created_at) WHERE id=?",
                (now, code["id"]),
            )
            return self._rotate(connection, owner, prepared, now, "recovery_used", request_id)

    reset_password_with_recovery = recover

    def replace_recovery_code(self, token, *, request_id=None):
        with self._transaction() as connection:
            authority = self._authorize(connection, token, request_id=request_id)
            now = self.now()
            result = self._new_recovery(
                connection, authority.owner_id, authority.credential_version, now
            )
            self._audit(
                connection,
                "recovery_code_replaced",
                now,
                owner=authority.owner_id,
                session=authority.session_id,
                request_id=request_id,
            )
            return result

    def reauthenticate(self, token, password, *, request_id=None):
        with self._transaction() as connection:
            authority = self._authorize(connection, token, request_id=request_id)
            now = self.now()
            self._password_check(
                connection, self._owner(connection), password, now, request_id=request_id
            )
            self._audit(
                connection,
                "password_reauthenticated",
                now,
                owner=authority.owner_id,
                session=authority.session_id,
                request_id=request_id,
            )
            return authority

    def issue_close_approval(
        self,
        connection,
        *,
        token,
        password,
        company_id,
        database_id,
        period,
        preview_digest,
        epochs,
    ):
        from .approval import insert_close_approval

        with self.authorization_gate:
            authority = self.reauthenticate(token, password)
            return insert_close_approval(
                connection,
                authority=authority,
                company_id=company_id,
                database_id=database_id,
                period=period,
                preview_digest=preview_digest,
                epochs=epochs,
                now=self.now(),
            )
