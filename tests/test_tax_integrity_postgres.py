from __future__ import annotations

import json
import shutil
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, date, datetime
from hashlib import sha256
from threading import Barrier, Event

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.fixed_asset_service import FixedAssetService
from ai_accounting.models import (
    BusinessEvent,
    Evidence,
    FixedAssetDisposal,
    TaxPeriod,
    TaxPeriodSource,
    TaxRule,
)
from ai_accounting.schemas import (
    AcquireFixedAssetRequest,
    ActivateFixedAssetRequest,
    DisposeFixedAssetRequest,
    RecordEventRequest,
    ReverseEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


def _config(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _sale_request(org_id: uuid.UUID, *, key: str, business_date: date) -> RecordEventRequest:
    return RecordEventRequest.model_validate(
        {
            "org_id": org_id,
            "idempotency_key": key,
            "event_type": "service_credit_sale",
            "business_dates": {
                "business_date": business_date,
                "fulfillment_date": business_date,
                "payment_date": business_date,
                "tax_obligation_date": business_date,
                "posting_date": business_date,
            },
            "amounts": {"gross_amount_fen": 10_100},
            "counterparty": {"kind": "customer", "name": "税务测试客户"},
            "tax_facts": {
                "taxable": True,
                "rate_percent": "1",
                "invoice_type": "ordinary",
                "waive_exemption": False,
                "tax_due_on_event": True,
            },
        }
    )


def _preview(
    service: FinanceService, org_id: uuid.UUID, start_date: date, end_date: date
) -> dict[str, object]:
    return service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=org_id,
            start_date=start_date,
            end_date=end_date,
            adjustment_posting_date=end_date,
        )
    )


def _confirm(
    service: FinanceService,
    org_id: uuid.UUID,
    start_date: date,
    end_date: date,
    calculation_hash: str,
    key: str,
):
    return service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=org_id,
            start_date=start_date,
            end_date=end_date,
            adjustment_posting_date=end_date,
            calculation_hash=calculation_hash,
            idempotency_key=key,
        )
    )


def _assert_sql_rejected(engine: sa.Engine, sql: str, code: str, **parameters: object) -> None:
    with pytest.raises(DBAPIError, match=code):
        with engine.begin() as connection:
            connection.execute(sa.text(sql), parameters)


def test_tax_determinism_commit_guards_and_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer(
        "postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193",
        driver="psycopg",
    ) as postgres:  # noqa: E501
        url = postgres.get_connection_url(driver="psycopg")
        config = _config(url, monkeypatch)
        engine = sa.create_engine(url)
        try:
            with engine.connect() as connection:
                preexisting_extensions = set(
                    connection.execute(
                        sa.text(
                            "SELECT extname FROM pg_extension "
                            "WHERE extname IN ('btree_gist', 'pgcrypto')"
                        )
                    ).scalars()
                )
            command.upgrade(config, "head")
            command.check(config)
            with engine.connect() as connection:
                actions = dict(
                    connection.execute(
                        sa.text(
                            "SELECT extension_name, action FROM tax_determinism_extension_actions"
                        )
                    ).all()
                )
            assert actions == {
                extension_name: (
                    "reused" if extension_name in preexisting_extensions else "created"
                )
                for extension_name in ("btree_gist", "pgcrypto")
            }

            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    taxpayer_identification_number="91330106MA1234567T",
                    name="0010 PostgreSQL 门禁",
                    accounting_period_control_enabled=False,
                )
                source = FinanceService(session).record_event(
                    _sale_request(
                        organization.id,
                        key="tax-integrity-q1-source",
                        business_date=date(2026, 1, 15),
                    )
                )
                assert source.status == "posted", source.errors
                session.commit()
                source_event = session.get(BusinessEvent, source.event_id)
                rule_reference = next(
                    item for item in source_event.rule_trace if item.get("stage") == "rule_selected"
                )
                referenced_rule_id = session.scalar(
                    sa.select(TaxRule.id).where(
                        TaxRule.code == rule_reference["rule"],
                        TaxRule.version == rule_reference["version"],
                    )
                )
                org_id = organization.id
                source_event_id = source.event_id

            # The current organization row is now a guarded projection of the
            # immutable profile-version history, so direct configuration drift
            # is rejected before historical tax-rule protections are exercised.
            _assert_sql_rejected(
                engine,
                "UPDATE organizations SET jurisdiction = 'CN-DRIFTED' WHERE id = :org_id",
                "ORGANIZATION_PROFILE_PROJECTION_INVALID",
                org_id=org_id,
            )
            _assert_sql_rejected(
                engine,
                "UPDATE tax_rules SET source_url = source_url || '#drifted' WHERE id = :id",
                "TAX_RULE_IMMUTABLE",
                id=referenced_rule_id,
            )
            _assert_sql_rejected(
                engine,
                "DELETE FROM tax_rules WHERE id = :id",
                "TAX_RULE_IMMUTABLE",
                id=referenced_rule_id,
            )
            with Session(engine) as session:
                preview = _preview(
                    FinanceService(session),
                    org_id,
                    date(2026, 1, 1),
                    date(2026, 3, 31),
                )
                confirmed = _confirm(
                    FinanceService(session),
                    org_id,
                    date(2026, 1, 1),
                    date(2026, 3, 31),
                    str(preview["calculation_hash"]),
                    "tax-integrity-q1-confirm",
                )
                assert confirmed.status == "posted", confirmed.errors
                session.commit()
                period = session.scalar(
                    sa.select(TaxPeriod).where(TaxPeriod.adjustment_event_id == confirmed.event_id)
                )
                snapshot = session.scalar(
                    sa.select(TaxPeriodSource).where(TaxPeriodSource.tax_period_id == period.id)
                )
                assert snapshot.source_event_id == source_event_id
                period_id = period.id
                adjustment_event_id = confirmed.event_id
                vat_rule_id = period.vat_rule_id

            _assert_sql_rejected(
                engine,
                "UPDATE tax_periods SET calculation = CAST('{}' AS json) WHERE id = :id",
                "TAX_PERIOD_SNAPSHOT_IMMUTABLE",
                id=period_id,
            )
            _assert_sql_rejected(
                engine,
                "UPDATE tax_periods SET calculation_hash = :hash WHERE id = :id",
                "TAX_PERIOD_SNAPSHOT_IMMUTABLE",
                id=period_id,
                hash="d" * 64,
            )
            _assert_sql_rejected(
                engine,
                "UPDATE tax_periods SET calculation_hash_payload = '{}' WHERE id = :id",
                "TAX_PERIOD_SNAPSHOT_IMMUTABLE",
                id=period_id,
            )
            _assert_sql_rejected(
                engine,
                "UPDATE business_events SET facts = CAST('{}' AS json) WHERE id = :id",
                "final business events are immutable",
                id=adjustment_event_id,
            )
            _assert_sql_rejected(
                engine,
                "UPDATE business_events SET rule_trace = CAST('[]' AS json) WHERE id = :id",
                "final business events are immutable",
                id=adjustment_event_id,
            )
            _assert_sql_rejected(
                engine,
                """
                UPDATE voucher_lines SET debit_fen = debit_fen + 1
                 WHERE voucher_id = (
                    SELECT id FROM vouchers WHERE event_id = :event_id
                 )
                """,
                "lines of a final voucher are immutable",
                event_id=adjustment_event_id,
            )
            _assert_sql_rejected(
                engine,
                """
                UPDATE voucher_lines
                   SET account_id = (
                       SELECT id FROM accounts
                        WHERE org_id = :org_id AND system_role = 'cash'
                   )
                 WHERE voucher_id = (
                       SELECT id FROM vouchers WHERE event_id = :event_id
                 )
                """,
                "lines of a final voucher are immutable",
                org_id=org_id,
                event_id=adjustment_event_id,
            )
            _assert_sql_rejected(
                engine,
                """
                INSERT INTO voucher_lines (
                    id, org_id, voucher_id, line_number, account_id,
                    counterparty_id, debit_fen, credit_fen, memo
                ) SELECT :id, :org_id, voucher.id, 99, account.id,
                         NULL, 1, 0, 'extra'
                    FROM vouchers AS voucher
                    JOIN accounts AS account ON account.org_id = voucher.org_id
                   WHERE voucher.event_id = :event_id AND account.system_role = 'cash'
                """,
                "lines of a final voucher are immutable",
                id=uuid.uuid4(),
                org_id=org_id,
                event_id=adjustment_event_id,
            )
            _assert_sql_rejected(
                engine,
                "UPDATE tax_period_sources SET gross_fen = gross_fen + 1 WHERE tax_period_id = :id",
                "TAX_PERIOD_SNAPSHOT_IMMUTABLE",
                id=period_id,
            )
            _assert_sql_rejected(
                engine,
                "DELETE FROM tax_period_sources WHERE tax_period_id = :id",
                "TAX_PERIOD_SNAPSHOT_IMMUTABLE",
                id=period_id,
            )
            _assert_sql_rejected(
                engine,
                """
                INSERT INTO tax_period_sources (
                    org_id, tax_period_id, source_event_id,
                    gross_fen, net_fen, vat_fen, exemption_eligible
                ) VALUES (
                    :org_id, :period_id, :event_id, 10100, 10000, 100, true
                )
                """,
                "TAX_PERIOD_SNAPSHOT_IMMUTABLE",
                org_id=org_id,
                period_id=period_id,
                event_id=uuid.uuid4(),
            )
            _assert_sql_rejected(
                engine,
                "UPDATE business_events SET description = 'tampered' WHERE id = :id",
                "TAX_PERIOD_SOURCE_LOCKED",
                id=source_event_id,
            )
            _assert_sql_rejected(
                engine,
                "DELETE FROM business_events WHERE id = :id",
                "TAX_PERIOD_SOURCE_LOCKED",
                id=source_event_id,
            )
            with Session(engine) as session:
                blocked_reversal = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=source_event_id,
                        idempotency_key="tax-integrity-source-reversal-blocked",
                        reason="税期有效时不得冲正来源",
                        posting_date=date(2026, 4, 1),
                    )
                )
                assert blocked_reversal.status == "rejected"
                assert blocked_reversal.errors == ["TAX_PERIOD_SOURCE_LOCKED"]
                session.rollback()
            _assert_sql_rejected(
                engine,
                """
                INSERT INTO business_events (
                    id, org_id, idempotency_key, request_payload_hash, event_type, status,
                    description, facts, business_date, tax_obligation_date, posting_date,
                    rule_trace, rule_version, created_at
                ) VALUES (
                    :id, :org_id, 'late-taxable-source', :hash,
                    'service_credit_sale', 'posted', 'late source',
                    CAST(:facts AS json), DATE '2026-02-01', DATE '2026-02-01',
                    DATE '2026-02-01', CAST('[]' AS json), '2026.1', :created_at
                )
                """,
                "TAX_PERIOD_SOURCE_LOCKED",
                id=uuid.uuid4(),
                org_id=org_id,
                hash="a" * 64,
                facts=json.dumps(
                    {
                        "derived": {
                            "taxable_gross_fen": 10_100,
                            "net_sales_fen": 10_000,
                            "vat_fen": 100,
                            "exemption_eligible": True,
                        }
                    }
                ),
                created_at=datetime.now(UTC),
            )
            _assert_sql_rejected(
                engine,
                """
                WITH adjustment AS (
                    INSERT INTO business_events (
                        id, org_id, idempotency_key, request_payload_hash, event_type, status,
                        description, facts, business_date, posting_date, rule_trace,
                        rule_version, created_at
                    ) VALUES (
                        :event_id, :org_id, 'invalid-tax-boundary', :hash,
                        'tax_relief', 'draft', 'invalid boundary', CAST('{}' AS json),
                            DATE '2026-06-30', DATE '2026-06-30', CAST('[]' AS json),
                        '2026.1+2023.12', :created_at
                    ) RETURNING id
                )
                    INSERT INTO tax_periods (
                        id, org_id, start_date, end_date, rule_version, status,
                        calculation, calculation_hash, calculation_hash_payload,
                        filing_cycle_snapshot, jurisdiction_snapshot,
                        urban_maintenance_rate_snapshot, vat_rule_id, surtax_rule_id,
                        adjustment_event_id, adjustment_posting_date, created_at
                    )
                    SELECT :period_id, :org_id, DATE '2026-04-02', DATE '2026-06-30',
                           '2026.1+2023.12', 'posted', CAST('{}' AS json), :hash, '{}',
                           'quarterly', 'CN', 0.07000,
                           :vat_rule_id,
                       (SELECT id FROM tax_rules
                         WHERE code = 'small_scale_surtax_2023_2027'
                           AND jurisdiction = 'CN'),
                       adjustment.id, DATE '2026-06-30', :created_at
                  FROM adjustment
                """,
                "TAX_PERIOD_INVALID_BOUNDARY",
                event_id=uuid.uuid4(),
                period_id=uuid.uuid4(),
                org_id=org_id,
                hash=sha256(b"{}").hexdigest(),
                vat_rule_id=vat_rule_id,
                created_at=datetime.now(UTC),
            )
            _assert_sql_rejected(
                engine,
                "UPDATE tax_rules SET source_url = source_url || '#tampered' WHERE id = :id",
                "TAX_RULE_IMMUTABLE",
                id=vat_rule_id,
            )
            _assert_sql_rejected(
                engine,
                "DELETE FROM tax_rules WHERE id = :id",
                "TAX_RULE_IMMUTABLE",
                id=vat_rule_id,
            )
            _assert_sql_rejected(
                engine,
                """
                INSERT INTO tax_rules (
                    id, code, jurisdiction, effective_from, effective_to,
                    version, source_url, parameters
                ) SELECT :id, code, jurisdiction, DATE '2027-12-31', DATE '2028-12-31',
                         'overlap-at-boundary', source_url, parameters
                    FROM tax_rules WHERE id = :rule_id
                """,
                "TAX_RULE_EFFECTIVE_RANGE_OVERLAP",
                id=uuid.uuid4(),
                rule_id=vat_rule_id,
            )

            rule_barrier = Barrier(2)

            def insert_competing_rule(version: str) -> str:
                rule_barrier.wait()
                try:
                    with engine.begin() as connection:
                        connection.execute(
                            sa.text(
                                """
                                INSERT INTO tax_rules (
                                    id, code, jurisdiction, effective_from, effective_to,
                                    version, source_url, parameters
                                ) VALUES (
                                    :id, 'concurrent_rule_guard', 'CN',
                                    DATE '2026-01-01', DATE '2026-12-31', :version,
                                    'https://fgk.chinatax.gov.cn/zcfgk/c100012/c5247426/content.html',
                                    CAST('{}' AS json)
                                )
                                """
                            ),
                            {"id": uuid.uuid4(), "version": version},
                        )
                except DBAPIError as exc:
                    assert "TAX_RULE_EFFECTIVE_RANGE_OVERLAP" in str(exc)
                    return "conflict"
                return "inserted"

            with ThreadPoolExecutor(max_workers=2) as pool:
                rule_outcomes = list(pool.map(insert_competing_rule, ("a", "b")))
            assert sorted(rule_outcomes) == ["conflict", "inserted"]

            # A canonical period reversal releases the source period only after commit.
            # Organization tax configuration remains on its immutable versioned profile.
            with Session(engine) as session:
                reversed_period = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=adjustment_event_id,
                        idempotency_key="tax-integrity-q1-reverse-period",
                        reason="更正税期来源",
                        posting_date=date(2026, 4, 1),
                    )
                )
                assert reversed_period.status == "posted", reversed_period.errors
                session.commit()
                assert session.get(TaxPeriod, period_id).status == "reversed"
                reversed_source = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=source_event_id,
                        idempotency_key="tax-integrity-q1-reverse-source",
                        reason="更正原始销售事实",
                        posting_date=date(2026, 4, 1),
                    )
                )
                assert reversed_source.status == "posted", reversed_source.errors
                session.commit()
                corrected = FinanceService(session).record_event(
                    _sale_request(
                        org_id,
                        key="tax-integrity-q1-corrected-source",
                        business_date=date(2026, 1, 20),
                    )
                )
                assert corrected.status == "posted", corrected.errors
                refreshed = _preview(
                    FinanceService(session), org_id, date(2026, 1, 1), date(2026, 3, 31)
                )
                reposted = _confirm(
                    FinanceService(session),
                    org_id,
                    date(2026, 1, 1),
                    date(2026, 3, 31),
                    str(refreshed["calculation_hash"]),
                    "tax-integrity-q1-repost",
                )
                assert reposted.status == "posted", reposted.errors
                session.commit()
                statuses = session.scalars(
                    sa.select(TaxPeriod.status)
                    .where(TaxPeriod.org_id == org_id)
                    .order_by(TaxPeriod.created_at)
                ).all()
                assert statuses == ["reversed", "posted"]

                q2_source = FinanceService(session).record_event(
                    _sale_request(
                        org_id,
                        key="tax-integrity-q2-source",
                        business_date=date(2026, 4, 15),
                    )
                )
                assert q2_source.status == "posted", q2_source.errors
                q2_preview = _preview(
                    FinanceService(session), org_id, date(2026, 4, 1), date(2026, 6, 30)
                )
                q2_hash = str(q2_preview["calculation_hash"])
                session.commit()

            # Two direct service confirmations enter the same database lock domain.
            barrier = Barrier(2)

            def confirm_q2(key: str) -> tuple[str, list[str]]:
                with Session(engine) as session:
                    barrier.wait()
                    result = _confirm(
                        FinanceService(session),
                        org_id,
                        date(2026, 4, 1),
                        date(2026, 6, 30),
                        q2_hash,
                        key,
                    )
                    try:
                        session.commit()
                    except DBAPIError:
                        session.rollback()
                        return "raw_error", []
                    return str(result.status), result.errors

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(confirm_q2, ("tax-integrity-q2-a", "tax-integrity-q2-b")))
            assert sorted(status for status, _errors in outcomes) == ["posted", "rejected"]
            assert [errors for status, errors in outcomes if status == "rejected"] == [
                ["TAX_PERIOD_ALREADY_POSTED"]
            ]
            with engine.connect() as connection:
                assert (
                    connection.scalar(
                        sa.text(
                            """
                        SELECT COUNT(*) FROM tax_periods
                         WHERE org_id = :org_id AND status = 'posted'
                           AND start_date = DATE '2026-04-01'
                           AND end_date = DATE '2026-06-30'
                        """
                        ),
                        {"org_id": org_id},
                    )
                    == 1
                )

            # Reopen Q2 through the supported adjustment reversal so the same
            # effective-rule quarter can exercise later lock races without
            # relying on a future posting date.
            with Session(engine) as session:
                active_q2 = session.scalar(
                    sa.select(TaxPeriod).where(
                        TaxPeriod.org_id == org_id,
                        TaxPeriod.start_date == date(2026, 4, 1),
                        TaxPeriod.end_date == date(2026, 6, 30),
                        TaxPeriod.status == "posted",
                    )
                )
                reopened_q2 = FinanceService(session).reverse_event(
                    ReverseEventRequest(
                        org_id=org_id,
                        event_id=active_q2.adjustment_event_id,
                        idempotency_key="tax-integrity-q2-adjustment-reversal",
                        reason="并发验收前规范冲正税期调整",
                        posting_date=date(2026, 7, 1),
                    )
                )
                assert reopened_q2.status == "posted", reopened_q2.errors
                session.commit()

            # A source transaction that owns the organization lock must commit
            # before confirmation recalculates, producing a stable stale-hash result.
            with Session(engine) as session:
                q3_source = FinanceService(session).record_event(
                    _sale_request(
                        org_id,
                        key="tax-integrity-q3-source-before-preview",
                        business_date=date(2026, 4, 15),
                    )
                )
                assert q3_source.status == "posted", q3_source.errors
                q3_preview = _preview(
                    FinanceService(session), org_id, date(2026, 4, 1), date(2026, 6, 30)
                )
                q3_hash = str(q3_preview["calculation_hash"])
                session.commit()

            source_locked = Event()
            allow_source_commit = Event()
            confirm_started = Event()

            def add_q3_source_while_locked() -> tuple[str, list[str]]:
                with Session(engine) as session:
                    result = FinanceService(session).record_event(
                        _sale_request(
                            org_id,
                            key="tax-integrity-q3-concurrent-source",
                            business_date=date(2026, 5, 15),
                        )
                    )
                    session.flush()
                    source_locked.set()
                    assert allow_source_commit.wait(timeout=10)
                    try:
                        session.commit()
                    except DBAPIError:
                        session.rollback()
                        return "raw_error", []
                    return str(result.status), result.errors

            def confirm_q3_after_source_lock() -> tuple[str, list[str]]:
                assert source_locked.wait(timeout=10)
                with Session(engine) as session:
                    confirm_started.set()
                    result = _confirm(
                        FinanceService(session),
                        org_id,
                        date(2026, 4, 1),
                        date(2026, 6, 30),
                        q3_hash,
                        "tax-integrity-q3-confirm-stale",
                    )
                    try:
                        session.commit()
                    except DBAPIError:
                        session.rollback()
                        return "raw_error", []
                    return str(result.status), result.errors

            with ThreadPoolExecutor(max_workers=2) as pool:
                source_future = pool.submit(add_q3_source_while_locked)
                assert source_locked.wait(timeout=10)
                confirm_future = pool.submit(confirm_q3_after_source_lock)
                assert confirm_started.wait(timeout=10)
                allow_source_commit.set()
                source_outcome = source_future.result(timeout=10)
                confirm_outcome = confirm_future.result(timeout=10)
            assert source_outcome == ("posted", [])
            assert confirm_outcome == ("rejected", ["TAX_PERIOD_CALCULATION_STALE"])

            # Profile-backed configuration cannot be changed by updating the
            # latest organization projection directly. Lifecycle concurrency is
            # covered by the multi-company PostgreSQL suite; here the rejected
            # bypass leaves the preview hash valid for confirmation.
            with Session(engine) as session:
                q4_source = FinanceService(session).record_event(
                    _sale_request(
                        org_id,
                        key="tax-integrity-q4-source-before-config",
                        business_date=date(2026, 6, 15),
                    )
                )
                assert q4_source.status == "posted", q4_source.errors
                q4_preview = _preview(
                    FinanceService(session), org_id, date(2026, 4, 1), date(2026, 6, 30)
                )
                q4_hash = str(q4_preview["calculation_hash"])
                session.commit()

            _assert_sql_rejected(
                engine,
                "UPDATE organizations SET urban_maintenance_rate = 0.05 WHERE id = :org_id",
                "ORGANIZATION_PROFILE_PROJECTION_INVALID",
                org_id=org_id,
            )
            with Session(engine) as session:
                restored_q2 = _confirm(
                    FinanceService(session),
                    org_id,
                    date(2026, 4, 1),
                    date(2026, 6, 30),
                    q4_hash,
                    "tax-integrity-q2-after-rejected-profile-bypass",
                )
                assert restored_q2.status == "posted", restored_q2.errors
                session.commit()

            # Exercise the real fixed-asset service path against a closed Q2:
            # taxable sale is blocked, while a zero-income retirement is allowed.
            with Session(engine) as session:
                evidence = Evidence(
                    org_id=org_id,
                    sha256="f" * 64,
                    original_name="tax-integrity-fixed-asset.pdf",
                    media_type="application/pdf",
                    source="test",
                    size_bytes=1,
                    storage_path="test/tax-integrity-fixed-asset",
                )
                session.add(evidence)
                session.flush()
                fixed_asset_service = FixedAssetService(session)

                def acquire_and_activate_asset(asset_code: str) -> uuid.UUID:
                    acquired = fixed_asset_service.acquire_fixed_asset(
                        AcquireFixedAssetRequest.model_validate(
                            {
                                "org_id": org_id,
                                "idempotency_key": f"tax-integrity-acquire-{asset_code}",
                                "asset_code": asset_code,
                                "asset_name": asset_code,
                                "category": "production_equipment",
                                "expected_use_over_one_year": True,
                                "purchase_date": "2026-04-02",
                                "posting_date": "2026-04-02",
                                "cost_components": {
                                    "purchase_price_fen": 1_000_000,
                                    "noncreditable_tax_fen": 30_000,
                                    "transport_and_handling_fen": 10_000,
                                    "installation_and_direct_cost_fen": 10_000,
                                },
                                "supplier": {"kind": "supplier", "name": "PG固定资产供应商"},
                                "settlement_method": "payable",
                                "due_date": "2026-05-02",
                                "evidence_references": [evidence.id],
                                "claims_creditable_input_vat": False,
                            }
                        )
                    )
                    assert acquired.status == "posted", acquired.errors
                    activated = fixed_asset_service.activate_fixed_asset(
                        ActivateFixedAssetRequest.model_validate(
                            {
                                "org_id": org_id,
                                "asset_id": acquired.asset_id,
                                "idempotency_key": f"tax-integrity-activate-{asset_code}",
                                "activation_date": "2026-04-10",
                                "posting_date": "2026-04-10",
                                "useful_life_months": 13,
                                "residual_value_fen": 10_000,
                                "benefit_area": "management",
                                "evidence_references": [evidence.id],
                            }
                        )
                    )
                    assert activated.status == "posted", activated.errors
                    return acquired.asset_id

                sale_asset_id = acquire_and_activate_asset("FA-PG-TAX-LOCK-SALE")
                blocked_sale = fixed_asset_service.dispose_fixed_asset(
                    DisposeFixedAssetRequest.model_validate(
                        {
                            "org_id": org_id,
                            "asset_id": sale_asset_id,
                            "idempotency_key": "tax-integrity-dispose-sale",
                            "disposal_date": "2026-04-20",
                            "posting_date": "2026-04-20",
                            "disposal_kind": "sale",
                            "gross_proceeds_fen": 500_000,
                            "invoice_type": "ordinary",
                            "waive_exemption": False,
                            "settlement_method": "receivable",
                            "customer": {"kind": "customer", "name": "PG税期锁客户"},
                            "tax_obligation_date": "2026-04-20",
                            "clearance_cost_fen": 0,
                            "evidence_references": [evidence.id],
                        }
                    )
                )
                assert blocked_sale.status == "rejected"
                assert blocked_sale.errors == ["TAX_PERIOD_SOURCE_LOCKED"]
                assert (
                    session.scalar(
                        sa.select(FixedAssetDisposal).where(
                            FixedAssetDisposal.asset_id == sale_asset_id
                        )
                    )
                    is None
                )

                retirement_asset_id = acquire_and_activate_asset("FA-PG-TAX-LOCK-RETIREMENT")
                retirement = fixed_asset_service.dispose_fixed_asset(
                    DisposeFixedAssetRequest.model_validate(
                        {
                            "org_id": org_id,
                            "asset_id": retirement_asset_id,
                            "idempotency_key": "tax-integrity-dispose-retirement",
                            "disposal_date": "2026-04-20",
                            "posting_date": "2026-04-20",
                            "disposal_kind": "retirement",
                            "settlement_method": "none",
                            "clearance_cost_fen": 0,
                            "evidence_references": [evidence.id],
                        }
                    )
                )
                assert retirement.status == "posted", retirement.errors
                session.commit()

        finally:
            engine.dispose()
