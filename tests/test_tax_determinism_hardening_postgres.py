from __future__ import annotations

import json
import shutil
import uuid
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import UTC, date, datetime
from hashlib import sha256
from threading import Barrier
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session
from testcontainers.community.postgres import PostgresContainer

from ai_accounting.coa import seed_organization
from ai_accounting.models import Account, TaxPeriod
from ai_accounting.schemas import (
    RecordEventRequest,
    TaxPeriodConfirmRequest,
    TaxPeriodPreviewRequest,
)
from ai_accounting.service import FinanceService
from alembic import command

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(shutil.which("docker") is None, reason="Docker CLI is not installed"),
]


GraphMutation = Callable[[dict[str, Any]], None]


def _config(database_url: str, monkeypatch: pytest.MonkeyPatch) -> Config:
    monkeypatch.setenv("DATABASE_URL", database_url)
    config = Config("alembic.ini")
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def _sale_request(
    org_id: uuid.UUID,
    *,
    key: str,
    invoice_type: str,
    business_date: date = date(2026, 1, 15),
) -> RecordEventRequest:
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
                "invoice_type": invoice_type,
                "waive_exemption": False,
                "tax_due_on_event": True,
            },
        }
    )


def _preview(service: FinanceService, org_id: uuid.UUID) -> dict[str, Any]:
    return service.preview_tax_period(
        TaxPeriodPreviewRequest(
            org_id=org_id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
        )
    )


def _confirm(service: FinanceService, org_id: uuid.UUID, calculation_hash: str, key: str):
    return service.confirm_tax_period(
        TaxPeriodConfirmRequest(
            org_id=org_id,
            start_date=date(2026, 1, 1),
            end_date=date(2026, 3, 31),
            adjustment_posting_date=date(2026, 3, 31),
            calculation_hash=calculation_hash,
            idempotency_key=key,
        )
    )


def _seed_graph(engine: sa.Engine, label: str) -> dict[str, Any]:
    with Session(engine) as session:
        organization = seed_organization(
            session,
            name=f"税务伪造验收-{label}",
            accounting_period_control_enabled=False,
        )
        service = FinanceService(session)
        ordinary = service.record_event(
            _sale_request(
                organization.id,
                key=f"forgery-{label}-ordinary",
                invoice_type="ordinary",
            )
        )
        special = service.record_event(
            _sale_request(
                organization.id,
                key=f"forgery-{label}-special",
                invoice_type="special",
                business_date=date(2026, 2, 15),
            )
        )
        assert ordinary.status.value == "posted", ordinary.errors
        assert special.status.value == "posted", special.errors
        preview = _preview(service, organization.id)
        assert preview["status"] == "calculated", preview
        assert preview["vat_relief_fen"] > 0
        assert preview["surtax_total_fen"] > 0
        accounts = {
            account.system_role: account.id
            for account in session.scalars(
                sa.select(Account).where(Account.org_id == organization.id)
            ).all()
            if account.system_role
        }
        org_id = organization.id
        session.commit()

    result = deepcopy(preview)
    result.pop("status")
    lines = [
        {
            "role": "vat_payable",
            "debit_fen": result["vat_relief_fen"],
            "credit_fen": 0,
        },
        {
            "role": "tax_relief_income",
            "debit_fen": 0,
            "credit_fen": result["vat_relief_fen"],
        },
        {
            "role": "taxes_and_surcharges",
            "debit_fen": result["surtax_total_fen"],
            "credit_fen": 0,
        },
        {
            "role": "surtax_payable",
            "debit_fen": 0,
            "credit_fen": result["surtax_total_fen"],
        },
    ]
    return {
        "org_id": org_id,
        "event_id": uuid.uuid4(),
        "period_id": uuid.uuid4(),
        "voucher_id": uuid.uuid4(),
        "result": result,
        "event_facts": {"tax_period": deepcopy(result)},
        "event_trace": deepcopy(result["trace"]),
        "event_rule_version": result["rule_version"],
        "period_calculation": deepcopy(result),
        "calculation_hash": result["calculation_hash"],
        "calculation_hash_payload": result["calculation_hash_payload"],
        "sources": deepcopy(result["source_event_snapshots"]),
        "accounts": accounts,
        "lines": lines,
        "now": datetime.now(UTC),
        "label": label,
    }


def _insert_graph(connection: sa.Connection, graph: dict[str, Any]) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO business_events (
                id, org_id, idempotency_key, request_payload_hash, event_type, status,
                description, facts, business_date, fulfillment_date, invoice_date,
                payment_date, tax_obligation_date, posting_date, rule_trace,
                rule_version, reversed_by_event_id, created_at
            ) VALUES (
                :event_id, :org_id, :idempotency_key, :request_hash,
                'tax_relief', 'draft', 'direct SQL forged tax period', CAST(:facts AS json),
                DATE '2026-03-31', NULL, NULL, NULL, DATE '2026-03-31',
                DATE '2026-03-31', CAST(:trace AS json), :rule_version, NULL, :created_at
            )
            """
        ),
        {
            "event_id": graph["event_id"],
            "org_id": graph["org_id"],
            "idempotency_key": f"forged-tax-period-{graph['label']}",
            "request_hash": "a" * 64,
            "facts": json.dumps(graph["event_facts"], ensure_ascii=False),
            "trace": json.dumps(graph["event_trace"], ensure_ascii=False),
            "rule_version": graph["event_rule_version"],
            "created_at": graph["now"],
        },
    )
    connection.execute(
        sa.text(
            """
            INSERT INTO vouchers (
                id, org_id, event_id, voucher_number, posting_date, description,
                status, reversal_of_voucher_id, posted_at
            ) VALUES (
                :voucher_id, :org_id, :event_id, :voucher_number, DATE '2026-03-31',
                'direct SQL forged tax period', 'draft', NULL, :posted_at
            )
            """
        ),
        {
            "voucher_id": graph["voucher_id"],
            "org_id": graph["org_id"],
            "event_id": graph["event_id"],
            "voucher_number": f"202603-{graph['label'][:12]}",
            "posted_at": graph["now"],
        },
    )
    for line_number, line in enumerate(graph["lines"], start=1):
        connection.execute(
            sa.text(
                """
                INSERT INTO voucher_lines (
                    id, org_id, voucher_id, line_number, account_id, counterparty_id,
                    debit_fen, credit_fen, memo
                ) VALUES (
                    :id, :org_id, :voucher_id, :line_number, :account_id, NULL,
                    :debit_fen, :credit_fen, ''
                )
                """
            ),
            {
                "id": uuid.uuid4(),
                "org_id": graph["org_id"],
                "voucher_id": graph["voucher_id"],
                "line_number": line_number,
                "account_id": graph["accounts"][line["role"]],
                "debit_fen": line["debit_fen"],
                "credit_fen": line["credit_fen"],
            },
        )
    connection.execute(
        sa.text("UPDATE vouchers SET status = 'posted' WHERE id = :id"),
        {"id": graph["voucher_id"]},
    )
    result = graph["result"]
    connection.execute(
        sa.text(
            """
            INSERT INTO tax_periods (
                id, org_id, start_date, end_date, adjustment_posting_date,
                rule_version, status,
                calculation, calculation_hash, calculation_hash_payload,
                filing_cycle_snapshot, jurisdiction_snapshot,
                urban_maintenance_rate_snapshot, vat_rule_id, surtax_rule_id,
                adjustment_event_id, created_at
            ) VALUES (
                :period_id, :org_id, DATE '2026-01-01', DATE '2026-03-31',
                DATE '2026-03-31',
                :rule_version, 'posted', CAST(:calculation AS json), :calculation_hash,
                :calculation_hash_payload, :filing_cycle, :jurisdiction,
                :urban_maintenance_rate, :vat_rule_id, :surtax_rule_id,
                :event_id, :created_at
            )
            """
        ),
        {
            "period_id": graph["period_id"],
            "org_id": graph["org_id"],
            "rule_version": result["rule_version"],
            "calculation": json.dumps(graph["period_calculation"], ensure_ascii=False),
            "calculation_hash": graph["calculation_hash"],
            "calculation_hash_payload": graph["calculation_hash_payload"],
            "filing_cycle": result["filing_cycle"],
            "jurisdiction": result["vat_rule"]["jurisdiction"],
            "urban_maintenance_rate": json.loads(result["calculation_hash_payload"])[
                "organization"
            ]["urban_maintenance_rate"],
            "vat_rule_id": uuid.UUID(result["vat_rule_id"]),
            "surtax_rule_id": uuid.UUID(result["surtax_rule_id"]),
            "event_id": graph["event_id"],
            "created_at": graph["now"],
        },
    )
    for source in graph["sources"]:
        connection.execute(
            sa.text(
                """
                INSERT INTO tax_period_sources (
                    org_id, tax_period_id, source_event_id,
                    gross_fen, net_fen, vat_fen, exemption_eligible
                ) VALUES (
                    :org_id, :period_id, :source_event_id,
                    :gross_fen, :net_fen, :vat_fen, :exemption_eligible
                )
                """
            ),
            {
                "org_id": graph["org_id"],
                "period_id": graph["period_id"],
                "source_event_id": uuid.UUID(source["event_id"]),
                "gross_fen": source["gross_fen"],
                "net_fen": source["net_fen"],
                "vat_fen": source["vat_fen"],
                "exemption_eligible": source["exemption_eligible"],
            },
        )
    connection.execute(
        sa.text("UPDATE business_events SET status = 'posted' WHERE id = :id"),
        {"id": graph["event_id"]},
    )


def _coherently_forge_rule_snapshot(graph: dict[str, Any]) -> None:
    result = deepcopy(graph["result"])
    hash_payload = json.loads(result["calculation_hash_payload"])
    forged_url = "https://example.invalid/forged-official-rule"
    hash_payload["vat_rule"]["source_url"] = forged_url
    payload_text = json.dumps(
        hash_payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    forged_hash = sha256(payload_text.encode("utf-8")).hexdigest()
    result["source_url"] = forged_url
    result["vat_rule"]["source_url"] = forged_url
    result["calculation_hash_payload"] = payload_text
    result["calculation_hash"] = forged_hash
    result["trace"][-1]["sha256"] = forged_hash
    graph["result"] = result
    graph["period_calculation"] = deepcopy(result)
    graph["event_facts"] = {"tax_period": deepcopy(result)}
    graph["event_trace"] = deepcopy(result["trace"])
    graph["calculation_hash_payload"] = payload_text
    graph["calculation_hash"] = forged_hash


def _forge_period_calculation(graph: dict[str, Any]) -> None:
    graph["period_calculation"]["threshold_fen"] += 1


def _forge_event_facts(graph: dict[str, Any]) -> None:
    graph["event_facts"]["tax_period"]["gross_sales_fen"] += 1


def _forge_trace(graph: dict[str, Any]) -> None:
    graph["event_trace"] = graph["event_trace"][:-1]


def _forge_hash_pair(graph: dict[str, Any]) -> None:
    graph["calculation_hash_payload"] = "{}"
    graph["calculation_hash"] = sha256(b"{}").hexdigest()


def _forge_source_snapshot(graph: dict[str, Any]) -> None:
    graph["sources"][0]["gross_fen"] += 1


def _forge_balanced_wrong_role(graph: dict[str, Any]) -> None:
    graph["lines"][0]["role"] = "bank"


def _forge_balanced_wrong_amount(graph: dict[str, Any]) -> None:
    graph["lines"][0]["debit_fen"] += 1
    graph["lines"][1]["credit_fen"] += 1


def _forge_balanced_extra_lines(graph: dict[str, Any]) -> None:
    graph["lines"].extend(
        [
            {"role": "bank", "debit_fen": 1, "credit_fen": 0},
            {"role": "paid_in_capital", "debit_fen": 0, "credit_fen": 1},
        ]
    )


FORGERIES: tuple[tuple[str, GraphMutation], ...] = (
    ("hash-payload", _forge_hash_pair),
    ("period-calculation", _forge_period_calculation),
    ("event-facts", _forge_event_facts),
    ("event-trace", _forge_trace),
    ("rule-snapshot", _coherently_forge_rule_snapshot),
    ("source-snapshot", _forge_source_snapshot),
    ("balanced-wrong-role", _forge_balanced_wrong_role),
    ("balanced-wrong-amount", _forge_balanced_wrong_amount),
    ("balanced-extra-lines", _forge_balanced_extra_lines),
)


def test_direct_sql_forgeries_and_concurrent_confirm_are_closed_at_commit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with PostgresContainer("postgres:17-alpine@sha256:742f40ea20b9ff2ff31db5458d127452988a2164df9e17441e191f3b72252193", driver="psycopg") as postgres:  # noqa: E501
        database_url = postgres.get_connection_url(driver="psycopg")
        engine = sa.create_engine(database_url)
        try:
            command.upgrade(_config(database_url, monkeypatch), "head")
            for label, mutate in FORGERIES:
                graph = _seed_graph(engine, label)
                mutate(graph)
                with pytest.raises(DBAPIError, match="TAX_PERIOD_SNAPSHOT_IMMUTABLE"):
                    with engine.begin() as connection:
                        _insert_graph(connection, graph)

            with Session(engine) as session:
                organization = seed_organization(
                    session,
                    name="双连接税期确认硬化验收",
                    accounting_period_control_enabled=False,
                )
                service = FinanceService(session)
                for invoice_type in ("ordinary", "special"):
                    source = service.record_event(
                        _sale_request(
                            organization.id,
                            key=f"concurrent-source-{invoice_type}",
                            invoice_type=invoice_type,
                        )
                    )
                    assert source.status.value == "posted", source.errors
                preview = _preview(service, organization.id)
                calculation_hash = str(preview["calculation_hash"])
                org_id = organization.id
                session.commit()

            barrier = Barrier(2)

            def concurrent_confirm(key: str) -> tuple[str, list[str], str | None]:
                with Session(engine) as session:
                    barrier.wait()
                    result = _confirm(
                        FinanceService(session),
                        org_id,
                        calculation_hash,
                        key,
                    )
                    outer_error: str | None = None
                    try:
                        session.commit()
                    except DBAPIError as exc:
                        session.rollback()
                        outer_error = str(exc)
                    return result.status.value, result.errors, outer_error

            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(
                    pool.map(
                        concurrent_confirm,
                        ("hardening-concurrent-a", "hardening-concurrent-b"),
                    )
                )
            assert all(outer_error is None for _, _, outer_error in outcomes), outcomes
            assert sorted(status for status, _, _ in outcomes) == ["posted", "rejected"]
            loser = next(item for item in outcomes if item[0] == "rejected")
            assert loser[1] == ["TAX_PERIOD_ALREADY_POSTED"]
            with Session(engine) as session:
                assert session.scalar(
                    sa.select(sa.func.count())
                    .select_from(TaxPeriod)
                    .where(TaxPeriod.org_id == org_id, TaxPeriod.status == "posted")
                ) == 1
        finally:
            engine.dispose()
