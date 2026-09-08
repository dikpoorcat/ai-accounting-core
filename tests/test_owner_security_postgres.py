from __future__ import annotations

import os
import sys
import uuid
from contextlib import ExitStack
from datetime import date
from decimal import Decimal
from types import SimpleNamespace

import pytest
from _postgres_helpers import _isolated_database_on_server, isolated_postgres_url
from alembic.config import Config
from pydantic import SecretStr
from sqlalchemy import create_engine, select
from sqlalchemy.engine import make_url
from sqlalchemy.orm import sessionmaker

from ai_accounting.config import Settings
from ai_accounting.identity import IdentityError
from ai_accounting.models import CatalogMetadata, CompanyRegistry, OwnerAccount
from ai_accounting.owner_login_launcher import OwnerSecurityWindowLauncher
from ai_accounting.owner_security import OwnerSecurityOperations, OwnerSecurityWindowRequest
from ai_accounting.replay_cli import ReplayError, _validate_replay_target
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(sys.platform != "win32", reason="isolated Windows Credential Manager"),
]
PASSWORD = SecretStr("Isolated-Security-Integration-2026!")


@pytest.fixture
def catalogs():
    with ExitStack() as stack:
        first = stack.enter_context(isolated_postgres_url("security_catalog"))
        second = stack.enter_context(
            _isolated_database_on_server(
                make_url(first).set(database="postgres"), "security_catalog"
            )
        )
        result = []
        for url in (first, second):
            config = Config("catalog_alembic.ini")
            config.attributes["database_url_override"] = url
            config.attributes["catalog_instance_id"] = uuid.uuid4()
            command.upgrade(config, "head")
            engine = create_engine(url)
            stack.callback(engine.dispose)
            factory = sessionmaker(engine, expire_on_commit=False)
            org_id = uuid.uuid4()
            with factory.begin() as session:
                session.info["catalog_mode"] = True
                session.add(
                    CompanyRegistry(
                        org_id=org_id,
                        database_name="finance_company_" + org_id.hex,
                        database_identity=uuid.uuid4(),
                        status="active",
                        display_name="隔离目录测试",
                        taxpayer_identification_number="91330106MA1234567T",
                        is_primary=True,
                        filing_cycle="quarterly",
                        profile_effective_from=date(2026, 1, 1),
                        urban_maintenance_rate=Decimal("0.07"),
                    )
                )
            settings = Settings(
                _env_file=None,
                database_url=url,
                finance_company_database_url=url,
                finance_migration_database_url=None,
            )
            ops = OwnerSecurityOperations(settings=settings, factory=factory)
            stack.callback(ops.store.delete_session_token)
            result.append((ops, org_id, factory))
        yield result


def bootstrap(entry):
    ops, org_id, _ = entry
    request = OwnerSecurityWindowRequest(kind="bootstrap_owner", org_id=org_id)
    recovery = ops.execute(request, ops.scope(), new_password=PASSWORD, repeat_password=PASSWORD)
    ops.finish_recovery_display(request, ops.scope(), new_password=PASSWORD)
    assert recovery and ops.login_completed


def test_two_catalogs_keep_independent_logins_and_reject_cross_target_requests(
    catalogs,
    tmp_path,
    monkeypatch,
):
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    first, second = catalogs
    a, _, _ = first
    b, b_org, b_factory = second
    bootstrap(first)
    bootstrap(second)
    assert a.scope() != b.scope()
    token_a = a.store.load_session_token()
    token_b = b.store.load_session_token()
    assert token_a != token_b
    a.execute(OwnerSecurityWindowRequest(kind="login"), a.scope(), password=PASSWORD)
    assert b.store.load_session_token() == token_b
    assert a.store.load_session_token() != token_a
    with pytest.raises(IdentityError, match="OWNER_SECURITY_TARGET_MISMATCH"):
        b.execute(OwnerSecurityWindowRequest(kind="login"), a.scope(), password=PASSWORD)
    with b_factory() as session:
        assert session.scalar(select(OwnerAccount)).org_id == b_org

    launcher_a = OwnerSecurityWindowLauncher(
        operations=a,
        popen=lambda *args, **kwargs: SimpleNamespace(pid=os.getpid()),
    )
    launcher_b = OwnerSecurityWindowLauncher(operations=b)
    result = launcher_a.request(OwnerSecurityWindowRequest(kind="login"))
    with pytest.raises(IdentityError, match="OWNER_SECURITY_TARGET_MISMATCH"):
        launcher_b.status(result["request_id"])
    with pytest.raises(IdentityError, match="OWNER_SECURITY_WINDOW_BUSY"):
        launcher_b.request(OwnerSecurityWindowRequest(kind="login"))


def test_replay_rejects_other_catalog_before_reading_company_data(catalogs):
    a, org_id, factory = catalogs[0]
    b, _, _ = catalogs[1]
    with factory() as session:
        marker = session.get(CatalogMetadata, 1)
        state = {
            "catalog_instance_id": str(marker.catalog_instance_id),
            "catalog_database": make_url(a.settings.database_url).database,
            "primary_org_id": str(org_id),
            "companies": [],
        }
    _validate_replay_target(state, a.settings)
    with pytest.raises(ReplayError, match="REPLAY_TARGET_CATALOG_MISMATCH"):
        _validate_replay_target(state, b.settings)
