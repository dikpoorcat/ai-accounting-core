from __future__ import annotations

import asyncio
import json

import pytest
from _postgres_helpers import authenticated_business_database, confirmed_payroll
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from ai_accounting import company_cli, dashboard_server, mcp_server
from ai_accounting.company_router import CompanyDatabaseRouter
from ai_accounting.config import Settings
from ai_accounting.credential_store import InMemoryCredentialStore
from ai_accounting.dashboard_common import DashboardDataError
from ai_accounting.schema_readiness import schema_state
from alembic import command

pytestmark = [pytest.mark.postgres, pytest.mark.postgres_current]


def _accounting_snapshot(engine):
    tables = (
        "business_events",
        "business_event_components",
        "vouchers",
        "voucher_lines",
        "payroll_batches",
        "payroll_lines",
        "open_items",
        "audit_logs",
        "business_event_amendments",
        "accounting_period_closes",
        "employees",
        "execution_attributions",
    )
    with engine.connect() as connection:
        return {
            table: sorted(
                json.dumps(row, sort_keys=True, ensure_ascii=False)
                for row in connection.scalars(text(f'SELECT row_to_json(t) FROM "{table}" t'))
            )
            for table in tables
        }


def test_v4_payroll_read_migration_and_current_mcp_keep_original_facts(monkeypatch):
    with authenticated_business_database(
        "schema_payroll", revision="0001_business_baseline_v4"
    ) as (engine, org_id, evidence_id, owner):
        with Session(engine) as session:
            _, _, _, _, original = confirmed_payroll(session, org_id, evidence_id, owner)
            event_id = original.id
            session.commit()
        before = _accounting_snapshot(engine)
        # Exercise the exact formerly failing handler, on an actual v4 payroll.
        with Session(engine) as session:
            marker = mcp_server._ACTIVE_TOOL_SESSION.set(session)
            try:
                direct = mcp_server.finance_get_event(str(org_id), str(event_id))
            finally:
                mcp_server._ACTIVE_TOOL_SESSION.reset(marker)
        assert direct["errors"] == ["DATABASE_SCHEMA_MISMATCH"], direct
        assert direct["data"]["diagnostic"]["sqlstate"] == "42703"

        catalog = create_engine(owner.catalog_url)
        settings = Settings(
            _env_file=None,
            finance_environment="development",
            database_url=owner.catalog_url,
            finance_company_database_url=engine.url.render_as_string(hide_password=False),
        )
        routing = CompanyDatabaseRouter(settings)
        # Keep catalog identity checks; only map the test's registered alias to its disposable DB.
        monkeypatch.setattr(routing, "company_url", lambda _name, **_kw: engine.url)
        monkeypatch.setattr(company_cli, "CompanyDatabaseRouter", lambda _settings: routing)
        monkeypatch.setattr(company_cli, "get_settings", lambda: settings)
        monkeypatch.setattr(mcp_server, "get_settings", lambda: settings)
        monkeypatch.setattr(mcp_server, "company_router", routing)
        monkeypatch.setattr(dashboard_server, "company_router", routing)
        monkeypatch.setattr(dashboard_server, "get_settings", lambda: settings)
        monkeypatch.setattr(
            mcp_server,
            "SessionLocal",
            mcp_server._ContextAwareSessionFactory(
                sessionmaker(bind=catalog, expire_on_commit=False)
            ),
        )
        store = InMemoryCredentialStore()
        store.save_session_token(owner.session_token)
        monkeypatch.setattr(mcp_server, "_MCP_CREDENTIAL_STORE", store)
        monkeypatch.setattr(mcp_server, "_OWNER_LOGIN_WINDOW_ENABLED", False)

        def read():
            _, response = asyncio.run(
                mcp_server.mcp.call_tool(
                    "finance_get_event", {"org_id": str(org_id), "event_id": str(event_id)}
                )
            )
            return response

        try:
            refused = read()
            assert refused["errors"] == ["DATABASE_SCHEMA_UPGRADE_REQUIRED"], refused
            assert refused["data"]["database_schema"]["actual_revisions"] == [
                "0001_business_baseline_v4"
            ]
            assert company_cli._check_schema()["status"] == "rejected"
            with pytest.raises(DashboardDataError, match="DATABASE_SCHEMA_UPGRADE_REQUIRED"):
                dashboard_server._dashboard_business_target(
                    catalog, query={"org_id": [str(org_id)]}, fixed_org_id=None
                )
            # Every write, even one not using the new column, is gated before attribution.
            _, write = asyncio.run(
                mcp_server.mcp.call_tool(
                    "finance_register_employee",
                    {
                        "request": {
                            "org_id": str(org_id),
                            "name": "must not be created",
                            "employee_code": "blocked-by-deployment",
                            "employment_start_date": "2026-03-01",
                        }
                    },
                )
            )
            assert write["errors"] == ["DATABASE_SCHEMA_UPGRADE_REQUIRED"], write
            assert _accounting_snapshot(engine) == before

            config = Config("alembic.ini")
            config.attributes["database_url_override"] = engine.url.render_as_string(
                hide_password=False
            )
            command.upgrade(config, "head")
            assert _accounting_snapshot(engine) == before
            restored = read()
            assert restored["status"] == "ok", restored
            assert restored["event"]["id"] == str(event_id)
            assert restored["facts_hash"]
            assert restored["vouchers"][0]["number"]
            assert restored["amendments"] == []
            assert restored["audit_log"]
            assert company_cli._check_schema()["status"] == "ok"
            assert _accounting_snapshot(engine) == before

            # A cached engine is not a permanently cached schema approval.
            with engine.begin() as connection:
                connection.execute(text("UPDATE alembic_version SET version_num='future_revision'"))
            again = read()
            assert again["errors"] == ["DATABASE_SCHEMA_UNSUPPORTED"], again
            with engine.connect() as connection:
                assert schema_state(connection)["ready"] is False
        finally:
            routing.dispose()
            catalog.dispose()
