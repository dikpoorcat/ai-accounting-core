from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from ai_accounting.database import Base, make_engine
from ai_accounting.models import (
    IdentityAuditEvent,
    Organization,
    OwnerAccount,
    OwnerRecoveryCode,
    OwnerSession,
)

PASSWORD_HASH = (
    "$argon2id$v=19$m=65536,t=3,p=4$AAAAAAAAAAAAAAAAAAAAAA$"
    "BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
)


def test_identity_metadata_is_sqlite_compatible_and_keeps_one_current_recovery_code() -> None:
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    now = datetime.now(UTC)
    try:
        with Session(engine) as session:
            organization = Organization(
                name="单负责人元数据",
                taxpayer_identification_number="91330106MA1234567T",
            )
            session.add(organization)
            session.flush()
            owner = OwnerAccount(
                org_id=organization.id,
                login_name="Owner",
                login_name_normalized="owner",
                password_hash=PASSWORD_HASH,
            )
            session.add(owner)
            session.flush()
            owner_id, org_id = owner.id, organization.id
            identity_session = OwnerSession(
                org_id=org_id,
                owner_account_id=owner_id,
                secret_sha256="a" * 64,
                credential_version=1,
                idle_expires_at=now + timedelta(minutes=30),
                absolute_expires_at=now + timedelta(hours=8),
            )
            recovery = OwnerRecoveryCode(
                org_id=org_id,
                owner_account_id=owner_id,
                code_sha256="b" * 64,
                credential_version=1,
            )
            audit = IdentityAuditEvent(
                org_id=org_id,
                owner_account_id=owner_id,
                event_type="owner_provisioned",
                outcome="succeeded",
                request_correlation_id=uuid.uuid4(),
            )
            session.add_all([identity_session, recovery, audit])
            session.commit()

        with Session(engine) as session:
            session.add(
                OwnerRecoveryCode(
                    org_id=org_id,
                    owner_account_id=owner_id,
                    code_sha256="c" * 64,
                    credential_version=1,
                )
            )
            with pytest.raises(IntegrityError):
                session.commit()
    finally:
        engine.dispose()


def test_identity_metadata_exposes_only_fixed_audit_fields() -> None:
    columns = set(IdentityAuditEvent.__table__.columns.keys())
    assert columns == {
        "id",
        "org_id",
        "owner_account_id",
        "session_id",
        "event_type",
        "outcome",
        "reason_code",
        "request_correlation_id",
        "occurred_at",
    }
    assert "details" not in columns
    assert "password" not in columns
    assert "login_name" not in columns
