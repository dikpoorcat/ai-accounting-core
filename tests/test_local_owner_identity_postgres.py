from __future__ import annotations

import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime, timedelta
from threading import Barrier

import pytest
import sqlalchemy as sa
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.identity import IdentityError
from ai_accounting.identity_schemas import (
    OwnerLoginRequest,
    OwnerPasswordChangeRequest,
    OwnerProvisionRequest,
)
from ai_accounting.identity_service import IdentityService
from ai_accounting.models import OwnerAccount, OwnerRecoveryCode, OwnerSession
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]

REVISION_0012 = "0012_accounting_period_close"
REVISION_0013 = "0013_local_owner_identity"
REVISION_0014 = "0014_execution_attribution"
PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
)


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_org(connection: sa.Connection, org_id: uuid.UUID, name: str) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO organizations (
                id, name, taxpayer_type, filing_cycle, jurisdiction,
                urban_maintenance_rate, accounting_standard, created_at
            ) VALUES (
                :id, :name, 'small_scale', 'quarterly', 'CN',
                0.07, 'small_enterprise', CURRENT_TIMESTAMP
            )
            """
        ),
        {"id": org_id, "name": name},
    )


def _insert_owner(
    connection: sa.Connection,
    *,
    owner_id: uuid.UUID,
    org_id: uuid.UUID,
    login_name: str,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO owner_accounts (
                id, org_id, login_name, login_name_normalized, password_hash
            ) VALUES (:id, :org_id, :login_name, :normalized, :password_hash)
            """
        ),
        {
            "id": owner_id,
            "org_id": org_id,
            "login_name": login_name,
            "normalized": login_name.lower(),
            "password_hash": PASSWORD_HASH,
        },
    )


def test_postgres_0013_zero_owner_linear_and_base_round_trip() -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        config = _config(database_url)
        engine = sa.create_engine(database_url)
        try:
            command.upgrade(config, REVISION_0012)
            command.upgrade(config, REVISION_0013)
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT COUNT(*) FROM owner_accounts")) == 0
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    REVISION_0013
                )

            command.downgrade(config, REVISION_0012)
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    REVISION_0012
                )
                assert connection.scalar(
                    sa.text("SELECT to_regclass('public.owner_accounts') IS NULL")
                )

            command.upgrade(config, "head")
            command.downgrade(config, "base")
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT COUNT(*) FROM alembic_version")) == 0
            command.upgrade(config, "head")
            command.check(config)
        finally:
            engine.dispose()


def test_postgres_0013_singleton_cross_org_concurrency_and_immutable_history() -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        config = _config(database_url)
        command.upgrade(config, "head")
        engine = sa.create_engine(database_url)
        org_ids = [uuid.uuid4(), uuid.uuid4()]
        owner_ids = [uuid.uuid4(), uuid.uuid4()]
        with engine.begin() as connection:
            _insert_org(connection, org_ids[0], "并发负责人甲")
            _insert_org(connection, org_ids[1], "并发负责人乙")

        barrier = Barrier(2)

        def insert_concurrently(index: int) -> tuple[str, int]:
            barrier.wait()
            try:
                with engine.begin() as connection:
                    _insert_owner(
                        connection,
                        owner_id=owner_ids[index],
                        org_id=org_ids[index],
                        login_name=f"Owner{index}",
                    )
                return ("inserted", index)
            except DBAPIError:
                return ("rejected", index)

        try:
            with ThreadPoolExecutor(max_workers=2) as executor:
                outcomes = list(executor.map(insert_concurrently, range(2)))
            assert sorted(outcome for outcome, _ in outcomes) == ["inserted", "rejected"]
            winner = next(index for outcome, index in outcomes if outcome == "inserted")
            loser = 1 - winner
            owner_id, owner_org_id = owner_ids[winner], org_ids[winner]
            other_org_id = org_ids[loser]

            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT COUNT(*) FROM owner_accounts")) == 1

            with engine.begin() as connection:
                with pytest.raises(DBAPIError):
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO owner_sessions (
                                id, org_id, owner_account_id, secret_sha256,
                                credential_version, idle_expires_at, absolute_expires_at
                            ) VALUES (
                                :id, :org_id, :owner_id, :secret, 1,
                                CURRENT_TIMESTAMP + INTERVAL '30 minutes',
                                CURRENT_TIMESTAMP + INTERVAL '8 hours'
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "org_id": other_org_id,
                            "owner_id": owner_id,
                            "secret": "f" * 64,
                        },
                    )

            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO owner_recovery_codes (
                                id, org_id, owner_account_id, code_sha256, credential_version
                            ) VALUES (:id, :org_id, :owner_id, :code_hash, 1)
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "org_id": other_org_id,
                            "owner_id": owner_id,
                            "code_hash": "9" * 64,
                        },
                    )

            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO identity_audit_events (
                                id, org_id, owner_account_id, event_type, outcome,
                                reason_code, request_correlation_id
                            ) VALUES (
                                :id, :org_id, :owner_id, 'login_failed', 'rejected',
                                'INVALID_CREDENTIALS', :correlation_id
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "org_id": other_org_id,
                            "owner_id": owner_id,
                            "correlation_id": uuid.uuid4(),
                        },
                    )

            session_id = uuid.uuid4()
            recovery_id = uuid.uuid4()
            audit_id = uuid.uuid4()
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO owner_sessions (
                            id, org_id, owner_account_id, secret_sha256,
                            credential_version, idle_expires_at, absolute_expires_at
                        ) VALUES (
                            :id, :org_id, :owner_id, :secret, 1,
                            CURRENT_TIMESTAMP + INTERVAL '30 minutes',
                            CURRENT_TIMESTAMP + INTERVAL '8 hours'
                        )
                        """
                    ),
                    {
                        "id": session_id,
                        "org_id": owner_org_id,
                        "owner_id": owner_id,
                        "secret": "a" * 64,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO owner_recovery_codes (
                            id, org_id, owner_account_id, code_sha256, credential_version
                        ) VALUES (:id, :org_id, :owner_id, :code_hash, 1)
                        """
                    ),
                    {
                        "id": recovery_id,
                        "org_id": owner_org_id,
                        "owner_id": owner_id,
                        "code_hash": "b" * 64,
                    },
                )
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO identity_audit_events (
                            id, org_id, owner_account_id, session_id, event_type,
                            outcome, request_correlation_id
                        ) VALUES (
                            :id, :org_id, :owner_id, :session_id, 'login_succeeded',
                            'succeeded', :correlation_id
                        )
                        """
                    ),
                    {
                        "id": audit_id,
                        "org_id": owner_org_id,
                        "owner_id": owner_id,
                        "session_id": session_id,
                        "correlation_id": uuid.uuid4(),
                    },
                )

            for statement, code in (
                (
                    "UPDATE owner_accounts SET org_id = :other WHERE id = :id",
                    "IDENTITY_OWNER_IMMUTABLE_FIELD",
                ),
                (
                    "UPDATE owner_accounts SET password_hash = 'changed' WHERE id = :id",
                    "IDENTITY_CREDENTIAL_ROTATION_INVALID",
                ),
                (
                    "UPDATE owner_accounts SET login_name = 'Renamed' WHERE id = :id",
                    "IDENTITY_OWNER_IMMUTABLE_FIELD",
                ),
                (
                    "DELETE FROM owner_accounts WHERE id = :id",
                    "IDENTITY_SUBJECT_DELETE_FORBIDDEN",
                ),
                (
                    "UPDATE owner_sessions SET secret_sha256 = :secret WHERE id = :id",
                    "IDENTITY_SESSION_IMMUTABLE_FIELD",
                ),
                (
                    "DELETE FROM owner_recovery_codes WHERE id = :id",
                    "IDENTITY_RECOVERY_HISTORY_DELETE_FORBIDDEN",
                ),
                (
                    "UPDATE identity_audit_events SET outcome = 'blocked' WHERE id = :id",
                    "IDENTITY_AUDIT_APPEND_ONLY",
                ),
                (
                    "DELETE FROM identity_audit_events WHERE id = :id",
                    "IDENTITY_AUDIT_APPEND_ONLY",
                ),
            ):
                target_id = (
                    session_id
                    if "owner_sessions" in statement
                    else recovery_id
                    if "owner_recovery_codes" in statement
                    else audit_id
                    if "identity_audit_events" in statement
                    else owner_id
                )
                with pytest.raises(DBAPIError, match=code):
                    with engine.begin() as connection:
                        connection.execute(
                            sa.text(statement),
                            {"id": target_id, "other": other_org_id, "secret": "e" * 64},
                        )

            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            UPDATE owner_accounts
                               SET password_hash = 'not-an-argon2id-hash',
                                   credential_version = credential_version + 1,
                                   password_changed_at = password_changed_at + INTERVAL '1 second',
                                   updated_at = updated_at + INTERVAL '1 second'
                             WHERE id = :id
                            """
                        ),
                        {"id": owner_id},
                    )

            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO owner_sessions (
                                id, org_id, owner_account_id, secret_sha256,
                                credential_version, idle_expires_at, absolute_expires_at
                            ) VALUES (
                                :id, :org_id, :owner_id, :secret, 1,
                                CURRENT_TIMESTAMP + INTERVAL '30 minutes',
                                CURRENT_TIMESTAMP + INTERVAL '8 hours'
                            )
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "org_id": owner_org_id,
                            "owner_id": owner_id,
                            "secret": "A" * 64,
                        },
                    )

            now = datetime.now(UTC)
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        """
                        UPDATE owner_accounts
                           SET password_failed_attempts = 2,
                               updated_at = :updated_at
                         WHERE id = :id
                        """
                    ),
                    {"id": owner_id, "updated_at": now + timedelta(seconds=1)},
                )
                connection.execute(
                    sa.text(
                        """
                        UPDATE owner_sessions
                           SET revoked_at = CURRENT_TIMESTAMP,
                               revoke_reason = 'logout'
                         WHERE id = :id
                        """
                    ),
                    {"id": session_id},
                )
                connection.execute(
                    sa.text(
                        "UPDATE owner_recovery_codes SET used_at = CURRENT_TIMESTAMP WHERE id = :id"
                    ),
                    {"id": recovery_id},
                )

            next_recovery_id = uuid.uuid4()
            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        """
                        INSERT INTO owner_recovery_codes (
                            id, org_id, owner_account_id, code_sha256, credential_version
                        ) VALUES (:id, :org_id, :owner_id, :code_hash, 1)
                        """
                    ),
                    {
                        "id": next_recovery_id,
                        "org_id": owner_org_id,
                        "owner_id": owner_id,
                        "code_hash": "c" * 64,
                    },
                )

            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            INSERT INTO owner_recovery_codes (
                                id, org_id, owner_account_id, code_sha256, credential_version
                            ) VALUES (:id, :org_id, :owner_id, :code_hash, 1)
                            """
                        ),
                        {
                            "id": uuid.uuid4(),
                            "org_id": owner_org_id,
                            "owner_id": owner_id,
                            "code_hash": "d" * 64,
                        },
                    )

            with pytest.raises(DBAPIError):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text("DELETE FROM organizations WHERE id = :id"),
                        {"id": owner_org_id},
                    )

            with engine.begin() as connection:
                connection.execute(
                    sa.text(
                        """
                        UPDATE owner_accounts
                           SET status = 'disabled', updated_at = :updated_at
                         WHERE id = :id
                        """
                    ),
                    {"id": owner_id, "updated_at": now + timedelta(seconds=2)},
                )

            with pytest.raises(DBAPIError, match="IDENTITY_OWNER_REACTIVATION_FORBIDDEN"):
                with engine.begin() as connection:
                    connection.execute(
                        sa.text(
                            """
                            UPDATE owner_accounts
                               SET status = 'active', updated_at = updated_at + INTERVAL '1 second'
                             WHERE id = :id
                            """
                        ),
                        {"id": owner_id},
                    )

            with pytest.raises(RuntimeError, match="IDENTITY_DOWNGRADE_UNSAFE"):
                command.downgrade(config, REVISION_0012)
            with engine.connect() as connection:
                assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                    REVISION_0014
                )
        finally:
            engine.dispose()


def test_postgres_identity_service_commits_password_rotation_atomically() -> None:
    class SequenceRandom:
        def __init__(self) -> None:
            self.value = 0

        def bytes(self, size: int) -> bytes:
            self.value += 1
            return bytes([self.value]) * size

    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        database_url = postgres.get_connection_url()
        command.upgrade(_config(database_url), "head")
        engine = sa.create_engine(database_url)
        random = SequenceRandom()
        try:
            with Session(engine) as session:
                organization = seed_organization(session, name="PG身份服务提交点")
                session.flush()
                service = IdentityService(session, randbytes=random.bytes)
                provisioned = service.provision_owner(
                    OwnerProvisionRequest(
                        org_id=organization.id,
                        login_name="owner",
                        password=SecretStr("Correct-Horse-Battery-2026!"),
                    )
                )
                session.commit()

                authenticated = service.authenticate(
                    OwnerLoginRequest(
                        login_name="OWNER",
                        password=SecretStr("Correct-Horse-Battery-2026!"),
                    )
                )
                original_token = authenticated.session_token.get_secret_value()
                session.commit()

                rotated = service.change_password(
                    OwnerPasswordChangeRequest(
                        session_token=SecretStr(original_token),
                        login_name="owner",
                        current_password=SecretStr("Correct-Horse-Battery-2026!"),
                        new_password=SecretStr("Updated-Horse-Battery-2026!"),
                    )
                )
                session.commit()

                owner = session.get(OwnerAccount, provisioned.owner_account_id)
                original_session = session.get(OwnerSession, authenticated.session_id)
                recovery_codes = session.scalars(
                    sa.select(OwnerRecoveryCode).order_by(OwnerRecoveryCode.created_at)
                ).all()
                assert owner is not None and owner.credential_version == 2
                assert original_session is not None
                assert original_session.revoke_reason == "credential_changed"
                assert len(recovery_codes) == 2
                assert sum(
                    code.used_at is None and code.invalidated_at is None
                    for code in recovery_codes
                ) == 1
                current_code = next(
                    code
                    for code in recovery_codes
                    if code.used_at is None and code.invalidated_at is None
                )
                assert current_code.credential_version == owner.credential_version
                assert rotated.recovery_code.get_secret_value() not in current_code.code_sha256

                with pytest.raises(IdentityError, match="IDENTITY_SESSION_INVALID"):
                    service.authorize_execution(
                        session_token=original_token,
                        executor=object(),  # type: ignore[arg-type]
                        request_correlation_id=uuid.uuid4(),
                    )
                session.rollback()
        finally:
            engine.dispose()
