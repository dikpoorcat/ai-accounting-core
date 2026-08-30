from __future__ import annotations

import sys
import uuid
from argparse import Namespace
from datetime import date

import pytest
from pydantic import SecretStr
from sqlalchemy import func, select

from ai_accounting import identity_cli
from ai_accounting.accounting_period_schemas import (
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
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
from ai_accounting.models import (
    AccountingPeriodCloseApproval,
    Evidence,
    IdentityAuditEvent,
    OwnerAccount,
)


def test_cli_failed_login_commits_throttle_and_fixed_audit_across_sessions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    store = InMemoryCredentialStore()
    try:
        with factory.begin() as session:
            organization = seed_organization(
                session,
                taxpayer_identification_number="91330106MA1234567T",
                name="CLI failure persistence",
            )
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


def test_cli_approve_close_reauthenticates_and_binds_exact_preview_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    store = InMemoryCredentialStore()
    password = "Owner-Explicit-Close-Approval-2026!"
    try:
        with factory.begin() as session:
            organization = seed_organization(
                session,
                taxpayer_identification_number="91330106MA1234567T",
                name="CLI owner close approval",
            )
            evidence = Evidence(
                org_id=organization.id,
                sha256="c" * 64,
                original_name="period-generation.txt",
                media_type="text/plain",
                source="test",
                size_bytes=1,
                storage_path="test/period-generation.txt",
            )
            session.add(evidence)
            session.flush()
            generated = AccountingPeriodService(session).generate_accounting_period(
                GenerateAccountingPeriodRequest(
                    org_id=organization.id,
                    period_month="2026-07",
                    idempotency_key="generate-close-approval-test",
                    confirmation_note="test period",
                    evidence_references=[evidence.id],
                )
            )
            assert generated.status.value == "posted"
            IdentityService(session).provision_owner(
                OwnerProvisionRequest(
                    org_id=organization.id,
                    login_name="close-owner",
                    password=SecretStr(password),
                )
            )
            org_id = organization.id
            period_id = generated.period_id
        assert period_id is not None
        with factory() as session:
            preview = AccountingPeriodService(session).preview_accounting_period_close(
                PreviewAccountingPeriodCloseRequest(
                    org_id=org_id,
                    period_id=period_id,
                    closing_date=date(2026, 7, 31),
                )
            )
        assert preview.calculation_hash is not None
        monkeypatch.setattr(identity_cli, "SessionLocal", factory)
        monkeypatch.setattr(identity_cli, "WindowsCredentialStore", lambda: store)
        monkeypatch.setattr(identity_cli, "_secret_prompt", lambda _prompt: password)

        identity_cli._approve_close(
            Namespace(
                org_id=org_id,
                period_id=period_id,
                calculation_hash=preview.calculation_hash,
                login_name="close-owner",
            )
        )

        with factory() as session:
            approval = session.scalar(select(AccountingPeriodCloseApproval))
            assert approval is not None
            assert approval.period_id == period_id
            assert approval.calculation_hash == preview.calculation_hash
            assert approval.confirmation_method == "local_password_reauthentication"
            assert approval.consumed_at is None
        assert store.load_session_token() is not None
    finally:
        engine.dispose()


def test_cli_close_approval_window_uses_dedicated_launcher(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[dict[str, str]] = []

    class _Launcher:
        def request(self, **kwargs: str) -> bool:
            calls.append(kwargs)
            return True

    monkeypatch.setattr(identity_cli, "OwnerCloseApprovalWindowLauncher", _Launcher)
    org_id = uuid.uuid4()
    period_id = uuid.uuid4()
    calculation_hash = "a" * 64

    identity_cli._approve_close_window(
        Namespace(
            org_id=org_id,
            period_id=period_id,
            calculation_hash=calculation_hash,
            login_name="owner",
        )
    )

    assert calls == [
        {
            "org_id": str(org_id),
            "period_id": str(period_id),
            "calculation_hash": calculation_hash,
            "login_name": "owner",
        }
    ]


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
