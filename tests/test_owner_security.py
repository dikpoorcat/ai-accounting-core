from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import date

import pytest
from pydantic import SecretStr, ValidationError
from sqlalchemy import func, select

from ai_accounting.accounting_period_schemas import (
    GenerateAccountingPeriodRequest,
    PreviewAccountingPeriodCloseRequest,
)
from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.coa import seed_organization
from ai_accounting.config import Settings
from ai_accounting.credential_store import InMemoryCredentialStore
from ai_accounting.database import Base, make_engine, make_session_factory
from ai_accounting.identity import IdentityError
from ai_accounting.identity_schemas import OwnerRecoveryCodeReplacementRequest
from ai_accounting.models import (
    AccountingPeriodCloseApproval,
    Evidence,
    IdentityAuditEvent,
    OwnerAccount,
    OwnerSession,
)
from ai_accounting.owner_security import OwnerSecurityOperations, OwnerSecurityWindowRequest

PASSWORD = SecretStr("Correct-Horse-Battery-2026!")
NEW_PASSWORD = SecretStr("Different-Strong-Battery-2026!")


@pytest.fixture
def security(tmp_path):
    url = "sqlite:///" + (tmp_path / "identity.db").as_posix()
    engine = make_engine(url)
    Base.metadata.create_all(engine)
    factory = make_session_factory(engine)
    with factory.begin() as session:
        org = seed_organization(
            session, taxpayer_identification_number="91330106MA1234567T", name="隔离安全窗口测试"
        )
        org_id = org.id
    settings = Settings(_env_file=None, database_url=url, finance_company_database_url=None)
    store = InMemoryCredentialStore()

    def operations():
        return OwnerSecurityOperations(settings=settings, factory=factory, store=store)

    yield operations, org_id, factory, store
    engine.dispose()


def provision(security, *, finish=True):
    operations, org_id, _, _ = security
    ops = operations()
    request = OwnerSecurityWindowRequest(kind="bootstrap_owner", org_id=org_id)
    recovery = ops.execute(request, ops.scope(), new_password=PASSWORD, repeat_password=PASSWORD)
    assert ops.operation_committed and not ops.login_completed
    if finish:
        ops.finish_recovery_display(request, ops.scope(), new_password=PASSWORD)
        assert ops.login_completed
    return recovery


def test_setup_only_provisions_once_and_logs_in_with_same_password(security):
    provision(security)
    operations, org_id, factory, store = security
    assert store.load_session_token() is not None
    with pytest.raises(IdentityError, match="IDENTITY_OWNER_ALREADY_PROVISIONED"):
        ops = operations()
        ops.execute(
            OwnerSecurityWindowRequest(kind="bootstrap_owner", org_id=org_id),
            ops.scope(),
            new_password=PASSWORD,
            repeat_password=PASSWORD,
        )
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(OwnerAccount)) == 1
        assert session.scalar(select(func.count()).select_from(OwnerSession)) == 1


@pytest.mark.parametrize(
    "repeat,code",
    [
        (SecretStr("different"), "IDENTITY_PASSWORD_CONFIRMATION_MISMATCH"),
        (SecretStr("short"), "IDENTITY_PASSWORD_POLICY_REJECTED"),
    ],
)
def test_new_password_validation_has_safe_codes(security, repeat, code):
    operations, org_id, factory, _ = security
    ops = operations()
    with pytest.raises(IdentityError, match=code):
        ops.execute(
            OwnerSecurityWindowRequest(kind="bootstrap_owner", org_id=org_id),
            ops.scope(),
            new_password=SecretStr("short"),
            repeat_password=repeat,
        )
    assert not ops.operation_committed
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(OwnerAccount)) == 0


def test_failed_authentication_commits_throttling_and_audit(security):
    provision(security)
    operations, _, factory, _ = security
    for _ in range(5):
        ops = operations()
        with pytest.raises(IdentityError, match="IDENTITY_AUTHENTICATION_FAILED"):
            ops.execute(
                OwnerSecurityWindowRequest(kind="login"),
                ops.scope(),
                password=SecretStr("Wrong-Test-Password!"),
            )
    ops = operations()
    with pytest.raises(IdentityError, match="IDENTITY_AUTHENTICATION_FAILED"):
        ops.execute(OwnerSecurityWindowRequest(kind="login"), ops.scope(), password=PASSWORD)
    with factory() as session:
        owner = session.scalar(select(OwnerAccount))
        assert owner.password_failed_attempts == 5
        assert owner.password_throttled_until is not None
        assert (
            session.scalar(
                select(func.count())
                .select_from(IdentityAuditEvent)
                .where(IdentityAuditEvent.event_type == "login_failed")
            )
            == 6
        )


def test_password_change_revokes_old_sessions_and_requires_relogin(security):
    old_recovery = provision(security)
    operations, _, _, store = security
    old_token = store.load_session_token()
    ops = operations()
    request = OwnerSecurityWindowRequest(kind="change_password")
    new_recovery = ops.execute(
        request,
        ops.scope(),
        password=PASSWORD,
        new_password=NEW_PASSWORD,
        repeat_password=NEW_PASSWORD,
    )
    assert old_recovery != new_recovery
    assert ops.operation_committed and not ops.login_completed
    with pytest.raises(IdentityError, match="IDENTITY_SESSION_INVALID"):
        operations().inspect(request, ops.scope())
    ops.finish_recovery_display(request, ops.scope())
    assert store.load_session_token() is None
    login = operations()
    login.execute(OwnerSecurityWindowRequest(kind="login"), login.scope(), password=NEW_PASSWORD)
    assert store.load_session_token() != old_token


def test_recovery_replaces_code_and_revokes_session(security):
    recovery = provision(security)
    operations, _, _, store = security
    ops = operations()
    request = OwnerSecurityWindowRequest(kind="recover")
    new_recovery = ops.execute(
        request,
        ops.scope(),
        recovery_code=recovery,
        new_password=NEW_PASSWORD,
        repeat_password=NEW_PASSWORD,
    )
    assert new_recovery != recovery
    ops.finish_recovery_display(request, ops.scope())
    assert store.load_session_token() is None
    with pytest.raises(IdentityError, match="IDENTITY_RECOVERY_FAILED"):
        operations().execute(
            request,
            ops.scope(),
            recovery_code=recovery,
            new_password=PASSWORD,
            repeat_password=PASSWORD,
        )


def test_recovery_failure_counts_are_committed(security):
    recovery = provision(security)
    operations, _, factory, _ = security
    for _ in range(5):
        ops = operations()
        with pytest.raises(IdentityError, match="IDENTITY_RECOVERY_FAILED"):
            ops.execute(
                OwnerSecurityWindowRequest(kind="recover"),
                ops.scope(),
                recovery_code=SecretStr("invalid-test-only-recovery-code"),
                new_password=NEW_PASSWORD,
                repeat_password=NEW_PASSWORD,
            )
    with pytest.raises(IdentityError, match="IDENTITY_RECOVERY_FAILED"):
        operations().execute(
            OwnerSecurityWindowRequest(kind="recover"),
            ops.scope(),
            recovery_code=recovery,
            new_password=NEW_PASSWORD,
            repeat_password=NEW_PASSWORD,
        )
    with factory() as session:
        assert session.scalar(select(OwnerAccount)).recovery_failed_attempts == 5


def test_replacement_requires_session_and_invalidates_old_recovery(security):
    recovery = provision(security)
    operations, _, _, store = security
    token = store.load_session_token()
    ops = operations()
    request = OwnerSecurityWindowRequest(kind="replace_recovery_code")
    replacement = ops.execute(request, ops.scope())
    assert replacement != recovery
    ops.finish_recovery_display(request, ops.scope())
    assert store.load_session_token() == token
    store.delete_session_token()
    with pytest.raises(IdentityError, match="IDENTITY_LOCAL_SESSION_REQUIRED"):
        operations().execute(request, ops.scope())
    with pytest.raises(IdentityError, match="IDENTITY_RECOVERY_FAILED"):
        operations().execute(
            OwnerSecurityWindowRequest(kind="recover"),
            ops.scope(),
            recovery_code=recovery,
            new_password=NEW_PASSWORD,
            repeat_password=NEW_PASSWORD,
        )


def test_setup_store_failure_is_partial_and_never_reprovisions(security, monkeypatch):
    operations, org_id, factory, store = security
    ops = operations()
    request = OwnerSecurityWindowRequest(kind="bootstrap_owner", org_id=org_id)
    ops.execute(request, ops.scope(), new_password=PASSWORD, repeat_password=PASSWORD)

    def fail(_):
        raise OSError("test-secret-must-not-escape")

    monkeypatch.setattr(store, "save_session_token", fail)
    with pytest.raises(IdentityError, match="IDENTITY_CREDENTIAL_STORE_WRITE_FAILED") as error:
        ops.finish_recovery_display(request, ops.scope(), new_password=PASSWORD)
    assert "test-secret" not in str(error.value)
    assert ops.operation_committed and not ops.login_completed
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(OwnerAccount)) == 1
        assert all(row.revoked_at is not None for row in session.scalars(select(OwnerSession)))


def test_target_change_is_rejected_before_any_password_operation(security):
    operations, org_id, factory, _ = security
    ops = operations()
    with pytest.raises(IdentityError, match="OWNER_SECURITY_TARGET_MISMATCH"):
        ops.execute(
            OwnerSecurityWindowRequest(kind="bootstrap_owner", org_id=org_id),
            "a" * 64,
            new_password=PASSWORD,
            repeat_password=PASSWORD,
        )
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(OwnerAccount)) == 0


def test_commit_acknowledgement_loss_is_marked_unknown(security, monkeypatch):
    provision(security)
    operations, _, _, store = security
    ops = operations()
    transaction = ops.transaction

    @contextmanager
    def lose_acknowledgement():
        with transaction() as session:
            yield session
        raise OSError("test-commit-response-lost")

    monkeypatch.setattr(ops, "transaction", lose_acknowledgement)
    with pytest.raises(OSError):
        ops._identity_operation(
            lambda service: service.replace_recovery_code(
                OwnerRecoveryCodeReplacementRequest(session_token=store.load_session_token())
            )
        )
    assert ops.operation_committed is None


def test_login_store_failure_restores_previous_token_and_revokes_new_session(security, monkeypatch):
    provision(security)
    operations, _, factory, store = security
    previous = store.load_session_token()
    save = store.save_session_token

    def partial_write(token):
        save(token)
        if token != previous:
            raise OSError("test-only-store-error")

    monkeypatch.setattr(store, "save_session_token", partial_write)
    ops = operations()
    with pytest.raises(IdentityError, match="IDENTITY_CREDENTIAL_STORE_WRITE_FAILED"):
        ops.execute(OwnerSecurityWindowRequest(kind="login"), ops.scope(), password=PASSWORD)
    assert store.load_session_token() == previous
    assert ops.operation_committed and not ops.login_completed
    with factory() as session:
        sessions = list(session.scalars(select(OwnerSession)))
        assert len(sessions) == 2
        assert sum(row.revoked_at is None for row in sessions) == 1


def test_close_reauth_binds_exact_preview_without_closing_books(security, monkeypatch):
    operations, org_id, factory, store = security
    with factory.begin() as session:
        evidence = Evidence(
            org_id=org_id,
            sha256="c" * 64,
            original_name="period.txt",
            media_type="text/plain",
            source="test",
            size_bytes=1,
            storage_path="test/period.txt",
        )
        session.add(evidence)
        session.flush()
        generated = AccountingPeriodService(session).generate_accounting_period(
            GenerateAccountingPeriodRequest(
                org_id=org_id,
                period_month="2026-07",
                idempotency_key="window-test",
                confirmation_note="test period",
                evidence_references=[evidence.id],
            )
        )
        assert generated.status.value == "posted"
        preview = AccountingPeriodService(session).preview_accounting_period_close(
            PreviewAccountingPeriodCloseRequest(
                org_id=org_id,
                period_id=generated.period_id,
                closing_date=date(2026, 7, 31),
            )
        )
    provision(security)
    request = OwnerSecurityWindowRequest(
        kind="approve_period_close",
        org_id=org_id,
        period_id=generated.period_id,
        calculation_hash=preview.calculation_hash,
    )
    ops = operations()
    old_token = store.load_session_token()
    ops.execute(request, ops.scope(), password=PASSWORD)
    with factory() as session:
        approval = session.scalar(select(AccountingPeriodCloseApproval))
        owner = session.scalar(select(OwnerAccount))
        assert approval.period_id == generated.period_id
        assert approval.calculation_hash == preview.calculation_hash
        assert approval.owner_account_id == owner.id
        assert approval.owner_credential_version == owner.credential_version
        assert approval.confirmation_method == "local_password_reauthentication"
        assert approval.consumed_at is None
        assert (approval.expires_at - approval.confirmed_at).total_seconds() == 1800
    assert store.load_session_token() != old_token
    # The second preview after password verification is mandatory.
    ops = operations()
    actual_authenticate = ops._authenticate

    def invalidate(*args):
        identity = actual_authenticate(*args)
        monkeypatch.setattr(
            ops,
            "_preview",
            lambda *_: (_ for _ in ()).throw(IdentityError("ACCOUNTING_PERIOD_CALCULATION_STALE")),
        )
        return identity

    monkeypatch.setattr(ops, "_authenticate", invalidate)
    with pytest.raises(IdentityError, match="ACCOUNTING_PERIOD_CALCULATION_STALE"):
        ops.execute(request, ops.scope(), password=PASSWORD)
    with factory() as session:
        assert session.scalar(select(func.count()).select_from(AccountingPeriodCloseApproval)) == 1


@pytest.mark.parametrize(
    "field", ["password", "session_token", "recovery_code", "command", "database_url"]
)
def test_public_schema_forbids_secrets_and_commands(field):
    with pytest.raises(ValidationError):
        OwnerSecurityWindowRequest.model_validate({"kind": "login", field: "test-only"})


def test_close_schema_requires_complete_binding():
    with pytest.raises(ValidationError):
        OwnerSecurityWindowRequest(kind="approve_period_close", org_id=uuid.uuid4())
    with pytest.raises(ValidationError):
        OwnerSecurityWindowRequest(kind="login", calculation_hash="a" * 64)


@pytest.mark.skipif(sys.platform != "win32", reason="native pythonw smoke")
def test_real_pythonw_launch_freezes_database_target_and_reports_crash(
    security,
    tmp_path,
    monkeypatch,
):
    from ai_accounting.owner_login_launcher import OwnerSecurityWindowLauncher, _state_root

    operations, org_id, _, store = security
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    # Deliberately conflicting inherited configuration must not override the caller.
    monkeypatch.setenv("DATABASE_URL", "sqlite:///wrong-target.db")
    children = []

    def start(*args, **kwargs):
        child = subprocess.Popen(*args, **kwargs)
        children.append(child)
        return child

    launcher = OwnerSecurityWindowLauncher(operations=operations(), popen=start)
    result = launcher.request(OwnerSecurityWindowRequest(kind="bootstrap_owner", org_id=org_id))
    native_pid = None
    try:
        deadline = time.monotonic() + 25
        while result["status"] == "starting" and time.monotonic() < deadline:
            time.sleep(0.1)
            result = launcher.status(result["request_id"])
        assert result["status"] == "waiting_for_user", result
        assert store.load_session_token() is None
        native_pid = launcher._read(_state_root(), result["request_id"]).pid
        os.kill(native_pid, signal.SIGTERM)
        native_pid = None
        children[0].wait(timeout=10)
        result = launcher.status(result["request_id"])
        assert result["status"] == "failed"
        assert result["error_code"] == "OWNER_SECURITY_WINDOW_INTERRUPTED"
    finally:
        if native_pid:
            os.kill(native_pid, signal.SIGTERM)
        for child in children:
            if child.poll() is None:
                child.terminate()
                child.wait(timeout=10)
