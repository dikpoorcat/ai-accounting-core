from __future__ import annotations

import sys
import uuid
from argparse import Namespace

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from ai_accounting import identity_cli
from ai_accounting.coa import seed_organization
from ai_accounting.credential_store import (
    InMemoryCredentialStore,
    WindowsCredentialStore,
    _assert_windows_credential_layout,
)
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.identity import IdentityError
from ai_accounting.identity_schemas import OwnerProvisionRequest
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import IdentityAuditEvent, OwnerAccount


def test_cli_failed_login_commits_throttle_and_fixed_audit_across_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    store = InMemoryCredentialStore()
    try:
        with factory.begin() as session:
            organization = seed_organization(session, name="CLI failure persistence")
            IdentityService(session).provision_owner(
                OwnerProvisionRequest(
                    org_id=organization.id,
                    login_name="owner",
                    password=SecretStr("Correct-Horse-Battery-2026!"),
                )
            )
        monkeypatch.setattr(identity_cli, "SessionLocal", factory)
        monkeypatch.setattr(identity_cli, "WindowsCredentialStore", lambda: store)
        monkeypatch.setattr(
            identity_cli,
            "_secret_prompt",
            lambda _prompt: "Wrong-Horse-Battery-2026!",
        )

        for _ in range(5):
            with pytest.raises(IdentityError, match="IDENTITY_AUTHENTICATION_FAILED"):
                identity_cli._login(Namespace(login_name="owner"))

        with factory() as session:
            account = session.scalar(select(OwnerAccount))
            assert account is not None
            assert account.password_failed_attempts == 5
            assert account.password_throttled_until is not None
            assert session.scalar(
                select(func.count()).select_from(IdentityAuditEvent).where(
                    IdentityAuditEvent.event_type == "login_failed"
                )
            ) == 5

        with pytest.raises(IdentityError, match="IDENTITY_AUTHENTICATION_FAILED"):
            identity_cli._login(Namespace(login_name="unknown-owner"))
        with factory() as session:
            assert session.scalar(
                select(func.count()).select_from(IdentityAuditEvent).where(
                    IdentityAuditEvent.event_type == "login_failed"
                )
            ) == 6
    finally:
        engine.dispose()


def test_cli_setup_surfaces_password_policy_as_stable_identity_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(identity_cli, "_secret_prompt", lambda _prompt: "12345")

    with pytest.raises(IdentityError, match="IDENTITY_PASSWORD_POLICY_REJECTED"):
        identity_cli._setup(
            Namespace(
                org_id=uuid.uuid4(),
                login_name="owner",
            )
        )


@pytest.mark.skipif(sys.platform != "win32", reason="Windows Credential Manager only")
def test_windows_credential_store_abi_and_unique_target_round_trip() -> None:
    _assert_windows_credential_layout()
    target = f"ai-accounting-core/test-session/{uuid.uuid4()}"
    store = WindowsCredentialStore(target_name=target)
    previous = store.load_session_token()
    try:
        expected = SecretStr("test-only-opaque-session-token")
        store.save_session_token(expected)
        loaded = store.load_session_token()
        assert loaded is not None
        assert loaded.get_secret_value() == expected.get_secret_value()
        store.delete_session_token()
        assert store.load_session_token() is None
    finally:
        if previous is None:
            store.delete_session_token()
        else:
            store.save_session_token(previous)
