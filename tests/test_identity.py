from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import select
from sqlalchemy.orm import Session

from ai_accounting.coa import seed_organization
from ai_accounting.identity import (
    ARGON2_HASH_LEN,
    ARGON2_MEMORY_COST_KIB,
    ARGON2_PARALLELISM,
    ARGON2_SALT_LEN,
    ARGON2_TIME_COST,
    PASSWORD_MAX_LENGTH,
    PASSWORD_MIN_LENGTH,
    ExecutorIdentity,
    ExecutorKind,
    IdentityError,
    hash_password,
    new_recovery_code,
    new_session_token,
    normalize_authentication_password,
    normalize_password,
    normalize_recovery_code,
    recovery_code_matches,
    recovery_code_sha256,
    token_sha256,
    validate_password_for_login,
    verify_password,
)
from ai_accounting.identity_schemas import (
    OwnerLoginRequest,
    OwnerProvisionRequest,
    OwnerRecoveryResetRequest,
)
from ai_accounting.identity_service import (
    AUTH_FAILURE_BACKOFF_BASE,
    SESSION_ABSOLUTE_TIMEOUT,
    SESSION_IDLE_TIMEOUT,
    IdentityService,
)
from ai_accounting.models import IdentityAuditEvent, OwnerAccount, OwnerRecoveryCode, OwnerSession


def _bytes(value: int):
    def generate(length: int) -> bytes:
        return bytes([value]) * length

    return generate


def test_argon2id_parameters_and_password_verification_are_fixed() -> None:
    password = "Correct-Horse-Battery-2026!"

    password_hash = hash_password(password)

    expected_parameters = (
        f"m={ARGON2_MEMORY_COST_KIB},t={ARGON2_TIME_COST},p={ARGON2_PARALLELISM}"
    )
    assert expected_parameters in password_hash
    assert password_hash.startswith("$argon2id$v=19$")
    assert verify_password(password_hash=password_hash, password=password)
    assert not verify_password(password_hash=password_hash, password="Wrong-Horse-Battery-2026!")
    assert ARGON2_HASH_LEN == 32
    assert ARGON2_SALT_LEN == 16


@pytest.mark.parametrize(
    "password",
    [
        "12345",
        "x" * 129,
        "12345\x00",
    ],
)
def test_password_policy_rejects_only_invalid_input_bounds(password: str) -> None:
    with pytest.raises(IdentityError, match="IDENTITY_PASSWORD_POLICY_REJECTED"):
        normalize_password(password)

    assert PASSWORD_MIN_LENGTH == 6
    assert PASSWORD_MAX_LENGTH == 128


@pytest.mark.parametrize("password", ["123456", "passwordpassword", "owner-owner"])
def test_password_policy_accepts_any_six_or_more_character_password(password: str) -> None:
    assert normalize_password(password) == password


def test_password_policy_allows_six_character_password_matching_login_name() -> None:
    assert validate_password_for_login(password="123456", login_name="123456") == "123456"


def test_password_policy_normalizes_nfc_without_echoing_secret() -> None:
    decomposed = "Cafe\u0301-Correct-Horse-2026!"

    normalized = normalize_password(decomposed)

    assert normalized == "Café-Correct-Horse-2026!"


def test_login_and_new_password_normalization_use_the_same_length_policy() -> None:
    assert normalize_authentication_password("passwordpassword") == "passwordpassword"
    assert normalize_password("passwordpassword") == "passwordpassword"


def test_session_token_is_deterministic_with_injected_csprng_and_only_hashes_for_storage() -> None:
    token = new_session_token(_bytes(7))

    assert token
    assert token_sha256(token) == hashlib.sha256(token.encode("ascii")).hexdigest()
    assert token not in token_sha256(token)


def test_private_pilot_session_timeouts_balance_continuity_and_reauthentication() -> None:
    assert SESSION_IDLE_TIMEOUT.days == 7
    assert SESSION_ABSOLUTE_TIMEOUT.days == 30


def test_recovery_code_is_128_bit_grouped_and_matches_only_its_hash() -> None:
    code = new_recovery_code(_bytes(1))
    compact = normalize_recovery_code(code)

    assert code.count("-") == 5
    assert len(compact) == 26
    assert recovery_code_matches(
        expected_sha256=recovery_code_sha256(code),
        supplied_code=compact.lower(),
    )
    assert not recovery_code_matches(
        expected_sha256=recovery_code_sha256(code),
        supplied_code=new_recovery_code(_bytes(2)),
    )


@pytest.mark.parametrize("code", ["not a recovery code", "1" * 26, "A" * 27])
def test_recovery_code_rejects_malformed_input(code: str) -> None:
    with pytest.raises(IdentityError, match="IDENTITY_RECOVERY_CODE_INVALID"):
        normalize_recovery_code(code)


def test_identity_request_models_redact_secrets_and_forbid_extra_input() -> None:
    request = OwnerProvisionRequest(
        org_id="00000000-0000-0000-0000-000000000001",
        login_name="owner",
        password=SecretStr("Correct-Horse-Battery-2026!"),
    )

    assert "Correct-Horse-Battery-2026!" not in repr(request)
    assert request.password.get_secret_value() == "Correct-Horse-Battery-2026!"
    with pytest.raises(ValidationError):
        OwnerLoginRequest(
            login_name="owner",
            password=SecretStr("Correct-Horse-Battery-2026!"),
            ignored="forbidden",
        )


class _Clock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 11, 0, 0, tzinfo=UTC)

    def now(self) -> datetime:
        return self.value


class _SequenceRng:
    def __init__(self) -> None:
        self.value = 16

    def bytes(self, length: int) -> bytes:
        self.value += 1
        return bytes([self.value]) * length


def _owner_request(
    org_id: uuid.UUID, *, password: str = "Correct-Horse-Battery-2026!"
) -> OwnerProvisionRequest:
    return OwnerProvisionRequest(org_id=org_id, login_name="owner", password=SecretStr(password))


def _service(session: Session, clock: _Clock) -> IdentityService:
    return IdentityService(session, now=clock.now, randbytes=_SequenceRng().bytes)


def test_six_character_password_can_provision_and_authenticate(session: Session) -> None:
    organization = seed_organization(session, name="六位密码登录")
    service = _service(session, _Clock())

    provisioned = service.provision_owner(_owner_request(organization.id, password="123456"))
    authenticated = service.authenticate(
        OwnerLoginRequest(login_name="owner", password=SecretStr("123456"))
    )

    assert authenticated.owner_account_id == provisioned.owner_account_id


def test_sqlite_owner_login_stores_only_hashes_and_separates_execution_attribution(
    session: Session,
) -> None:
    organization = seed_organization(session, name="身份 SQLite 服务")
    clock = _Clock()
    service = _service(session, clock)

    provisioned = service.provision_owner(_owner_request(organization.id))
    authenticated = service.authenticate(
        OwnerLoginRequest(
            login_name="OWNER",
            password=SecretStr("Correct-Horse-Battery-2026!"),
        )
    )
    context = service.authorize_execution(
        session_token=authenticated.session_token.get_secret_value(),
        executor=ExecutorIdentity(
            kind=ExecutorKind.AI_AGENT,
            executor_name="local-ai-agent",
            executor_version="v1",
        ),
        request_correlation_id=uuid.uuid4(),
    )
    account = session.get(OwnerAccount, provisioned.owner_account_id)
    stored_session = session.get(OwnerSession, authenticated.session_id)
    stored_code = session.scalar(select(OwnerRecoveryCode))

    assert account is not None
    assert stored_session is not None
    assert stored_code is not None
    assert account.login_name_normalized == "owner"
    assert account.password_hash != "Correct-Horse-Battery-2026!"
    assert authenticated.session_token.get_secret_value() not in stored_session.secret_sha256
    assert provisioned.recovery_code.get_secret_value() not in stored_code.code_sha256
    assert context.owner_account_id == account.id
    assert context.executor_kind is ExecutorKind.AI_AGENT
    assert context.executor_name == "local-ai-agent"
    assert context.executor_version == "v1"
    assert len(session.scalars(select(IdentityAuditEvent)).all()) == 2


def test_sqlite_failures_are_generic_and_use_incremental_backoff(session: Session) -> None:
    organization = seed_organization(session, name="身份登录限速")
    clock = _Clock()
    service = _service(session, clock)
    service.provision_owner(_owner_request(organization.id))
    bad_request = OwnerLoginRequest(
        login_name="owner", password=SecretStr("Wrong-Horse-Battery-2026!")
    )

    for _ in range(5):
        with pytest.raises(IdentityError, match="IDENTITY_AUTHENTICATION_FAILED"):
            service.authenticate(bad_request)
    account = session.scalar(select(OwnerAccount))
    assert account is not None
    assert account.password_failed_attempts == 5
    assert account.password_throttled_until is not None
    assert account.password_throttled_until.replace(tzinfo=UTC) == (
        clock.value + AUTH_FAILURE_BACKOFF_BASE
    )
    clock.value += AUTH_FAILURE_BACKOFF_BASE
    with pytest.raises(IdentityError, match="IDENTITY_AUTHENTICATION_FAILED"):
        service.authenticate(bad_request)
    assert account.password_failed_attempts == 6
    assert account.password_throttled_until is not None
    assert account.password_throttled_until.replace(tzinfo=UTC) == (
        clock.value + AUTH_FAILURE_BACKOFF_BASE * 2
    )


def test_sqlite_recovery_consumes_code_revokes_sessions_and_issues_one_replacement(
    session: Session,
) -> None:
    organization = seed_organization(session, name="身份恢复")
    clock = _Clock()
    service = _service(session, clock)
    provisioned = service.provision_owner(_owner_request(organization.id))
    logged_in = service.authenticate(
        OwnerLoginRequest(
            login_name="owner",
            password=SecretStr("Correct-Horse-Battery-2026!"),
        )
    )
    reset = service.reset_password_with_recovery(
        OwnerRecoveryResetRequest(
            login_name="owner",
            recovery_code=provisioned.recovery_code,
            new_password=SecretStr("Updated-Horse-Battery-2026!"),
        )
    )
    account = session.get(OwnerAccount, provisioned.owner_account_id)
    sessions = session.scalars(select(OwnerSession)).all()
    codes = session.scalars(select(OwnerRecoveryCode).order_by(OwnerRecoveryCode.created_at)).all()

    assert account is not None
    assert account.credential_version == 2
    assert all(
        row.revoked_at is not None and row.revoke_reason == "recovery_used" for row in sessions
    )
    assert len(codes) == 2
    assert codes[0].used_at is not None
    assert codes[0].invalidated_at is None
    assert codes[1].used_at is None and codes[1].invalidated_at is None
    assert reset.recovery_code.get_secret_value() != provisioned.recovery_code.get_secret_value()
    with pytest.raises(IdentityError, match="IDENTITY_SESSION_INVALID"):
        service.authorize_execution(
            session_token=logged_in.session_token.get_secret_value(),
            executor=ExecutorIdentity(
                kind=ExecutorKind.DETERMINISTIC_KERNEL,
                executor_name="deterministic-kernel",
                executor_version="v1",
            ),
            request_correlation_id=uuid.uuid4(),
        )
    new_session = service.authenticate(
        OwnerLoginRequest(
            login_name="owner",
            password=SecretStr("Updated-Horse-Battery-2026!"),
        )
    )
    assert new_session.owner_account_id == provisioned.owner_account_id


def test_sqlite_session_absolute_expiry_is_not_extended_by_activity(session: Session) -> None:
    organization = seed_organization(session, name="身份会话超时")
    clock = _Clock()
    service = _service(session, clock)
    service.provision_owner(_owner_request(organization.id))
    authenticated = service.authenticate(
        OwnerLoginRequest(
            login_name="owner",
            password=SecretStr("Correct-Horse-Battery-2026!"),
        )
    )
    clock.value += SESSION_ABSOLUTE_TIMEOUT

    with pytest.raises(IdentityError, match="IDENTITY_SESSION_INVALID"):
        service.authorize_execution(
            session_token=authenticated.session_token.get_secret_value(),
            executor=ExecutorIdentity(
                kind=ExecutorKind.DETERMINISTIC_KERNEL,
                executor_name="deterministic-kernel",
                executor_version="v1",
            ),
            request_correlation_id=uuid.uuid4(),
        )

    stored_session = session.get(OwnerSession, authenticated.session_id)
    assert stored_session is not None
    assert stored_session.revoke_reason == "absolute_expired"
