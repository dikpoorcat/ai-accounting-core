from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

from ai_accounting.accounting_period_service import AccountingPeriodService
from ai_accounting.execution_attribution import EXECUTION_ATTRIBUTION_SESSION_KEY


@pytest.mark.parametrize(
    "mutation",
    [
        "none",
        "organization",
        "period",
        "preview",
        "owner",
        "session",
        "credentials",
        "catalog",
        "expired",
        "consumed",
        "method",
        "missing_approval",
    ],
)
def test_window_completion_cannot_weaken_exact_close_authority(monkeypatch, mutation):
    org_id, period_id = uuid.uuid4(), uuid.uuid4()
    attribution = SimpleNamespace(
        org_id=org_id,
        owner_account_id=uuid.uuid4(),
        owner_session_id=uuid.uuid4(),
        owner_credential_version=1,
        catalog_instance_id=uuid.uuid4(),
    )
    approval = SimpleNamespace(
        **vars(attribution),
        period_id=period_id,
        calculation_hash="a" * 64,
        confirmation_method="local_password_reauthentication",
        consumed_at=None,
        expires_at=datetime.now(UTC) + timedelta(minutes=30),
    )
    request = SimpleNamespace(
        org_id=org_id,
        owner_approval_id=uuid.uuid4(),
        calculation_hash="a" * 64,
    )
    if mutation == "organization":
        attribution.org_id = uuid.uuid4()
    elif mutation == "period":
        approval.period_id = uuid.uuid4()
    elif mutation == "preview":
        approval.calculation_hash = "b" * 64
    elif mutation == "owner":
        approval.owner_account_id = uuid.uuid4()
    elif mutation == "session":
        approval.owner_session_id = uuid.uuid4()
    elif mutation == "credentials":
        approval.owner_credential_version = 2
    elif mutation == "catalog":
        approval.catalog_instance_id = uuid.uuid4()
    elif mutation == "expired":
        approval.expires_at = datetime.now(UTC) - timedelta(seconds=1)
    elif mutation == "consumed":
        approval.consumed_at = datetime.now(UTC)
    elif mutation == "method":
        approval.confirmation_method = "ordinary_login"
    elif mutation == "missing_approval":
        request.owner_approval_id = None
    session = SimpleNamespace(
        info={EXECUTION_ATTRIBUTION_SESSION_KEY: uuid.uuid4()},
        get=lambda *_: attribution,
        scalar=lambda *_: approval,
    )
    service = AccountingPeriodService(session)
    monkeypatch.setattr(service, "_owner_close_approval_required", lambda _: True)
    result = service._validated_owner_close_approval(request, SimpleNamespace(id=period_id))
    assert (result is approval) is (mutation == "none")
