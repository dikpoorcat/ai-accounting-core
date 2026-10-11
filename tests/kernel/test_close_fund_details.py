"""Frozen exact sources survive later definitions and continue account balances."""

import sqlite3
from copy import deepcopy

import pytest
from close_storage_fixture import stored_manifest, use_v4_close_manifest
from entity_fixture import seed_entities
from test_identity_corrections import identity_engine as identity_engine_fixture
from test_reports import book as book_fixture
from test_reports import scenario

from ai_accounting.kernel.close_fund_details import (
    build_fund_close_snapshot,
    require_fund_close_snapshot,
    verify_fund_close_snapshot,
)
from ai_accounting.kernel.contracts import KernelError
from ai_accounting.kernel.types import YearMonth

book = book_fixture
identity_engine = identity_engine_fixture


@pytest.fixture
def fund_sources(monkeypatch):
    from ai_accounting.kernel import close_fund_details, fund_accounts, fund_line_details

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute(
        "CREATE TABLE voucher_line(version_id TEXT,line_no INTEGER,account TEXT,"
        "debit INTEGER,credit INTEGER)"
    )
    connection.execute("INSERT INTO voucher_line VALUES('v1',1,'1002',75,0)")
    definitions = [
        {
            "entity_id": "bank-a",
            "detail_code": "1002.001",
            "parent_account": "1002",
            "identity": {"bank_account_number": "12345678"},
            "digest": "a" * 64,
        },
        {
            "entity_id": "bank-zero",
            "detail_code": "1002.002",
            "parent_account": "1002",
            "identity": {"bank_account_number": "87654321"},
            "digest": "b" * 64,
        },
    ]

    def selected(_connection, entity_ids=None):
        return deepcopy(
            [item for item in definitions if entity_ids is None or item["entity_id"] in entity_ids]
        )

    def snapshot(_connection, period, *, version_ids=None, calculation_ids=None):
        return {
            "contract": "ai-accounting-kernel/2/fund-line-details/1",
            "period": period,
            "activation": YearMonth("2026-09").ordinal,
            "opening": [],
            "opening_lines": [],
            "lines": (
                [
                    {
                        "version_id": "v1",
                        "line_no": 1,
                        "entity_id": "bank-a",
                        "detail_code": "1002.001",
                        "parent_account": "1002",
                    }
                ]
                if "v1" in version_ids
                else []
            ),
        }

    monkeypatch.setattr(fund_accounts, "fund_account_definitions", selected)
    monkeypatch.setattr(
        fund_accounts,
        "historical_fund_account",
        lambda connection, entity_id, checksum: selected(connection, [entity_id])[0],
        raising=False,
    )
    monkeypatch.setattr(fund_line_details, "snapshot_fund_details", snapshot)
    monkeypatch.setattr(close_fund_details, "_verify_account_balances", lambda *args: None)
    yield connection, definitions
    connection.close()


def manifest(period="2026-09", amount=75, vouchers=None):
    return {
        "format_version": 5,
        "period": period,
        "vouchers": [{"id": "v1"}] if vouchers is None else vouchers,
        "opening_calculation_id": None,
        "previous_close_period": None,
        "previous_close_digest": None,
        "trial_balance": [{"account": "1002", "debit": amount, "credit": 0}],
    }


def test_freeze_preserves_zero_accounts_and_exact_lines(fund_sources):
    connection, _ = fund_sources
    close = manifest()
    close["fund_details"] = build_fund_close_snapshot(connection, close)
    assert close["fund_details"]["balances"] == [
        {
            "entity_id": "bank-a",
            "detail_code": "1002.001",
            "parent_account": "1002",
            "opening_fen": 0,
            "debit_fen": 75,
            "credit_fen": 0,
            "adjustment_fen": 0,
            "ending_fen": 75,
        },
        {
            "entity_id": "bank-zero",
            "detail_code": "1002.002",
            "parent_account": "1002",
            "opening_fen": 0,
            "debit_fen": 0,
            "credit_fen": 0,
            "adjustment_fen": 0,
            "ending_fen": 0,
        },
    ]
    verify_fund_close_snapshot(connection, close)
    connection.execute("UPDATE voucher_line SET debit=76 WHERE version_id='v1'")
    with pytest.raises(KernelError, match="冻结"):
        verify_fund_close_snapshot(connection, close)


def test_later_definition_does_not_change_frozen_coverage(fund_sources):
    connection, definitions = fund_sources
    close = manifest()
    close["fund_details"] = build_fund_close_snapshot(connection, close)
    definitions.append(
        {
            "entity_id": "cash-later",
            "detail_code": "1001.001",
            "parent_account": "1001",
            "identity": {"display_name": "cash"},
            "digest": "c" * 64,
        }
    )
    verify_fund_close_snapshot(connection, close)
    definitions[0]["identity"]["bank_account_number"] = "changed"
    with pytest.raises(KernelError):
        verify_fund_close_snapshot(connection, close)


def test_next_month_continues_exact_verified_ending(fund_sources):
    connection, _ = fund_sources
    previous = manifest()
    previous["fund_details"] = build_fund_close_snapshot(connection, previous)
    current = manifest("2026-10", vouchers=[])
    current["fund_details"] = build_fund_close_snapshot(
        connection, current, previous_manifest=previous
    )
    assert current["fund_details"]["balances"][0]["opening_fen"] == 75
    verify_fund_close_snapshot(connection, current, previous_manifest=previous)
    with pytest.raises(KernelError):
        verify_fund_close_snapshot(connection, current)


def test_parent_trial_requires_complete_account_assignments(fund_sources):
    connection, _ = fund_sources
    with pytest.raises(KernelError):
        build_fund_close_snapshot(connection, manifest(amount=76))


def test_v4_verification_has_no_new_required_fields():
    verify_fund_close_snapshot(None, {"format_version": 4})


def test_snapshot_digest_covers_zero_balance_definitions(fund_sources):
    connection, _ = fund_sources
    frozen = build_fund_close_snapshot(connection, manifest())
    frozen["definitions"][1]["identity"] = {"bank_account_number": "changed"}
    with pytest.raises(KernelError):
        require_fund_close_snapshot(frozen)


def test_typed_funds_close_preserves_definitions_and_exact_journal_sources(book):
    engine, _, _, close = book
    scenario(book)
    seed_entities(engine, [("unused-bank", "fund_account", "bank")])
    close("2026-01")
    with engine.store.connection(read_only=True) as connection:
        january = stored_manifest(connection, "2026-01")
        balances = {row["entity_id"]: row for row in january["fund_details"]["balances"]}
        assert balances["cash"]["ending_fen"] == 50000
        assert balances["unused-bank"]["ending_fen"] == 0
        assert len(january["fund_details"]["sources"]["lines"]) == 1
        verify_fund_close_snapshot(connection, january)
    close("2026-02")
    with engine.store.connection(read_only=True) as connection:
        february = stored_manifest(connection, "2026-02")
        assert february["fund_details"]["balances"][0]["opening_fen"] == 50000
        verify_fund_close_snapshot(connection, february, previous_manifest=january)


def test_v4_to_v5_activation_uses_original_verified_per_account_balance(book, monkeypatch):
    from ai_accounting.kernel.fund_line_details import initialize_open_details

    engine, _, _, close = book
    scenario(book)
    with monkeypatch.context() as historical:
        use_v4_close_manifest(historical)
        close("2026-01")
    with engine.store.connection() as connection:
        connection.execute("BEGIN IMMEDIATE")
        initialize_open_details(engine, connection, "2026-02")
        connection.commit()
    close("2026-02")
    with engine.store.connection(read_only=True) as connection:
        january = stored_manifest(connection, "2026-01")
        february = stored_manifest(connection, "2026-02")
        assert january["format_version"] == 4 and "fund_details" not in january
        assert february["format_version"] == 5
        opening = february["fund_details"]["sources"]["opening"]
        assert next(row["amount"] for row in opening if row["entity_id"] == "cash") == 50000
        verify_fund_close_snapshot(connection, february, previous_manifest=january)


def test_closed_fund_identity_is_fixed_and_next_close_adopts_explicit_correction(book):
    from ai_accounting.kernel.entities import Entities
    from ai_accounting.kernel.fund_accounts import require_fund_account

    engine, _, _, close = book
    scenario(book)
    seed_entities(engine, [("unused-bank", "fund_account", "bank")])
    close("2026-01")
    with engine.store.connection(read_only=True) as connection:
        january = stored_manifest(connection, "2026-01")
        original = require_fund_account(connection, "unused-bank")
    original_frozen = deepcopy(january["fund_details"])
    proof = engine.register_evidence(
        b"Synthetic account document explicitly corrects the registered account holder",
        "text/plain",
        "synthetic account correction",
        request_id="identity-correction-proof",
    )["digest"]
    corrected_identity = {**original["identity"], "account_holder_name": "合成更正户名"}
    Entities(engine).correct_fund_account_identity(
        "unused-bank",
        account_type="bank",
        data=corrected_identity,
        expected_identity_digest=original["digest"],
        recording_error_confirmed=True,
        source="synthetic corrected original account document",
        evidence_digest=proof,
        request_id="correct-bank-account-holder",
    )
    with engine.store.connection(read_only=True) as connection:
        verify_fund_close_snapshot(connection, january)
        saved = stored_manifest(connection, "2026-01")
        assert saved["fund_details"] == original_frozen
    close("2026-02")
    with engine.store.connection(read_only=True) as connection:
        february = stored_manifest(connection, "2026-02")
        definitions = {row["entity_id"]: row for row in february["fund_details"]["definitions"]}
        revised = definitions["unused-bank"]
        assert revised["identity"] == corrected_identity
        assert revised["digest"] != original["digest"]
        assert revised["detail_code"] == original["detail_code"]
        verify_fund_close_snapshot(connection, january)
        verify_fund_close_snapshot(connection, february, previous_manifest=january)


def test_closed_opening_account_reassignment_freezes_adjustment_without_money_activity(
    identity_engine,
):
    from test_identity_corrections import confirm, opening_package
    from test_opening_continuation import _close_without_current_business

    engine, proof, owner, _ = identity_engine
    seed_entities(
        engine, [("cash-first", "fund_account", "cash"), ("cash-second", "fund_account", "cash")]
    )
    opening_package(
        engine,
        proof,
        [
            (
                "opening_cash",
                "opening-cash",
                {"cash_account_id": "cash-first", "balance_fen": 10000},
            ),
            (
                "opening_equity",
                "capital",
                {
                    "equity_kind": "paid_in_capital",
                    "balance_fen": 10000,
                    "holder_or_basis_id": owner,
                },
            ),
        ],
    )
    _close_without_current_business(engine, "2026-01", proof)
    with engine.store.connection(read_only=True) as connection:
        january = stored_manifest(connection, "2026-01")
    confirm(
        engine,
        {
            "changes": [
                {
                    "subject_id": "opening-cash",
                    "expected_revision": 1,
                    "action": "reassign",
                    "data": {
                        "period": "2026-01",
                        "package_id": "opening",
                        "cash_account_id": "cash-second",
                        "balance_fen": 10000,
                    },
                }
            ],
            "evidence": [proof],
            "reason": "explicit original cash account identity correction",
            "posting_period": "2026-02",
        },
    )
    _close_without_current_business(engine, "2026-02", proof)
    with engine.store.connection(read_only=True) as connection:
        february = stored_manifest(connection, "2026-02")
        balances = {row["entity_id"]: row for row in february["fund_details"]["balances"]}
        assert balances["cash-first"]["ending_fen"] == 0
        assert balances["cash-first"]["adjustment_fen"] == -10000
        assert balances["cash-second"]["ending_fen"] == 10000
        assert balances["cash-second"]["adjustment_fen"] == 10000
        assert all(row["debit_fen"] == row["credit_fen"] == 0 for row in balances.values())
        assert len(february["fund_details"]["adjustments"]) == 1
        assert february["vouchers"] == []
        verify_fund_close_snapshot(connection, january)
        verify_fund_close_snapshot(connection, february, previous_manifest=january)
