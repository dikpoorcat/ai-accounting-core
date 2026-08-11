from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy import create_engine, inspect

from ai_accounting.models import (
    AccountingPeriod,
    AccountingPeriodAction,
    AccountingPeriodCalendar,
    AccountingPeriodClose,
    AccountingPeriodCloseSource,
    BusinessEventDependency,
    Organization,
    TaxPeriod,
)
from alembic import command


def _config(database_url: str) -> Config:
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _insert_org(connection: sa.Connection, org_id: uuid.UUID) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO organizations (
                id, name, taxpayer_type, filing_cycle, jurisdiction,
                urban_maintenance_rate, accounting_standard, created_at
            ) VALUES (
                :id, '期间迁移测试', 'small_scale', 'quarterly', 'CN',
                0.07, 'small_enterprise', :created_at
            )
            """
        ),
        {"id": org_id.hex, "created_at": datetime.now(UTC)},
    )


def _insert_event(
    connection: sa.Connection,
    *,
    event_id: uuid.UUID,
    org_id: uuid.UUID,
    key: str,
    event_type: str,
    facts: dict[str, object],
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO business_events (
                id, org_id, idempotency_key, request_payload_hash, event_type,
                status, description, facts, business_date, fulfillment_date,
                invoice_date, payment_date, tax_obligation_date, posting_date,
                rule_trace, rule_version, reversed_by_event_id, created_at
            ) VALUES (
                :id, :org_id, :key, :hash, :event_type, 'posted', '', :facts,
                '2026-03-01', NULL, NULL, NULL, NULL, '2026-03-01',
                :trace, NULL, NULL, :created_at
            )
            """
        ),
        {
            "id": event_id.hex,
            "org_id": org_id.hex,
            "key": key,
            "hash": "1" * 64,
            "event_type": event_type,
            "facts": json.dumps(facts),
            "trace": json.dumps([]),
            "created_at": datetime.now(UTC),
        },
    )


def test_accounting_period_metadata_contract() -> None:
    assert len("0012_accounting_period_close") <= 32
    assert Organization.__table__.c.accounting_period_control_enabled.default.arg is True
    assert Organization.__table__.c.accounting_period_control_enabled.server_default is not None
    assert TaxPeriod.__table__.c.adjustment_posting_date.nullable is False
    assert AccountingPeriod.__table__.c.generation_action_id.nullable is False
    assert {
        AccountingPeriodAction.__tablename__,
        AccountingPeriodCalendar.__tablename__,
        AccountingPeriodClose.__tablename__,
        AccountingPeriodCloseSource.__tablename__,
        BusinessEventDependency.__tablename__,
    } == {
        "accounting_period_actions",
        "accounting_period_calendars",
        "accounting_period_closes",
        "accounting_period_close_sources",
        "business_event_dependencies",
    }


def test_legacy_period_preflight_runs_before_first_ddl(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'legacy-period.db').as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "0011_intangible_borrowings")
    engine = create_engine(database_url)
    org_id = uuid.uuid4()
    try:
        with engine.begin() as connection:
            _insert_org(connection, org_id)
            connection.execute(
                sa.text(
                    """
                    INSERT INTO accounting_periods (
                        id, org_id, start_date, end_date, status, closed_at
                    ) VALUES (:id, :org_id, '2026-03-01', '2026-03-31', 'open', NULL)
                    """
                ),
                {"id": uuid.uuid4().hex, "org_id": org_id.hex},
            )
        with pytest.raises(RuntimeError, match="ACCOUNTING_PERIOD_LEGACY_PERIOD_PRECHECK_FAILED"):
            command.upgrade(config, "head")
        assert "accounting_period_actions" not in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert connection.scalar(sa.text("SELECT version_num FROM alembic_version")) == (
                "0011_intangible_borrowings"
            )
    finally:
        engine.dispose()


def test_dependency_pollution_preflight_and_proven_backfill(tmp_path) -> None:
    bad_url = f"sqlite+pysqlite:///{(tmp_path / 'bad-dependency.db').as_posix()}"
    bad_config = _config(bad_url)
    command.upgrade(bad_config, "0011_intangible_borrowings")
    bad_engine = create_engine(bad_url)
    org_id, parent_id, child_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        with bad_engine.begin() as connection:
            _insert_org(connection, org_id)
            _insert_event(
                connection,
                event_id=parent_id,
                org_id=org_id,
                key="bad-parent",
                event_type="service_credit_sale",
                facts={"amounts": {"gross_amount_fen": 100}},
            )
            _insert_event(
                connection,
                event_id=child_id,
                org_id=org_id,
                key="bad-child",
                event_type="customer_refund",
                facts={
                    "amounts": {"amount_fen": 100},
                    "details": {
                        "refund_kind": "sale_return",
                        "original_event_id": str(parent_id),
                    },
                },
            )
        with pytest.raises(RuntimeError, match="BUSINESS_EVENT_DEPENDENCY_PRECHECK_FAILED"):
            command.upgrade(bad_config, "head")
        assert "business_event_dependencies" not in inspect(bad_engine).get_table_names()
    finally:
        bad_engine.dispose()

    good_url = f"sqlite+pysqlite:///{(tmp_path / 'good-dependency.db').as_posix()}"
    good_config = _config(good_url)
    command.upgrade(good_config, "0011_intangible_borrowings")
    good_engine = create_engine(good_url)
    org_id, parent_id, child_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    try:
        with good_engine.begin() as connection:
            _insert_org(connection, org_id)
            _insert_event(
                connection,
                event_id=parent_id,
                org_id=org_id,
                key="good-parent",
                event_type="customer_advance",
                facts={"amounts": {"gross_amount_fen": 100}},
            )
            _insert_event(
                connection,
                event_id=child_id,
                org_id=org_id,
                key="good-child",
                event_type="customer_refund",
                facts={
                    "amounts": {"amount_fen": 100},
                    "details": {
                        "refund_kind": "advance",
                        "original_event_id": str(parent_id),
                    },
                },
            )
        command.upgrade(good_config, "head")
        with good_engine.connect() as connection:
            row = connection.execute(
                sa.text("SELECT dependency_kind, amount_fen FROM business_event_dependencies")
            ).one()
            assert row == ("advance_refund", 100)
        command.downgrade(good_config, "0011_intangible_borrowings")
        assert "business_event_dependencies" not in inspect(good_engine).get_table_names()
    finally:
        good_engine.dispose()


def test_existing_org_is_legacy_false_and_new_org_defaults_true(tmp_path) -> None:
    database_url = f"sqlite+pysqlite:///{(tmp_path / 'org-default.db').as_posix()}"
    config = _config(database_url)
    command.upgrade(config, "0011_intangible_borrowings")
    engine = create_engine(database_url)
    old_id, new_id = uuid.uuid4(), uuid.uuid4()
    try:
        with engine.begin() as connection:
            _insert_org(connection, old_id)
        command.upgrade(config, "head")
        with engine.begin() as connection:
            _insert_org(connection, new_id)
            rows = dict(
                connection.execute(
                    sa.text(
                        "SELECT id, accounting_period_control_enabled FROM organizations "
                        "WHERE id IN (:old_id, :new_id)"
                    ),
                    {"old_id": old_id.hex, "new_id": new_id.hex},
                ).all()
            )
        assert rows[old_id.hex] == 0
        assert rows[new_id.hex] == 1
        command.check(config)
    finally:
        engine.dispose()
