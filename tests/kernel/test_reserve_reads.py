"""Reserve reads expose actual funds movements without a synthetic reserve balance."""

import itertools
import json
from types import SimpleNamespace

import pytest
from entity_fixture import seed_registration_entities
from material_fixture import supporting_text

from ai_accounting.kernel.dashboard import Dashboard
from ai_accounting.kernel.engine import Engine
from ai_accounting.kernel.reports import ReportClassification, _statements
from ai_accounting.kernel.schema_bundle import production_bundle
from ai_accounting.kernel.storage import Store


@pytest.fixture
def reserve_book(tmp_path):
    engine = Engine(
        Store.create(
            tmp_path / "company.sqlite",
            production_bundle(),
            "company",
            "911100000000000001",
            "database",
        )
    )
    proof = engine.register_evidence(
        b"Synthetic reserve funds evidence",
        "text/plain",
        "fixture",
        request_id="evidence",
    )["digest"]
    supporting_text(engine, proof)
    counter = itertools.count()

    def save(kind, subject, data):
        seed_registration_entities(engine, kind, data)
        return engine.save_fact(
            kind,
            subject,
            data,
            evidence=(proof,),
            expected_revision=0,
            request_id=f"save-{next(counter)}",
        )

    def publish(*subjects):
        preview = engine.preview(subjects)
        return engine.confirm(
            subjects,
            preview_digest=preview["digest"],
            epochs=preview["epochs"],
            request_id=f"publish-{next(counter)}",
        )

    return engine, save, publish, proof


def test_reserve_facts_read_as_external_money_on_all_supported_accounts(reserve_book):
    engine, save, publish, proof = reserve_book
    for direction in ("outflow", "inflow"):
        subject = f"platform-{direction}"
        save(
            "platform_movement",
            subject,
            {
                "period": "2026-09",
                "actual_date": "2026-09-03",
                "platform_account_id": "platform-a",
                "direction": direction,
                "amount_fen": 100 if direction == "outflow" else 10,
                "source_evidence_digest": proof,
                "source_location": subject,
                "transaction_reference": subject,
            },
        )
    publish("platform-outflow", "platform-inflow")

    facts = (
        ("managed_reserve_expense", "bank-expense", "bank_account_id", "bank-a", 300),
        ("managed_reserve_refund", "bank-refund", "bank_account_id", "bank-a", 30),
        ("managed_reserve_expense", "cash-expense", "cash_account_id", "cash-a", 200),
        ("managed_reserve_refund", "cash-refund", "cash_account_id", "cash-a", 20),
        (
            "managed_reserve_expense",
            "platform-expense",
            "platform_account_id",
            "platform-a",
            100,
        ),
        (
            "managed_reserve_refund",
            "platform-refund",
            "platform_account_id",
            "platform-a",
            10,
        ),
    )
    for kind, subject, account_field, account_id, amount in facts:
        data = {
            "period": "2026-09",
            "actual_date": "2026-09-03",
            account_field: account_id,
            "amount_fen": amount,
        }
        if account_field == "platform_account_id":
            data["movement_ids"] = [
                "platform-outflow" if kind == "managed_reserve_expense" else "platform-inflow"
            ]
        save(kind, subject, data)
    publish(*(subject for _kind, subject, _field, _account, _amount in facts))

    data = Dashboard(engine).funds("2026-09")["data"]

    assert data["inflow_fen"] == 60
    assert data["outflow_fen"] == 600
    assert data["net_change_fen"] == data["total_fen"] == -540
    assert data["internal_transfer_fen"] == 0
    reserve = [
        row for row in data["movements"] if row["component_kinds"][0].startswith("managed_reserve_")
    ]
    assert len(reserve) == 6
    assert {
        (row["component_kinds"][0], row["account_type"], row["direction"]) for row in reserve
    } == {
        ("managed_reserve_expense", account_type, "outflow")
        for account_type in ("bank", "cash", "payment_platform")
    } | {
        ("managed_reserve_refund", account_type, "inflow")
        for account_type in ("bank", "cash", "payment_platform")
    }
    assert not any(row["internal_transfer"] for row in reserve)
    assert {row["type"] for row in reserve} == {"备用金支出", "备用金退款"}
    assert {row["party"] for row in reserve} == {"未提供往来对象"}


def test_reserve_vouchers_show_actual_amounts_without_settlements(reserve_book):
    engine, save, publish, _proof = reserve_book
    save(
        "managed_reserve_expense",
        "expense",
        {
            "period": "2026-09",
            "actual_date": "2026-09-02",
            "bank_account_id": "bank-a",
            "amount_fen": 300,
        },
    )
    save(
        "managed_reserve_refund",
        "refund",
        {
            "period": "2026-09",
            "actual_date": "2026-09-03",
            "bank_account_id": "bank-a",
            "amount_fen": 100,
        },
    )
    publish("expense", "refund")

    vouchers = Dashboard(engine).brief("2026-09")["data"]["vouchers"]
    reserve = {row["kind"]: row for row in vouchers}

    expense = reserve["managed_reserve_expense"]
    assert expense["type"] == "备用金支出"
    assert expense["list_summary"] == "支出备用金"
    assert expense["business_amount_fen"] == 300
    assert expense["business_amount_label"] == "备用金实际支出"
    assert expense["fund_outflow_fen"] == 300
    assert expense["settlements"] == []

    refund = reserve["managed_reserve_refund"]
    assert refund["type"] == "备用金退款"
    assert refund["list_summary"] == "收到备用金退款"
    assert refund["business_amount_fen"] == 100
    assert refund["business_amount_label"] == "备用金实际退款"
    assert refund["fund_inflow_fen"] == 100
    assert refund["settlements"] == []


def test_reserve_refund_reduces_management_expense_without_clipping_report_signs():
    def classification(version, line_no, amount):
        return ReportClassification.model_validate_json(
            json.dumps(
                {
                    "period": "2026-09",
                    "voucher_version_id": version,
                    "profit_details": [
                        {
                            "line_no": line_no,
                            "detail_code": "management_other",
                            "amount_fen": amount,
                        }
                    ],
                }
            )
        )

    def row(account, amount, version, line_no, *, cashflow=None, classified=None):
        return {
            "period": 24308,
            "account": account,
            "amount": amount,
            "party": None,
            "classification": classified,
            "kind": "managed_reserve_expense"
            if version == "reserve-expense"
            else "managed_reserve_refund",
            "values": {},
            "cash_source": SimpleNamespace(),
            "fact": SimpleNamespace(),
            "cashflow": cashflow,
            "version_id": version,
            "reverses_id": None,
            "line_no": line_no,
        }

    expense_classification = classification("reserve-expense", 1, 300)
    refund_classification = classification("reserve-refund", 2, 100)
    rows = [
        row("5602", 300, "reserve-expense", 1, classified=expense_classification),
        row("1002", -300, "reserve-expense", 2, cashflow="managed_reserve_outflow"),
        row("1002", 100, "reserve-refund", 1, cashflow="other_operating_receipts"),
        row("5602", -100, "reserve-refund", 2, classified=refund_classification),
    ]
    issues = []

    statements = _statements(rows, 24306, 24300, 24308, issues)

    assert not issues
    assert statements["profit_statement"]["14"]["current_fen"] == 200
    assert statements["cash_flow_statement"]["2"]["current_fen"] == 100
    assert statements["cash_flow_statement"]["6"]["current_fen"] == 300
    assert statements["cash_flow_statement"]["20"]["current_fen"] == -200
