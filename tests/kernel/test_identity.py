"""Synthetic identity tests; no real catalogue or Windows credential is opened."""

import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime, timedelta

import pytest
from pydantic import SecretStr

from ai_accounting.kernel.runtime import connect
from ai_accounting.kernel.security import (
    IdentityError,
    SecurityService,
    consume_close_approval,
    initialize_catalog,
    initialize_company,
)
from ai_accounting.kernel.security.primitives import (
    normalize_authentication_password,
    verify_password,
)

PASSWORD = SecretStr("Synthetic-owner-password-123")
NEW_PASSWORD = SecretStr("Synthetic-replacement-password-456")


class Clock:
    def __init__(self):
        self.value = datetime(2026, 9, 1, tzinfo=UTC)

    def __call__(self):
        return self.value

    def advance(self, **duration):
        self.value += timedelta(**duration)


def catalogue(path, *, clock=None):
    with closing(connect(path)) as connection:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "CREATE TABLE catalog_identity(id INTEGER PRIMARY KEY,instance_id TEXT,"
            "schema_version INTEGER) STRICT"
        )
        connection.execute("INSERT INTO catalog_identity VALUES(1,'synthetic-catalog',2)")
        initialize_catalog(connection)
        connection.commit()
    return SecurityService(path, clock=clock, catalog_validator=lambda connection: None)


@pytest.fixture
def identity(tmp_path):
    clock = Clock()
    service = catalogue(tmp_path / "catalog.sqlite", clock=clock)
    recovery = service.provision("owner", PASSWORD)
    return service, clock, recovery


def test_constructor_requires_migrated_catalog_and_does_not_create(tmp_path):
    path = tmp_path / "missing.sqlite"
    with pytest.raises(IdentityError, match="IDENTITY_CATALOG_UNAVAILABLE"):
        SecurityService(path)
    assert not path.exists()


def test_invalid_audit_request_rolls_back_owner_creation(tmp_path):
    service = catalogue(tmp_path / "catalog.sqlite")
    with pytest.raises(IdentityError, match="REQUEST_INVALID"):
        service.provision("owner", PASSWORD, request_id="")
    assert not service.status()["provisioned"]


def test_real_catalog_schema_tampering_blocks_identity_mutation(tmp_path):
    from ai_accounting.kernel.catalog import Catalog
    from ai_accounting.kernel.contracts import KernelError
    from ai_accounting.kernel.service import default_registry

    catalog = Catalog(tmp_path / "root", default_registry())
    service = SecurityService(catalog.path)
    with closing(connect(catalog.path)) as connection:
        connection.execute("DROP TRIGGER security_audit_no_update")
    with pytest.raises(KernelError):
        service.provision("owner", PASSWORD)
    with closing(connect(catalog.path, read_only=True)) as connection:
        assert connection.execute("SELECT count(*) FROM security_owner").fetchone()[0] == 0


def test_security_ddl_is_owned_by_migration_transaction(tmp_path):
    with closing(connect(tmp_path / "ddl.sqlite")) as connection:
        with pytest.raises(RuntimeError):
            initialize_catalog(connection)
        connection.execute("BEGIN IMMEDIATE")
        initialize_catalog(connection)
        connection.rollback()
        assert (
            connection.execute(
                "SELECT count(*) FROM sqlite_schema WHERE name LIKE 'security_%'"
            ).fetchone()[0]
            == 0
        )


def test_current_password_and_recovery_rules_are_reused(identity):
    service, _, recovered = identity
    login = service.login(" OWNER ", PASSWORD)
    assert service.authorize(login.session_token) == login.authority
    assert PASSWORD.get_secret_value() not in repr(login)
    assert recovered.recovery_code.get_secret_value() not in repr(recovered)
    with closing(connect(service.path, read_only=True)) as connection:
        account = connection.execute("SELECT * FROM security_owner").fetchone()
        assert verify_password(
            password_hash=account["password_hash"], password=PASSWORD.get_secret_value()
        )
        assert account["credential_version"] == 1
        assert (
            connection.execute("SELECT length(secret_hash) FROM security_session").fetchone()[0]
            == 32
        )
        assert (
            connection.execute("SELECT length(code_hash) FROM security_recovery").fetchone()[0]
            == 32
        )
    assert normalize_authentication_password("e\u0301abcde") == "éabcde"
    assert service.status()["provisioned"] is True
    with pytest.raises(IdentityError, match="IDENTITY_OWNER_ALREADY_PROVISIONED"):
        service.provision("another-owner", PASSWORD)


def test_failed_password_attempts_persist_and_throttle_correct_password(identity):
    service, clock, _ = identity
    for _ in range(5):
        with pytest.raises(IdentityError, match="IDENTITY_AUTHENTICATION_FAILED"):
            service.login("owner", SecretStr("incorrect-synthetic-password"))
    with closing(connect(service.path, read_only=True)) as connection:
        assert connection.execute("SELECT password_failures FROM security_owner").fetchone()[0] == 5
        assert (
            connection.execute(
                "SELECT count(*) FROM security_audit WHERE event='login_failed'"
            ).fetchone()[0]
            == 5
        )
    with pytest.raises(IdentityError):
        service.login("owner", PASSWORD)
    clock.advance(seconds=30)
    service.login("owner", PASSWORD)
    with closing(connect(service.path, read_only=True)) as connection:
        assert connection.execute("SELECT password_failures FROM security_owner").fetchone()[0] == 0


def test_unknown_login_returns_generic_failure_without_echo_or_owner_throttle(identity):
    service, _, _ = identity
    with pytest.raises(IdentityError) as caught:
        service.login("missing-user", SecretStr("synthetic-unknown-password"))
    assert caught.value.code == "IDENTITY_AUTHENTICATION_FAILED"
    assert "missing-user" not in str(caught.value.response())
    service.login("owner", PASSWORD)


def test_idle_expiry_is_seven_days(identity):
    service, clock, _ = identity
    login = service.login("owner", PASSWORD)
    clock.advance(days=7)
    with pytest.raises(IdentityError, match="IDENTITY_SESSION_INVALID"):
        service.authorize(login.session_token)
    with closing(connect(service.path, read_only=True)) as connection:
        assert (
            connection.execute("SELECT revoke_reason FROM security_session").fetchone()[0]
            == "idle_expired"
        )


def test_active_session_does_not_extend_thirty_day_absolute_limit(identity):
    service, clock, _ = identity
    login = service.login("owner", PASSWORD)
    for days in (6, 6, 6, 6, 5):
        clock.advance(days=days)
        service.authorize(login.session_token)
    clock.advance(days=1)
    with pytest.raises(IdentityError, match="IDENTITY_SESSION_INVALID"):
        service.authorize(login.session_token)


def test_password_change_revokes_every_session_and_replaces_recovery(identity):
    service, _, old_recovery = identity
    first, second = service.login("owner", PASSWORD), service.login("owner", PASSWORD)
    changed = service.change_password(first.session_token, PASSWORD, NEW_PASSWORD)
    for token in (first.session_token, second.session_token):
        with pytest.raises(IdentityError):
            service.authorize(token)
    with pytest.raises(IdentityError):
        service.recover("owner", old_recovery.recovery_code, PASSWORD)
    service.login("owner", NEW_PASSWORD)
    replacement = service.recover("owner", changed.recovery_code, PASSWORD)
    assert replacement.owner_id == changed.owner_id
    service.login("owner", PASSWORD)
    with pytest.raises(IdentityError):
        service.recover("owner", changed.recovery_code, NEW_PASSWORD)


def test_recovery_failures_are_separate_and_persistent(identity):
    service, clock, recovery = identity
    for _ in range(5):
        with pytest.raises(IdentityError, match="IDENTITY_RECOVERY_FAILED"):
            service.recover("owner", "not-a-recovery-code", NEW_PASSWORD)
    service.login("owner", PASSWORD)
    with pytest.raises(IdentityError):
        service.recover("owner", recovery.recovery_code, NEW_PASSWORD)
    clock.advance(seconds=30)
    service.recover("owner", recovery.recovery_code, NEW_PASSWORD)
    service.login("owner", NEW_PASSWORD)


def test_recovery_replacement_preserves_session_and_invalidates_old_code(identity):
    service, _, old = identity
    login = service.login("owner", PASSWORD)
    new = service.replace_recovery_code(login.session_token)
    assert service.authorize(login.session_token) == login.authority
    with pytest.raises(IdentityError):
        service.recover("owner", old.recovery_code, NEW_PASSWORD)
    service.recover("owner", new.recovery_code, NEW_PASSWORD)
    with pytest.raises(IdentityError):
        service.authorize(login.session_token)


def test_logout_is_idempotent_and_gate_orders_revocation_after_business_commit(identity):
    service, _, _ = identity
    login = service.login("owner", PASSWORD)
    started = threading.Event()

    def revoke():
        started.set()
        return service.logout(login.session_token)

    with ThreadPoolExecutor(max_workers=1) as pool:
        with service.authorized(login.session_token) as authority:
            future = pool.submit(revoke)
            assert started.wait(timeout=2)
            assert not future.done()
            assert service.validate_authority(authority) == authority
        assert future.result(timeout=2)["status"] == "logged_out"
    assert service.logout(login.session_token)["status"] == "logged_out"
    with pytest.raises(IdentityError):
        service.validate_authority(login.authority)


def test_authority_is_bound_to_catalogue_and_credentials(identity):
    service, _, _ = identity
    authority = service.login("owner", PASSWORD).authority
    for changed in (
        replace(authority, catalog_instance_id="another-catalogue"),
        replace(authority, owner_id="another-owner"),
        replace(authority, credential_version=authority.credential_version + 1),
    ):
        with pytest.raises(IdentityError):
            service.validate_authority(changed)


def test_atomic_security_failure_does_not_partly_rotate_password(identity):
    service, _, _ = identity
    login = service.login("owner", PASSWORD)
    with closing(connect(service.path)) as connection:
        connection.execute(
            "CREATE TRIGGER reject_rotation BEFORE INSERT ON security_audit WHEN "
            "NEW.event='password_changed' BEGIN SELECT RAISE(ABORT,'synthetic disk-stage "
            "failure'); END"
        )
    with pytest.raises(Exception, match="synthetic disk-stage failure"):
        service.change_password(login.session_token, PASSWORD, NEW_PASSWORD)
    service.authorize(login.session_token)
    service.login("owner", PASSWORD)


def company(path):
    connection = connect(path)
    connection.execute("BEGIN IMMEDIATE")
    connection.execute(
        "CREATE TABLE identity(id INTEGER PRIMARY KEY,company_id TEXT,database_id TEXT) STRICT"
    )
    connection.execute("INSERT INTO identity VALUES(1,'synthetic-company','synthetic-database')")
    initialize_company(connection)
    connection.commit()
    return connection


def binding():
    return {
        "company_id": "synthetic-company",
        "database_id": "synthetic-database",
        "period": "2026-09",
        "preview_digest": "12" * 32,
        "epochs": {"accounting": 4, "material": 5, "management": 6},
    }


def test_close_approval_consumption_rolls_back_with_close(identity, tmp_path):
    service, _, _ = identity
    login = service.login("owner", PASSWORD)
    with closing(company(tmp_path / "company.sqlite")) as connection:
        connection.execute("BEGIN IMMEDIATE")
        approval = service.issue_close_approval(
            connection, token=login.session_token, password=PASSWORD, **binding()
        )
        connection.commit()
        for commit in (False, True):
            with service.authorized(login.session_token) as authority:
                connection.execute("BEGIN IMMEDIATE")
                consume_close_approval(
                    connection,
                    approval["approval_id"],
                    authority=authority,
                    now=service.now(),
                    **binding(),
                )
                connection.commit() if commit else connection.rollback()
        with pytest.raises(IdentityError, match="IDENTITY_CLOSE_APPROVAL_INVALID"):
            connection.execute("BEGIN IMMEDIATE")
            consume_close_approval(
                connection,
                approval["approval_id"],
                authority=login.authority,
                now=service.now(),
                **binding(),
            )
        connection.rollback()


@pytest.mark.parametrize(
    "field,value",
    [
        ("company_id", "wrong-company"),
        ("database_id", "wrong-database"),
        ("period", "2026-10"),
        ("preview_digest", "34" * 32),
        ("epochs", {"accounting": 5, "material": 5, "management": 6}),
    ],
)
def test_close_approval_rejects_changed_target_or_preview(identity, tmp_path, field, value):
    service, _, _ = identity
    login = service.login("owner", PASSWORD)
    with closing(company(tmp_path / "company.sqlite")) as connection:
        connection.execute("BEGIN IMMEDIATE")
        approval = service.issue_close_approval(
            connection, token=login.session_token, password=PASSWORD, **binding()
        )
        connection.commit()
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(IdentityError):
            consume_close_approval(
                connection,
                approval["approval_id"],
                authority=login.authority,
                now=service.now(),
                **(binding() | {field: value}),
            )
        connection.rollback()


def test_close_approval_requires_fresh_password_and_expires_after_thirty_minutes(
    identity, tmp_path
):
    service, clock, _ = identity
    login = service.login("owner", PASSWORD)
    with closing(company(tmp_path / "company.sqlite")) as connection:
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(IdentityError):
            service.issue_close_approval(
                connection, token=login.session_token, password="incorrect-password", **binding()
            )
        assert connection.execute("SELECT count(*) FROM security_close_approval").fetchone()[0] == 0
        approval = service.issue_close_approval(
            connection, token=login.session_token, password=PASSWORD, **binding()
        )
        connection.commit()
        clock.advance(minutes=30)
        connection.execute("BEGIN IMMEDIATE")
        with pytest.raises(IdentityError):
            consume_close_approval(
                connection,
                approval["approval_id"],
                authority=login.authority,
                now=service.now(),
                **binding(),
            )
        connection.rollback()
